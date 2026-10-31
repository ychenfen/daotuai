"""视频：报价 → 预算 → 提交（单镜参数可覆盖、参考素材可勾选、多版本、幂等）→ 回执 → 下载/封面 → 选版本 → 按集合成 → 批量下载。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import zipfile

from . import db, media
from .agg import AggError
from .errors import BudgetExceeded, Conflict, NotFound, WorkflowError
from .util import new_id, short_hash

log = logging.getLogger("workflow.video")

ACTIVE_JOB = ("submitted", "running")


class VideoMixin:
    @staticmethod
    def _job_view(j: dict) -> dict:
        j["refs"] = db.load(j.pop("refs_json"), [])
        j["play_url"] = media.url_of(j["local_path"]) or ("" if media.is_mock_url(j["url"]) else j["url"])
        j["poster_url"] = media.url_of(j["poster_path"])
        return j

    async def model_info(self, model_id: str) -> dict:
        if self._models is None or model_id not in self._models:
            self._models = {m["id"]: m for m in await self.agg.models()}
        m = self._models.get(model_id)
        if not m:
            raise WorkflowError(f"聚合层没有这个模型：{model_id}")
        return m

    @staticmethod
    def _clamp_duration(seconds: float, m: dict) -> float:
        d = m.get("duration") or {}
        lo, hi = float(d.get("min", 4)), float(d.get("max", 15))
        return float(max(lo, min(hi, round(seconds or 5))))

    @staticmethod
    def _pick_targets(proj: dict, shot_ids: list[str] | None, force: bool,
                      episode_no: int | None = None) -> tuple[list[dict], list[dict]]:
        targets, skipped = [], []
        for s in proj["shots"]:
            if shot_ids and s["id"] not in shot_ids:
                continue
            if episode_no is not None and s["episode_no"] != episode_no:
                continue
            if not s.get("prompt") or not s["prompt"].get("positive"):
                skipped.append({"shot": s["id"], "code": s["code"], "reason": "还没有提示词"})
                continue
            active = [j for j in s["jobs"] if j["status"] in ACTIVE_JOB]
            if active and not force:
                skipped.append({"shot": s["id"], "code": s["code"], "reason": "已经在生成"})
                continue
            done = [j for j in s["jobs"] if j["status"] == "succeeded"]
            if done and not force and not shot_ids:
                skipped.append({"shot": s["id"], "code": s["code"], "reason": "已有成片（重做请单独选这个镜头）"})
                continue
            targets.append(s)
        return targets, skipped

    async def quote_videos(self, pid: str, shot_ids: list[str] | None = None, force: bool = False,
                           resolution: str | None = None, model: str | None = None,
                           duration: float | None = None, episode_no: int | None = None) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        model = model or st["video_model"]
        res = (resolution or st["resolution"]).upper()
        m = await self.model_info(model)
        if m.get("specs") and res not in m["specs"]:
            raise WorkflowError(f"{model} 不支持 {res}，可选：{'/'.join(m['specs'])}")
        targets, skipped = self._pick_targets(proj, shot_ids, force, episode_no)
        items, total = [], 0.0
        for s in targets:
            dur = self._clamp_duration(duration or s["duration_s"], m)
            q = await self.agg.quote(model, spec=res, seconds=dur)
            items.append({"shot": s["id"], "code": s["code"], "episode": s["episode_no"], "shot_no": s["shot_no"],
                          "seconds": dur, "rmb": q["rmb"], "points": q.get("points")})
            total += q["rmb"]
        spend = proj["spend"]
        return {"model": model, "resolution": res, "live": bool(m.get("live")), "items": items,
                "skipped": skipped, "total_rmb": round(total, 4), "total_points": round(total * 10, 2),
                "spent_rmb": spend["total_rmb"], "budget_rmb": spend["budget_rmb"],
                "remaining_rmb": spend["remaining_rmb"], "within_budget": total <= spend["remaining_rmb"] + 1e-9}

    def _video_refs(self, proj: dict, s: dict, mode: str, m: dict, form_ids: list[str] | None,
                    material_ids: list[str] | None) -> tuple[str, list[dict], list[dict]]:
        """决定出片模式与参考素材。返回 (mode, 给上游的 images, 记录用 refs)。
        顺序：站位图 → 绑定（或手选）的资产形态 → 手选的本地素材；音频只在模型支持“音频参考”时带上。"""
        st = proj["settings"]
        modes = m.get("modes") or []
        forms = {f["id"]: (a, f) for a in proj["assets"] for f in a["forms"]}
        if form_ids is not None:
            chosen = [forms[x] for x in form_ids if x in forms]
        else:
            chosen = [forms[b["form_id"]] for b in ((s.get("prompt") or {}).get("bindings") or [])
                      if b.get("form_id") in forms]
        mats = [x for x in proj["materials"] if material_ids and x["id"] in material_ids]
        img_refs = [(a, f) for a, f in chosen if a["kind"] != "audio" and (f["local_path"] or f["image_url"])]
        audio_refs = [(a, f) for a, f in chosen if a["kind"] == "audio" and f["local_path"]]
        has_blocking = bool(st["use_blocking_ref"] and s.get("blocking_image"))
        if mode == "auto":
            if (img_refs or mats or has_blocking) and "全能参考" in modes:
                mode = "reference"
            elif s.get("frame") and "首帧参考" in modes:
                mode = "first_frame"
            else:
                mode = "text"
        images, record = [], []
        if mode == "reference":
            if has_blocking:
                sc = next((x for x in proj["scenes"] if x["id"] == s["scene_id"]), None)
                if sc:
                    images.append({"url": self._ref_url(sc["blocking_path"], sc["blocking_url"]),
                                   "role": "reference_image"})
                    record.append({"blocking": sc["code"], "role": "reference_image"})
            for a, f in img_refs[:6]:
                images.append({"url": self._ref_url(f["local_path"], f["image_url"]), "role": "reference_image"})
                record.append({"asset_id": a["id"], "form_id": f["id"], "name": a["name"], "form": f["name"],
                               "version": f["current_version"], "role": "reference_image"})
            for x in mats:
                if x["media_type"] == "image":
                    images.append({"url": media.data_uri(x["local_path"]), "role": "reference_image"})
                    record.append({"material_id": x["id"], "name": x["name"], "role": "reference_image"})
            if "音频参考" in modes:
                for a, f in audio_refs[:3]:
                    images.append({"url": self._ref_url(f["local_path"], ""), "role": "reference_audio"})
                    record.append({"asset_id": a["id"], "name": a["name"], "version": f["current_version"],
                                   "role": "reference_audio"})
            images = images[:9]
        elif mode == "first_frame":
            if s.get("frame_path") or s.get("frame_url"):
                images.append({"url": self._ref_url(s.get("frame_path", ""), s.get("frame_url", "")),
                               "role": "first_frame"})
                record.append({"frame": True, "role": "first_frame"})
            else:
                mode = "text"
        return mode, images, record

    async def submit_videos(self, pid: str, shot_ids: list[str] | None = None, force: bool = False,
                            resolution: str | None = None, mode: str | None = None, model: str | None = None,
                            duration: float | None = None, aspect_ratio: str | None = None,
                            form_ids: list[str] | None = None, material_ids: list[str] | None = None,
                            episode_no: int | None = None, accept_stale: bool = False,
                            allow_missing: bool = False) -> dict:
        quote = await self.quote_videos(pid, shot_ids, force, resolution, model, duration, episode_no)
        if quote["items"] and not quote["within_budget"]:
            raise BudgetExceeded(
                f"超出项目预算：本次约 ¥{quote['total_rmb']:.2f}，剩余 ¥{quote['remaining_rmb']:.2f}"
                f"（预算 ¥{quote['budget_rmb']:.2f}）。调高预算或少选几个镜头。", quote)
        self._reference_gate(pid, [i["shot"] for i in quote["items"]], accept_stale, allow_missing, form_ids)
        self.resolve_references(pid, shot_ids)
        proj = self.get(pid)
        st = proj["settings"]
        model, res = quote["model"], quote["resolution"]
        aspect = aspect_ratio or st["aspect_ratio"]
        m = await self.model_info(model)
        mode = mode or st["video_mode"]
        if mode not in ("auto", "text", "first_frame", "reference"):
            raise WorkflowError("mode 只能是 auto/text/first_frame/reference")
        shots = {s["id"]: s for s in proj["shots"]}
        submitted, skipped = [], list(quote["skipped"])
        for item in quote["items"]:
            s = shots[item["shot"]]
            used_mode, images, record = self._video_refs(proj, s, mode, m, form_ids, material_ids)
            version = (max((j["version"] for j in s["jobs"]), default=0)) + 1
            crid = short_hash(s["id"], version, s["prompt"]["rev"], model, res, item["seconds"], used_mode)
            prompt = s["prompt"]["positive"]
            if s["prompt"].get("negative"):
                prompt += f"\n避免：{s['prompt']['negative']}"
            try:
                r = await self._with_topup(lambda: self.agg.submit_video(
                    model, prompt, res, item["seconds"], aspect, images or None, idem=crid))
            except AggError as e:
                skipped.append({"shot": s["id"], "code": s["code"], "reason": str(e)[:200]})
                continue
            jid = new_id("job")
            with db.tx() as c:
                c.execute("""INSERT INTO jobs(id,shot_id,version,client_request_id,agg_task_id,model,resolution,
                             aspect_ratio,duration_s,mode,refs_json,prompt_snapshot,status,quoted_rmb,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (jid, s["id"], version, crid, r["id"], model, res, aspect, item["seconds"], used_mode,
                           db.dump(record), prompt, "submitted",
                           float((r.get("billing") or {}).get("quoted_rmb") or item["rmb"]), db.now(), db.now()))
                c.execute("UPDATE shots SET status='video_submitted' WHERE id=?", (s["id"],))
            submitted.append({"shot": s["id"], "code": s["code"], "episode": s["episode_no"],
                              "shot_no": s["shot_no"], "job": jid, "version": version, "mode": used_mode,
                              "refs": len(images), "task": r["id"],
                              "quoted_rmb": (r.get("billing") or {}).get("quoted_rmb")})
        self.refresh_status(pid)
        if submitted:
            self.notify(pid, "info", f"已提交 {len(submitted)} 个镜头出片",
                        f"{model} · {res} · 预计 ¥{quote['total_rmb']:.2f}")
        return {"project": pid, "submitted": submitted, "skipped": skipped, "quote": quote}

    def _reference_gate(self, pid: str, shot_ids: list[str], accept_stale: bool, allow_missing: bool,
                        form_ids: list[str] | None) -> None:
        """提交前的参考一致性闸门（对应刀徒的“参考素材已变化”，但给出明细并允许一键确认）：
        - 提示词写好后，引用的资产又换了图（版本变了）→ 需要 accept_stale 确认用最新版；
        - 引用的资产还没有图 → 需要 allow_missing 确认不带它的参考图出片。"""
        if accept_stale and allow_missing:
            return
        proj = self.get(pid)
        forms = {f["id"]: (a, f) for a in proj["assets"] for f in a["forms"]}
        stale, missing = [], []
        for s in proj["shots"]:
            if s["id"] not in shot_ids or not s.get("prompt"):
                continue
            binds = s["prompt"]["bindings"]
            if form_ids is not None:   # 手选参考时只检查手选的
                binds = [{"form_id": x, "version": forms[x][1]["current_version"]} for x in form_ids if x in forms]
            for b in binds:
                af = forms.get(b.get("form_id") or "")
                if not af:
                    continue
                a, f = af
                if a["kind"] == "audio":
                    continue
                label = a["name"] + ("" if f["is_default"] else f"（{f['name']}）")
                if not f["current_version"]:
                    missing.append({"code": s["code"], "asset": label})
                elif int(b.get("version") or 0) != int(f["current_version"]):
                    stale.append({"code": s["code"], "asset": label, "from": b.get("version"), "to": f["current_version"]})
        problems = {}
        if stale and not accept_stale:
            problems["stale"] = stale
        if missing and not allow_missing:
            problems["missing"] = missing
        if problems:
            parts = []
            if "stale" in problems:
                parts.append("这些资产在写提示词之后换过图：" + "、".join(
                    f"{x['code']} {x['asset']} v{x['from']}→v{x['to']}" for x in stale[:8]))
            if "missing" in problems:
                parts.append("这些引用的资产还没有图：" + "、".join(f"{x['code']} {x['asset']}" for x in missing[:8]))
            raise Conflict("参考素材已变化。" + "；".join(parts) + "。确认后会按当前版本提交。",
                           {"code": "reference_changed", **problems})

    async def poll_videos(self, pid: str | None = None) -> dict:
        """推进所有在跑的视频任务（pid=None 时跨项目）。成功即下载到本地、抽封面，并设为该镜头的选用版本。
        后台轮询器和一键任务可能同时调用，这里串行化，避免同一任务被结算/下载两次。"""
        if self._poll_lock is None:
            self._poll_lock = asyncio.Lock()
        async with self._poll_lock:
            return await self._poll_videos(pid)

    async def _poll_videos(self, pid: str | None) -> dict:
        sql = ("SELECT j.*, s.project_id, s.shot_no, e.no episode_no FROM jobs j JOIN shots s ON s.id=j.shot_id "
               "JOIN episodes e ON e.id=s.episode_id WHERE j.status IN ('submitted','running')")
        args: tuple = ()
        if pid:
            sql += " AND s.project_id=?"
            args = (pid,)
        with db.tx() as c:
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
        done, failed, running = [], [], []
        projects: dict[str, list] = {}
        for j in rows:
            code = f"{j['episode_no']}-{j['shot_no']}"
            try:
                r = await self.agg.poll_video(j["agg_task_id"])
            except AggError as e:
                with db.tx() as c:
                    c.execute("UPDATE jobs SET error=?, updated_at=? WHERE id=?", (str(e)[:300], db.now(), j["id"]))
                running.append({"job": j["id"], "code": code, "error": str(e)[:120]})
                continue
            status = r.get("status")
            if status == "succeeded":
                url = r.get("url") or ""
                seconds = float(r.get("seconds") or j["duration_s"])
                local, poster = await self._store_video(j["project_id"], j, url, seconds)
                charged = float((r.get("billing") or {}).get("charged_rmb") or 0)
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='succeeded', url=?, local_path=?, poster_path=?, seconds=?,"
                              " charged_rmb=?, error='', updated_at=? WHERE id=?",
                              (url, local, poster, seconds, charged, db.now(), j["id"]))
                    c.execute("UPDATE shots SET status='video_done', selected_job_id=? WHERE id=?",
                              (j["id"], j["shot_id"]))
                done.append({"job": j["id"], "shot": j["shot_id"], "code": code, "url": url, "local": bool(local),
                             "charged_rmb": charged})
                projects.setdefault(j["project_id"], []).append(("ok", code))
            elif status in ("failed", "cancelled"):
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                              (status, str(r.get("error") or "上游失败，已退回预扣"), db.now(), j["id"]))
                    has_ok = c.execute("SELECT 1 FROM jobs WHERE shot_id=? AND status='succeeded'",
                                       (j["shot_id"],)).fetchone()
                    c.execute("UPDATE shots SET status=? WHERE id=?",
                              ("video_done" if has_ok else "video_failed", j["shot_id"]))
                failed.append({"job": j["id"], "shot": j["shot_id"], "code": code})
                projects.setdefault(j["project_id"], []).append(("bad", code))
            else:
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (db.now(), j["id"]))
                running.append({"job": j["id"], "shot": j["shot_id"], "code": code})
        for p, evs in projects.items():
            self.refresh_status(p)
            ok = [c for k, c in evs if k == "ok"]
            bad = [c for k, c in evs if k == "bad"]
            if ok:
                self.notify(p, "success", f"{len(ok)} 个镜头出片完成", "、".join(ok[:20]))
            if bad:
                self.notify(p, "error", f"{len(bad)} 个镜头出片失败（已退回预扣）", "、".join(bad[:20]))
        return {"project": pid, "done": done, "failed": failed, "running": running}

    async def _store_video(self, pid: str, j: dict, url: str, seconds: float) -> tuple[str, str]:
        path = media.path_for(pid, "videos", f"{j['shot_id']}_v{j['version']}.mp4")
        try:
            if not url or media.is_mock_url(url):
                ok = await media.placeholder_mp4(path, seconds, seed=f"{j['shot_id']}{j['version']}",
                                                 aspect=j.get("aspect_ratio") or "9:16")
                if not ok:
                    return "", ""
            else:
                data = await self.agg.fetch(url)
                with open(path, "wb") as f:
                    f.write(data)
        except Exception as e:  # 下载失败不影响任务成功；界面上仍可用上游 URL 播放
            log.warning("download video failed %s: %s", url[:80], e)
            return "", ""
        poster = path[:-4] + ".jpg"
        try:
            await media.poster(path, poster)
        except Exception as e:  # 封面失败无所谓
            log.info("poster failed: %s", e)
            poster = ""
        return path, poster

    async def wait_videos(self, pid: str, timeout_s: float | None = None, interval_s: float | None = None) -> dict:
        timeout_s = float(timeout_s if timeout_s is not None else os.environ.get("WORKFLOW_WAIT_TIMEOUT_S", 3600))
        interval_s = float(interval_s if interval_s is not None else os.environ.get("WORKFLOW_WAIT_INTERVAL_S", 10))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        rounds = 0
        while True:
            rounds += 1
            r = await self.poll_videos(pid)
            if not r["running"]:
                break
            if loop.time() >= deadline:
                return {"rounds": rounds, "timed_out": True, "running": len(r["running"])}
            await asyncio.sleep(interval_s)
        with db.tx() as c:
            counts = {row["status"]: row["n"] for row in c.execute(
                "SELECT j.status, COUNT(*) n FROM jobs j JOIN shots s ON s.id=j.shot_id WHERE s.project_id=? "
                "GROUP BY j.status", (pid,)).fetchall()}
        return {"rounds": rounds, "timed_out": False, "jobs": counts}

    async def cancel_job(self, job_id: str) -> dict:
        with db.tx() as c:
            j = c.execute("SELECT j.*, s.project_id FROM jobs j JOIN shots s ON s.id=j.shot_id WHERE j.id=?",
                          (job_id,)).fetchone()
        if not j:
            raise NotFound(f"任务不存在：{job_id}")
        if j["status"] not in ACTIVE_JOB:
            return {"job": job_id, "status": j["status"]}
        r = await self.agg.cancel_video(j["agg_task_id"])
        with db.tx() as c:
            c.execute("UPDATE jobs SET status='cancelled', error='手动取消', updated_at=? WHERE id=?",
                      (db.now(), job_id))
            has_ok = c.execute("SELECT 1 FROM jobs WHERE shot_id=? AND status='succeeded'",
                               (j["shot_id"],)).fetchone()
            c.execute("UPDATE shots SET status=? WHERE id=?",
                      ("video_done" if has_ok else "prompt_ready", j["shot_id"]))
        self.refresh_status(j["project_id"])
        return {"job": job_id, "status": "cancelled", "refund": r.get("refund")}

    def select_job(self, shot_id: str, job_id: str) -> dict:
        with db.tx() as c:
            j = c.execute("SELECT * FROM jobs WHERE id=? AND shot_id=?", (job_id, shot_id)).fetchone()
            if not j:
                raise NotFound("这个镜头没有该版本")
            if j["status"] != "succeeded":
                raise WorkflowError("只能选用已经成功的版本")
            c.execute("UPDATE shots SET selected_job_id=? WHERE id=?", (job_id, shot_id))
            ep = c.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
            c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id=?",
                      (ep["episode_id"],))
        return {"shot": shot_id, "selected_job_id": job_id}

    # ------------------------------------------------------------------
    # 成片
    # ------------------------------------------------------------------

    async def assemble(self, pid: str, episode_no: int | None = None, allow_partial: bool = False) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        out = []
        for ep in proj["episodes"]:
            if episode_no is not None and ep["no"] != episode_no:
                continue
            shots = [s for s in proj["shots"] if s["episode_id"] == ep["id"]]
            if not shots:
                continue
            clips, missing = [], []
            for s in shots:
                j = s["job"] if s["job"] and s["job"]["status"] == "succeeded" else None
                if j and j["local_path"] and os.path.exists(j["local_path"]):
                    clips.append({"path": j["local_path"], "seconds": j["seconds"],
                                  "dialogue": self._subtitle_text(s["dialogue"])})
                else:
                    missing.append(s["code"])
            if missing and not allow_partial:
                raise WorkflowError(f"第 {ep['no']} 集还有镜头没出片或没下载：{'、'.join(missing)}")
            if not clips:
                continue
            name = f"EP{ep['no']:02d}"
            final = media.path_for(pid, "exports", f"{name}.mp4")
            srt = media.path_for(pid, "exports", f"{name}.srt")
            r = await media.assemble(clips, final, srt, st["aspect_ratio"], st["burn_subtitles"])
            with db.tx() as c:
                c.execute("UPDATE episodes SET final_path=?, srt_path=?, final_seconds=?, final_at=? WHERE id=?",
                          (final, srt, r["seconds"], db.now(), ep["id"]))
            out.append({"episode": ep["no"], "url": media.url_of(final), "srt_url": media.url_of(srt),
                        "seconds": r["seconds"], "clips": r["clips"], "missing": missing,
                        "subtitles": r["subtitles"]})
        if not out:
            raise WorkflowError("没有可合成的剧集（先把视频跑完）")
        self.refresh_status(pid)
        self.notify(pid, "success", f"成片已合成：{len(out)} 集",
                    "、".join(f"第{x['episode']}集 {x['seconds']:.1f}s" for x in out))
        return {"project": pid, "episodes": out}

    @staticmethod
    def _subtitle_text(dialogue: str) -> str:
        """字幕只放台词本身：去掉“角色：”和表演括号。"""
        lines = []
        for ln in (dialogue or "").splitlines():
            ln = re.sub(r"^\s*[^：:]{1,10}[：:]\s*", "", ln).strip()
            ln = re.sub(r"[（(][^）)]*[）)]", "", ln).strip("{}“”\" ")
            if ln:
                lines.append(ln)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 批量下载（zip）
    # ------------------------------------------------------------------

    def build_zip(self, pid: str, what: str = "videos", episode_no: int | None = None,
                  shot_ids: list[str] | None = None) -> tuple[str, str]:
        """返回 (zip 临时路径, 下载文件名)。what: videos | assets | finals | all。"""
        proj = self.get(pid)
        files: list[tuple[str, str]] = []
        if what in ("videos", "all"):
            for s in proj["shots"]:
                if (episode_no is not None and s["episode_no"] != episode_no) or (shot_ids and s["id"] not in shot_ids):
                    continue
                j = s["job"]
                if j and j["local_path"] and os.path.exists(j["local_path"]):
                    files.append((j["local_path"], f"视频/第{s['episode_no']}集/{s['code']}_v{j['version']}.mp4"))
        if what in ("assets", "all"):
            for a in proj["assets"]:
                for f in a["forms"]:
                    if f["local_path"] and os.path.exists(f["local_path"]):
                        ext = os.path.splitext(f["local_path"])[1]
                        stem = a["name"] if f["is_default"] else f"{a['name']}-{f['name']}"
                        files.append((f["local_path"], f"资产/{a['kind_cn']}/{stem}{ext}"))
            for sc in proj["scenes"]:
                if sc["blocking_path"] and os.path.exists(sc["blocking_path"]):
                    files.append((sc["blocking_path"], f"站位图/{sc['code']}_{sc['location'] or '场景'}"
                                                      f"{os.path.splitext(sc['blocking_path'])[1]}"))
        if what in ("finals", "all"):
            for e in proj["episodes"]:
                for p, ext in ((e["final_path"], ".mp4"), (e["srt_path"], ".srt")):
                    if p and os.path.exists(p):
                        files.append((p, f"成片/第{e['no']}集{ext}"))
        if not files:
            raise WorkflowError("没有可下载的文件")
        fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="dl_")
        os.close(fd)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for src, arc in files:
                z.write(src, arc)
        label = {"videos": "视频", "assets": "资产", "finals": "成片", "all": "全部"}.get(what, what)
        return tmp, f"{proj['name']}-{label}.zip"


__all__ = ["VideoMixin", "ACTIVE_JOB", "new_id"]
