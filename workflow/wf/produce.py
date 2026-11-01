"""多集排队生产：晚上挂着跑，早上看报告。

逐集：分镜 → 站位图 → 提示词 → 报价（超过单集上限或项目剩余预算就跳过这一集）→ 出片 → 等出片
→ 质检 →（可选）按质检意见重抽不合格的一轮 → 配音（按设置）→ 合成 →（可选）导出发布版。
一集失败不影响下一集；每集记录花费、质检、重抽、成片地址，最后写一份生产报告（Markdown）并发通知。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from . import db, llm, media
from .errors import WorkflowError

log = logging.getLogger("workflow.produce")


def _seconds_until(hhmm: str) -> float:
    """“HH:MM” → 距离下一次这个时刻的秒数（本机时间）。空串 = 立即。"""
    if not hhmm:
        return 0.0
    try:
        h, m = [int(x) for x in hhmm.strip().split(":")]
        now = datetime.now()
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, TypeError):
        raise WorkflowError("开始时间格式是 HH:MM，例如 01:30")
    if at <= now:
        at += timedelta(days=1)
    return (at - now).total_seconds()


class ProduceMixin:
    async def produce(self, pid: str, episodes: list[int] | None = None, progress=None,
                      per_episode_budget_rmb: float = 0.0, retake: bool = True, export_preset: str = "",
                      start_at: str = "", wait_timeout_s: float | None = None) -> dict:
        if export_preset and export_preset not in media.PRESETS:
            raise WorkflowError(f"导出预设只能是：{'、'.join(media.PRESETS)}")
        delay = _seconds_until(start_at)
        if delay > 0:
            if progress:
                await progress.start(f"等到 {start_at} 开始")
            await asyncio.sleep(delay)
            if progress:
                await progress.done(f"等到 {start_at} 开始", {})
        llm.current_project.set(pid)
        self._ensure_parsed(pid)
        proj = self.get(pid)
        st = proj["settings"]
        eps = sorted(episodes or [e["no"] for e in proj["episodes"]])
        missing = [n for n in eps if n not in {e["no"] for e in proj["episodes"]}]
        if missing:
            raise WorkflowError(f"没有这些集：{missing}")
        report = {"project": pid, "name": proj["name"], "started_at": db.now(), "episodes": [],
                  "settings": {"video_model": st["video_model"], "resolution": st["resolution"],
                               "per_episode_budget_rmb": per_episode_budget_rmb, "retake": retake,
                               "export_preset": export_preset, "dub_mode": st.get("dub_mode", "off")}}

        async def step(name, coro):
            if progress:
                await progress.start(name)
            try:
                r = await coro
            except Exception as e:
                if progress:
                    await progress.fail(name, str(e))
                raise
            if progress:
                await progress.done(name, r if isinstance(r, dict) and len(str(r)) < 2000 else {})
            return r

        # 全项目共用的：资产与参考图（已有的跳过）
        with db.tx() as c:
            has_assets = c.execute("SELECT COUNT(*) n FROM assets WHERE project_id=?", (pid,)).fetchone()["n"]
        if not has_assets:
            await step("资产抽取", self._brief(self.extract_assets(pid), lambda r: {"added": len(r["added"])}))
        if st["gen_asset_images"]:
            await step("资产参考图", self._brief(self.gen_asset_images(pid), lambda r: {
                "generated": len(r["generated"]), "failed": len(r["failed"])}))

        for no in eps:
            rec = {"episode": no, "status": "running", "errors": []}
            spend0 = self._spend(pid)["total_rmb"]
            t0 = datetime.now()
            try:
                await step(f"第{no}集 分镜", self._brief(self.storyboard(pid, episode_no=no), lambda r: {
                    "shots": sum(x.get("shots", 0) if isinstance(x.get("shots"), int) else 0 for x in r["results"])}))
                if st["gen_blocking"]:
                    await step(f"第{no}集 站位图", self._brief(self.gen_blocking(pid, episode_no=no), lambda r: {
                        "generated": len(r["generated"]), "failed": len(r["failed"])}))
                await step(f"第{no}集 提示词", self._brief(self.prompts(pid, episode_no=no), lambda r: {
                    "generated": r["generated"]}))
                q = await self.quote_videos(pid, episode_no=no)
                rec["quote_rmb"] = q["total_rmb"]
                if per_episode_budget_rmb and q["total_rmb"] > per_episode_budget_rmb:
                    raise WorkflowError(f"本集出片预计 ¥{q['total_rmb']:.2f}，超过单集上限 ¥{per_episode_budget_rmb:.2f}，跳过")
                if q["items"] and not q["within_budget"]:
                    raise WorkflowError(f"本集出片预计 ¥{q['total_rmb']:.2f}，超过项目剩余预算 ¥{q['remaining_rmb']:.2f}，跳过")
                await step(f"第{no}集 出片", self._brief(
                    self.submit_videos(pid, episode_no=no, accept_stale=True, allow_missing=True),
                    lambda r: {"submitted": len(r["submitted"]), "skipped": len(r["skipped"])}))
                await step(f"第{no}集 等出片", self.wait_videos(pid, wait_timeout_s))
                await self.drain_background()
                qc = await step(f"第{no}集 质检", self.qc_project(pid, episode_no=no))
                rec["qc"] = {k: qc[k] for k in ("checked", "pass", "warn", "fail")}
                if retake and qc["fail"]:
                    try:
                        rt = await step(f"第{no}集 重抽不合格", self._brief(
                            self.retake_shots(pid, episode_no=no), lambda r: {"submitted": len(r["submitted"])}))
                        rec["retakes"] = rt["submitted"]
                        await self.wait_videos(pid, wait_timeout_s)
                        await self.drain_background()
                        qc = await self.qc_project(pid, episode_no=no)
                        rec["qc_after_retake"] = {k: qc[k] for k in ("checked", "pass", "warn", "fail")}
                    except WorkflowError as e:
                        rec["errors"].append(f"重抽：{e}")
                asm = await step(f"第{no}集 合成", self.assemble(pid, no, allow_partial=True))
                ep = asm["episodes"][0]
                rec.update(final_url=ep["url"], seconds=ep["seconds"], missing=ep["missing"], dubbed=ep.get("dubbed", 0))
                if export_preset:
                    ex = await step(f"第{no}集 导出", self.export_episode(pid, no, export_preset))
                    rec["export_url"] = ex["url"]
                rec["status"] = "done" if not ep["missing"] else "partial"
            except Exception as e:  # 一集失败不影响下一集
                log.warning("produce ep%s failed: %s", no, e)
                rec["status"] = "failed"
                rec["errors"].append(str(e)[:300])
            rec["spent_rmb"] = round(self._spend(pid)["total_rmb"] - spend0, 4)
            rec["minutes"] = round((datetime.now() - t0).total_seconds() / 60, 1)
            report["episodes"].append(rec)
        report["finished_at"] = db.now()
        report["spend"] = self._spend(pid)
        path = media.path_for(pid, "exports", f"生产报告_{datetime.now():%Y%m%d_%H%M}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._report_md(report))
        report["report_url"] = media.url_of(path)
        ok = [r["episode"] for r in report["episodes"] if r["status"] == "done"]
        bad = [r["episode"] for r in report["episodes"] if r["status"] != "done"]
        self.notify(pid, "error" if bad and not ok else ("warning" if bad else "success"),
                    f"批量生产结束：{len(ok)} 集完成" + (f"，{len(bad)} 集未完成" if bad else ""),
                    f"花费 ¥{sum(r['spent_rmb'] for r in report['episodes']):.2f}；报告：{report['report_url']}")
        return report

    @staticmethod
    async def _brief(coro, fn):
        return fn(await coro)

    @staticmethod
    def _report_md(r: dict) -> str:
        s = r["settings"]
        out = [f"# 生产报告 · {r['name']}", "",
               f"- 时间：{r['started_at']} → {r.get('finished_at', '')}",
               f"- 出片：{s['video_model']} · {s['resolution']}；单集上限 ¥{s['per_episode_budget_rmb'] or '不限'}；"
               f"重抽 {'开' if s['retake'] else '关'}；配音 {s['dub_mode']}；导出 {s['export_preset'] or '不导出'}",
               f"- 项目累计花费：¥{r['spend']['total_rmb']:.2f} / 预算 ¥{r['spend']['budget_rmb']:.2f}", "",
               "| 集 | 结果 | 时长 | 本集花费 | 质检（合格/待看/不合格） | 重抽 | 用时 | 备注 |", "|---|---|---|---|---|---|---|---|"]
        name = {"done": "✅ 完成", "partial": "⚠ 缺镜头", "failed": "❌ 未完成"}
        for e in r["episodes"]:
            q = e.get("qc_after_retake") or e.get("qc") or {}
            qs = f"{q.get('pass', '-')}/{q.get('warn', '-')}/{q.get('fail', '-')}" if q else "-"
            note = "；".join(e["errors"])[:120] or (f"缺 {'、'.join(e['missing'])}" if e.get("missing") else "")
            out.append(f"| 第{e['episode']}集 | {name.get(e['status'], e['status'])} | {e.get('seconds', 0):.1f}s | "
                       f"¥{e['spent_rmb']:.2f} | {qs} | {len(e.get('retakes', []))} | {e['minutes']} 分 | {note} |")
        out += ["", "成片和导出文件在工作台“成片”页；不合格镜头在“剪辑”页标红。"]
        return "\n".join(out) + "\n"
