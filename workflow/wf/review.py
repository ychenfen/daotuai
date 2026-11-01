"""质量闭环：按质检意见重抽（可自动）、重抽后版本择优、审片（通过/打回）、动作过密检查与拆卡。

实测第 1 集：12 镜里 1 镜不合格、1 镜待看，问题几乎都是“分镜里写的关键动作没演出来”。
两条路一起走：事后——把质检/审片意见写回提示词再抽一次，新版本更差就自动保留旧版本；
事前——分镜一张卡只放一两个动作，塞太满的提示拆卡，并能一键拆。
"""
from __future__ import annotations

import logging

from . import db, llm
from .errors import BudgetExceeded, NotFound, WorkflowError
from .util import new_id

log = logging.getLogger("workflow.review")

RETAKE_VERDICTS = ("fail",)


class ReviewMixin:
    # ==================================================================
    # 审片
    # ==================================================================

    def review_job(self, job_id: str, status: str, note: str = "") -> dict:
        if status not in ("approved", "rejected", ""):
            raise WorkflowError("审片结果只能是 approved（通过）/ rejected（打回）/ 空（撤销）")
        with db.tx() as c:
            j = c.execute("SELECT j.id, j.status, s.project_id, s.id sid FROM jobs j JOIN shots s ON s.id=j.shot_id "
                          "WHERE j.id=?", (job_id,)).fetchone()
            if not j:
                raise NotFound(f"没有这个视频版本：{job_id}")
            if j["status"] != "succeeded":
                raise WorkflowError("只能审已经出片成功的版本")
            if status == "rejected" and not note.strip():
                raise WorkflowError("打回要写一句原因（会写进重抽的提示词）")
            c.execute("UPDATE jobs SET review=?, review_note=?, updated_at=? WHERE id=?",
                      (status, note.strip()[:300], db.now(), job_id))
        return {"job": job_id, "review": status, "note": note.strip()[:300]}

    # ==================================================================
    # 按质检意见重抽
    # ==================================================================

    @staticmethod
    def _retake_issues(j: dict, explicit: bool, verdicts: tuple[str, ...]) -> list[dict]:
        issues: list[dict] = []
        q = j.get("qc")
        if q and (explicit or q["verdict"] in verdicts or j.get("review") == "rejected"):
            serious = [i for i in q.get("issues", []) if i.get("severity") != "轻微"]
            issues += serious or ([] if q["verdict"] == "pass" and not explicit else q.get("issues", []))
        if j.get("review") == "rejected":
            issues.append({"type": "人工意见", "severity": "严重", "detail": j.get("review_note") or "审片打回"})
        return issues[:8]

    async def retake_shots(self, pid: str, shot_ids: list[str] | None = None, episode_no: int | None = None,
                           verdicts: tuple[str, ...] | list[str] = RETAKE_VERDICTS, engine: str | None = None,
                           auto: bool = False) -> dict:
        """把质检问题（和审片打回意见）写回提示词，再出一个新版本。
        不指定镜头时：本集/全项目里 质检不合格（verdicts）或审片打回 的镜头。"""
        proj = self.get(pid)
        st = proj["settings"]
        verdicts = tuple(verdicts)
        targets = []
        for s in proj["shots"]:
            if episode_no is not None and s["episode_no"] != episode_no:
                continue
            if shot_ids and s["id"] not in shot_ids:
                continue
            j = s["job"]
            if not j or j["status"] != "succeeded" or not s["prompt"]:
                continue
            if any(x["status"] in ("submitted", "running") for x in s["jobs"]):
                continue
            issues = self._retake_issues(j, bool(shot_ids), verdicts)
            if issues:
                targets.append((s, j, issues))
        if not targets:
            raise WorkflowError("没有需要重抽的镜头（只重抽质检不合格或审片打回、且已有提示词的镜头）")
        llm.current_project.set(pid)
        outs = await self.gather_limited(
            llm.revise_prompt(self.agg, st["text_model"], s["prompt"]["positive"], issues,
                              [c for c in (s.get("characters") or [])], engine or st["engine"])
            for s, _, issues in targets)
        plan = {}
        with db.tx() as c:
            for (s, j, issues), out in zip(targets, outs):
                if isinstance(out, Exception):
                    raise out
                new, changes, source = out
                c.execute("UPDATE prompts SET positive=?, rev=rev+1, updated_at=? WHERE id=?",
                          (new, db.now(), s["prompt"]["id"]))
                plan[s["id"]] = {"from_job": j["id"], "from_version": j["version"],
                                 "from_score": (j.get("qc") or {}).get("score"), "issues": issues,
                                 "changes": changes, "source": source, "auto": auto}
        try:
            r = await self.submit_videos(pid, shot_ids=list(plan), force=True, accept_stale=True, allow_missing=True)
        except BudgetExceeded:
            raise
        with db.tx() as c:
            for sub in r["submitted"]:
                p = plan[sub["shot"]]
                c.execute("UPDATE jobs SET retake_of=?, retake_json=? WHERE id=?",
                          (p["from_job"], db.dump(p), sub["job"]))
        codes = [x["code"] for x in r["submitted"]]
        if codes:
            self.notify(pid, "info", f"{'自动' if auto else ''}按质检意见重抽 {len(codes)} 个镜头", "、".join(codes))
        return {"project": pid, "submitted": r["submitted"], "skipped": r["skipped"], "quote": r["quote"],
                "plans": [{"shot": k, "code": next(s["code"] for s, _, _ in targets if s["id"] == k), **v}
                          for k, v in plan.items()]}

    def _retake_count(self, shot_id: str) -> int:
        with db.tx() as c:
            return c.execute("SELECT COUNT(*) n FROM jobs WHERE shot_id=? AND retake_of!='' "
                             "AND status NOT IN ('failed','cancelled')", (shot_id,)).fetchone()["n"]

    def keep_better(self, job_id: str) -> dict | None:
        """重抽版本质检完：分数比原版本低，就把镜头的选用版本改回原版本。返回比较结果。"""
        with db.tx() as c:
            j = c.execute("SELECT id, shot_id, retake_of, qc_json, version FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not j or not j["retake_of"]:
                return None
            old = c.execute("SELECT id, qc_json, status, version FROM jobs WHERE id=?", (j["retake_of"],)).fetchone()
            if not old or old["status"] != "succeeded":
                return None
            new_q, old_q = db.load(j["qc_json"], {}) or {}, db.load(old["qc_json"], {}) or {}
            if "score" not in new_q or "score" not in old_q:
                return None
            better = new_q["score"] >= old_q["score"]
            chosen = j["id"] if better else old["id"]
            c.execute("UPDATE shots SET selected_job_id=? WHERE id=?", (chosen, j["shot_id"]))
        return {"shot": j["shot_id"], "new": job_id, "new_score": new_q["score"], "old": old["id"],
                "old_score": old_q["score"], "kept": "new" if better else "old",
                "new_version": j["version"], "old_version": old["version"]}

    async def after_auto_qc(self, pid: str, results: list[tuple[str, dict]]) -> None:
        """自动质检之后：重抽版本择优；不合格且开了自动重抽的，在次数和预算内再抽一次。"""
        st = self.settings(pid)
        kept_old = []
        for jid, q in results:
            cmp_ = self.keep_better(jid)
            if cmp_ and cmp_["kept"] == "old":
                kept_old.append(f"v{cmp_['new_version']} {cmp_['new_score']} 分 < v{cmp_['old_version']} "
                                f"{cmp_['old_score']} 分")
        if kept_old:
            self.notify(pid, "warning", f"{len(kept_old)} 个重抽版本不如原版本，已保留原版本", "\n".join(kept_old))
        if not st.get("auto_retake"):
            return
        limit = int(st.get("retake_max") or 1)
        want = []
        with db.tx() as c:
            for jid, q in results:
                if q.get("verdict") not in RETAKE_VERDICTS:
                    continue
                r = c.execute("SELECT shot_id FROM jobs WHERE id=?", (jid,)).fetchone()
                sid = r["shot_id"] if r else ""
                sel = c.execute("SELECT selected_job_id FROM shots WHERE id=?", (sid,)).fetchone()
                if sel and sel["selected_job_id"] == jid:   # 被择优换掉的旧版本不再抽
                    want.append(sid)
        want = [sid for sid in dict.fromkeys(want) if self._retake_count(sid) < limit]
        if not want:
            return
        try:
            await self.retake_shots(pid, shot_ids=want, auto=True)
        except (BudgetExceeded, WorkflowError) as e:
            self.notify(pid, "warning", "自动重抽没有提交", str(e)[:300])

    # ==================================================================
    # 剪辑精修：入点/出点、叠化
    # ==================================================================

    def set_cut(self, sid: str, trim_in: float | None = None, trim_out: float | None = None,
                xfade_s: float | None = None) -> dict:
        """片头/片尾各剪掉几秒（模型出片常有起势慢、收尾拖的问题），和下一镜叠化几秒（0 = 硬切）。"""
        with db.tx() as c:
            s = c.execute("SELECT s.*, j.seconds jsec FROM shots s LEFT JOIN jobs j ON j.id=s.selected_job_id "
                          "WHERE s.id=?", (sid,)).fetchone()
            if not s:
                raise NotFound("镜头不存在")
            dur = float(s["jsec"] or s["duration_s"] or 5)
            ti = float(s["trim_in"] if trim_in is None else trim_in)
            to = float(s["trim_out"] if trim_out is None else trim_out)
            xf = float(s["xfade_s"] if xfade_s is None else xfade_s)
            if ti < 0 or to < 0 or xf < 0:
                raise WorkflowError("剪辑时长不能是负数")
            if dur - ti - to < 1.0:
                raise WorkflowError(f"剪完至少要留 1 秒（这一镜 {dur:g} 秒）")
            if xf > 2.0:
                raise WorkflowError("叠化最长 2 秒")
            c.execute("UPDATE shots SET trim_in=?, trim_out=?, xfade_s=?, updated_at=? WHERE id=?",
                      (round(ti, 2), round(to, 2), round(xf, 2), db.now(), sid))
            c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id=?", (s["episode_id"],))
        cut = None
        dub = db.load(s["dub_json"] or "", {}) or {}
        for ln in dub.get("lines", []):     # 剪到配音台词时提醒
            if ln["start"] < ti - 0.05 or ln["end"] > dur - to + 0.05:
                cut = ln["text"]
                break
        return {"shot": sid, "trim_in": round(ti, 2), "trim_out": round(to, 2), "xfade_s": round(xf, 2),
                "kept_seconds": round(dur - ti - to, 2), "cuts_dialogue": cut}

    # ==================================================================
    # 动作过密检查 / 拆卡
    # ==================================================================

    def density(self, pid: str, episode_no: int | None = None) -> dict:
        proj = self.get(pid)
        items = [{"shot": s["id"], "code": s["code"], "problems": s["density"]} for s in proj["shots"]
                 if (episode_no is None or s["episode_no"] == episode_no) and s["density"]]
        return {"project": pid, "items": items}

    async def split_shot(self, sid: str) -> dict:
        """把一张卡按时间轴一分为二：前一半时间段+前一半台词留在原卡，后一半放到新卡（插在后面）。
        两张卡的提示词都要重新生成；原卡的历史视频保留但不再选用。"""
        with db.tx() as c:
            row = c.execute("SELECT s.*, e.no episode_no FROM shots s JOIN episodes e ON e.id=s.episode_id "
                            "WHERE s.id=?", (sid,)).fetchone()
            if not row:
                raise NotFound("镜头不存在")
            s = dict(row)
            if c.execute("SELECT 1 FROM jobs WHERE shot_id=? AND status IN ('submitted','running')", (sid,)).fetchone():
                raise WorkflowError("这个镜头有视频在生成，先取消再拆")
        segs = llm.segments(s["picture"])
        lines = [ln for ln in (s["dialogue"] or "").splitlines() if ln.strip()]
        if len(segs) < 2 and len(lines) < 2:
            raise WorkflowError("这张卡只有一个时间段、一句台词，没法再拆")
        lo, hi = await self._duration_range(self.settings(s["project_id"])["video_model"])
        if len(segs) >= 2:
            k = (len(segs) + 1) // 2
            a, b = segs[:k], segs[k:]
        else:
            a, b = segs, []
        k2 = (len(lines) + 1) // 2 if len(lines) >= 2 else len(lines)
        d1, d2 = "\n".join(lines[:k2]), "\n".join(lines[k2:])

        def retime(part):
            t, out = 0.0, []
            for x0, x1, txt in part:
                out.append(f"【{t:g}-{t + (x1 - x0):g}秒】{txt}")
                t = round(t + (x1 - x0), 2)
            return "".join(out), t

        p1, t1 = retime(a) if a else (s["picture"], float(s["duration_s"]))
        p2, t2 = retime(b) if b else (s["picture"], float(s["duration_s"]))
        dur1 = llm.fit_duration(t1 or s["duration_s"], d1, lo, hi)
        dur2 = llm.fit_duration(t2 or s["duration_s"], d2, lo, hi)
        with db.tx() as c:
            c.execute("UPDATE shots SET picture=?, dialogue=?, duration_s=?, selected_job_id='', status='storyboard_ready',"
                      " dub_path='', dub_json='', rev=rev+1, updated_at=? WHERE id=?", (p1, d1, dur1, db.now(), sid))
            c.execute("DELETE FROM prompts WHERE shot_id=?", (sid,))
            c.execute("UPDATE shots SET shot_no=shot_no+1 WHERE episode_id=? AND shot_no>?",
                      (s["episode_id"], s["shot_no"]))
            nid = new_id("shot")
            c.execute("""INSERT INTO shots(id,project_id,episode_id,scene_id,shot_no,duration_s,scene,size,angle,movement,
                         picture,performance,blocking,dialogue,sfx,transition,original_text,characters_json,scene_asset,
                         status,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (nid, s["project_id"], s["episode_id"], s["scene_id"], s["shot_no"] + 1, dur2, s["scene"],
                       s["size"], s["angle"], s["movement"], p2, s["performance"], s["blocking"], d2, s["sfx"],
                       s["transition"], s["original_text"], s["characters_json"], s["scene_asset"],
                       "storyboard_ready", "manual", db.now()))
            c.execute("UPDATE shots SET transition='硬切' WHERE id=?", (sid,))
            c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id=?", (s["episode_id"],))
        self.refresh_status(s["project_id"])
        code = f"{s['episode_no']}-{s['shot_no']}"
        return {"split": sid, "new": nid, "codes": [code, f"{s['episode_no']}-{s['shot_no'] + 1}"],
                "durations": [dur1, dur2]}
