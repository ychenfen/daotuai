"""工作流编排：剧本 → 分集 → 分镜 → 提示词 → 视频任务 → 回执。"""
from __future__ import annotations

import re
import uuid

from . import db, llm
from .agg import Agg

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


class WorkflowError(Exception):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Workflow:
    def __init__(self, agg: Agg, model_text: str = "gpt-6.1-sol", model_video: str = "seedance-2.5",
                 resolution: str = "480P", auto_topup_rmb: float = 0.0):
        self.agg = agg
        self.model_text = model_text
        self.model_video = model_video
        self.resolution = resolution
        self.auto_topup_rmb = auto_topup_rmb

    # ---------------- 项目 ----------------

    def create(self, name: str, style: str = "真人") -> dict:
        pid = _id("proj")
        with db.tx() as c:
            c.execute("INSERT INTO projects(id,name,style,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                      (pid, name, style, "draft", db.now(), db.now()))
        return self.get(pid)

    def get(self, pid: str) -> dict:
        with db.tx() as c:
            p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            if not p:
                raise WorkflowError(f"project not found: {pid}")
            eps = [dict(r) for r in c.execute(
                "SELECT * FROM episodes WHERE project_id=? ORDER BY no", (pid,)).fetchall()]
            shots = [dict(r) for r in c.execute(
                "SELECT s.*, e.no episode_no FROM shots s JOIN episodes e ON e.id=s.episode_id "
                "WHERE s.project_id=? ORDER BY e.no, s.shot_no", (pid,)).fetchall()]
            prompts = {r["shot_id"]: dict(r) for r in c.execute(
                "SELECT p.* FROM prompts p JOIN shots s ON s.id=p.shot_id WHERE s.project_id=?", (pid,)).fetchall()}
            jobs = {r["shot_id"]: dict(r) for r in c.execute(
                "SELECT j.* FROM jobs j JOIN shots s ON s.id=j.shot_id WHERE s.project_id=? ORDER BY j.created_at",
                (pid,)).fetchall()}
        for s in shots:
            s["prompt"] = prompts.get(s["id"])
            s["job"] = jobs.get(s["id"])
        return {**dict(p), "episodes": eps, "shots": shots}

    def list(self) -> list[dict]:
        with db.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM episodes e WHERE e.project_id=p.id) episodes,"
                " (SELECT COUNT(*) FROM shots s WHERE s.project_id=p.id) shots"
                " FROM projects p ORDER BY created_at DESC").fetchall()]
        return rows

    def set_script(self, pid: str, text: str) -> dict:
        if not text.strip():
            raise WorkflowError("script is empty")
        with db.tx() as c:
            c.execute("UPDATE projects SET script_text=?, status='script_ready', updated_at=? WHERE id=?",
                      (text, db.now(), pid))
        return self.get(pid)

    # ---------------- 分集 ----------------

    def parse(self, pid: str) -> dict:
        proj = self.get(pid)
        text = proj["script_text"]
        if not text.strip():
            raise WorkflowError("script is empty")
        parts = self._split_episodes(text)
        with db.tx() as c:
            c.execute("DELETE FROM episodes WHERE project_id=?", (pid,))
            for no, (title, body) in enumerate(parts, start=1):
                c.execute("INSERT INTO episodes(id,project_id,no,title,raw_text) VALUES(?,?,?,?,?)",
                          (_id("ep"), pid, no, title, body))
            c.execute("UPDATE projects SET status='parsed', updated_at=? WHERE id=?", (db.now(), pid))
        return self.get(pid)

    @staticmethod
    def _split_episodes(text: str) -> list[tuple[str, str]]:
        pattern = re.compile(r"(?m)^\s*(?:第\s*([0-9一二三四五六七八九十]+)\s*集|EP\s*(\d+)\b)[^\n]*$")
        marks = list(pattern.finditer(text))
        if not marks:
            return [("第1集", text.strip())]
        out = []
        for i, m in enumerate(marks):
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            title = m.group(0).strip()
            out.append((title, text[start:end].strip()))
        return out

    # ---------------- 分镜 ----------------

    async def storyboard(self, pid: str, episode_no: int | None = None, force: bool = False,
                         max_shots: int = 8) -> dict:
        proj = self.get(pid)
        if not proj["episodes"]:
            proj = self.parse(pid)
        targets = [e for e in proj["episodes"] if episode_no is None or e["no"] == episode_no]
        if not targets:
            raise WorkflowError(f"episode {episode_no} not found")

        results = []
        for ep in targets:
            with db.tx() as c:
                existing = c.execute("SELECT COUNT(*) n FROM shots WHERE episode_id=?", (ep["id"],)).fetchone()["n"]
            if existing and not force:
                results.append({"episode": ep["no"], "skipped": True, "shots": existing, "source": "cached"})
                continue

            shots, source = await llm.storyboard(self.agg, ep["raw_text"], proj["style"],
                                                 self.model_text, max_shots)
            if not shots:
                raise WorkflowError("storyboard produced no shots")
            with db.tx() as c:
                c.execute("DELETE FROM shots WHERE episode_id=?", (ep["id"],))
                for s in shots:
                    c.execute("""INSERT INTO shots(id,project_id,episode_id,shot_no,duration_s,scene,size,angle,
                                 movement,picture,dialogue,sfx,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (_id("shot"), pid, ep["id"], s["shot_no"], s["duration"], s["scene"], s["size"],
                               s["angle"], s["movement"], s["picture"], s["dialogue"], s["sfx"], "storyboard_ready"))
            results.append({"episode": ep["no"], "shots": len(shots), "source": source})

        self._refresh_status(pid)
        return {"project": pid, "results": results, "state": self.get(pid)}

    # ---------------- 提示词 ----------------

    async def prompts(self, pid: str, force: bool = False) -> dict:
        proj = self.get(pid)
        shots = [s for s in proj["shots"] if force or not s.get("prompt")]
        if not shots:
            return {"project": pid, "generated": 0, "source": "cached", "state": proj}
        data, source = await llm.prompts(self.agg, shots, proj["style"], self.model_text)
        with db.tx() as c:
            for shot, item in zip(shots, data):
                c.execute("INSERT OR REPLACE INTO prompts(id,shot_id,positive,negative,model,created_at) "
                          "VALUES(?,?,?,?,?,?)",
                          (_id("prompt"), shot["id"], item["positive"], item["negative"], self.model_text, db.now()))
                c.execute("UPDATE shots SET status='prompt_ready' WHERE id=? AND status='storyboard_ready'", (shot["id"],))
        self._refresh_status(pid)
        return {"project": pid, "generated": len(data), "source": source, "state": self.get(pid)}

    # ---------------- 视频 ----------------

    async def submit_videos(self, pid: str, force: bool = False, resolution: str | None = None) -> dict:
        proj = self.get(pid)
        res = resolution or self.resolution
        submitted, skipped = [], []
        for s in proj["shots"]:
            if not s.get("prompt") or not s["prompt"].get("positive"):
                skipped.append({"shot": s["shot_no"], "reason": "no prompt"})
                continue
            job = s.get("job")
            if job and job["status"] in ("submitted", "running") and not force:
                skipped.append({"shot": s["shot_no"], "reason": "already running"})
                continue
            duration = max(4.0, min(15.0, float(s["duration_s"] or 5)))
            try:
                r = await self._submit_one(s["prompt"]["positive"], res, duration)
            except Exception as e:  # 单镜失败不影响整批
                skipped.append({"shot": s["shot_no"], "reason": str(e)[:120]})
                continue
            with db.tx() as c:
                c.execute("""INSERT INTO jobs(id,shot_id,agg_task_id,model,resolution,duration_s,status,
                             created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                          (_id("job"), s["id"], r["id"], self.model_video, res, duration, "submitted",
                           db.now(), db.now()))
                c.execute("UPDATE shots SET status='video_submitted' WHERE id=?", (s["id"],))
            submitted.append({"shot": s["shot_no"], "task": r["id"],
                              "quoted_rmb": r.get("billing", {}).get("quoted_rmb")})
        self._refresh_status(pid)
        return {"project": pid, "submitted": submitted, "skipped": skipped}

    async def poll_videos(self, pid: str) -> dict:
        proj = self.get(pid)
        done, failed, running = [], [], []
        for s in proj["shots"]:
            job = s.get("job")
            if not job or job["status"] in ("succeeded", "failed", "cancelled"):
                continue
            try:
                r = await self.agg.poll_video(job["agg_task_id"])
            except Exception as e:
                with db.tx() as c:
                    c.execute("UPDATE jobs SET error=?, updated_at=? WHERE id=?", (str(e)[:200], db.now(), job["id"]))
                continue
            status = r.get("status")
            if status == "succeeded":
                charged = (r.get("billing") or {}).get("charged_rmb") or 0
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='succeeded', url=?, charged_rmb=?, updated_at=? WHERE id=?",
                              (r.get("url", ""), charged, db.now(), job["id"]))
                    c.execute("UPDATE shots SET status='video_done' WHERE id=?", (s["id"],))
                done.append({"shot": s["shot_no"], "url": r.get("url"), "charged_rmb": charged})
            elif status == "failed":
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                              ("upstream failed", db.now(), job["id"]))
                    c.execute("UPDATE shots SET status='video_failed' WHERE id=?", (s["id"],))
                failed.append({"shot": s["shot_no"]})
            else:
                running.append({"shot": s["shot_no"]})
        self._refresh_status(pid)
        return {"project": pid, "done": done, "failed": failed, "running": running}

    # ---------------- 一键跑通 ----------------

    async def run_all(self, pid: str, max_shots: int = 8, resolution: str | None = None,
                      force: bool = False) -> dict:
        proj = self.get(pid)
        if not proj["episodes"]:
            self.parse(pid)
        sb = await self.storyboard(pid, force=force, max_shots=max_shots)
        pr = await self.prompts(pid, force=force)
        vs = await self.submit_videos(pid, force=force, resolution=resolution)
        return {"storyboard": sb["results"], "prompts": pr["generated"], "videos": vs,
                "state": self.get(pid)}

    # ---------------- 内部 ----------------

    async def _submit_one(self, prompt: str, resolution: str, duration: float) -> dict:
        """提交单个镜头；余额不足时按 WORKFLOW_AUTO_TOPUP 自动充值后重试一次（内部自用）。"""
        from .agg import AggError
        try:
            return await self.agg.submit_video(self.model_video, prompt, resolution, duration)
        except AggError as e:
            if "402" not in str(e) or self.auto_topup_rmb <= 0:
                raise
            await self.agg.topup(self.auto_topup_rmb)
            return await self.agg.submit_video(self.model_video, prompt, resolution, duration)

    def _refresh_status(self, pid: str) -> None:
        with db.tx() as c:
            shots = c.execute("SELECT status FROM shots WHERE project_id=?", (pid,)).fetchall()
            if not shots:
                return
            statuses = {r["status"] for r in shots}
            if statuses <= {"video_done", "video_failed"}:
                status = "videos_done"
            elif any(r["status"] in ("video_done", "video_submitted") for r in shots):
                status = "videos_running"
            elif statuses <= {"prompt_ready", "video_submitted", "video_done", "video_failed"}:
                status = "prompts_done"
            elif statuses <= {"storyboard_ready", "prompt_ready", "video_submitted", "video_done", "video_failed"}:
                status = "storyboard_done"
            else:
                status = "parsed"
            c.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?", (status, db.now(), pid))
