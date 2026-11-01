"""运维：存储用量、清理旧版本素材、运行状态。

清理只动“可以再生成 / 已经不用”的文件，且默认先预览：
- 视频：每个镜头保留 当前选用的、审片通过的、以及最近 keep_versions 个其他成功版本；其余版本的本地 mp4 和封面帧删掉
  （数据库里的版本记录、提示词、质检结论都保留，只是不能再本地播放）；
- 质检抽帧：对应视频已删的；
- 配音分句：不属于当前配音轨的旧分句 wav（当前的 *_dub.wav 保留）。
成片、导出文件、资产图、素材、封面一律不动。
"""
from __future__ import annotations

import os
import re
import shutil
import time

from . import db, media
from .errors import NotFound


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _tree(folder: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if not os.path.isdir(folder):
        return out
    for sub in os.listdir(folder):
        p = os.path.join(folder, sub)
        if os.path.isdir(p):
            total = 0
            for dp, _, files in os.walk(p):
                total += sum(_size(os.path.join(dp, f)) for f in files)
            out[sub] = total
        else:
            out["(其他)"] = out.get("(其他)", 0) + _size(p)
    return out


class OpsMixin:
    _started_at = time.time()

    def storage(self) -> dict:
        root = media.root()
        with db.tx() as c:
            names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM projects")}
        items = []
        if os.path.isdir(root):
            for pid in sorted(os.listdir(root)):
                folder = os.path.join(root, pid)
                if not os.path.isdir(folder):
                    continue
                parts = _tree(folder)
                items.append({"project": pid, "name": names.get(pid, "（已删除的项目）"), "orphan": pid not in names,
                              "bytes": sum(parts.values()), "parts": parts})
        items.sort(key=lambda x: -x["bytes"])
        try:
            du = shutil.disk_usage(root if os.path.isdir(root) else ".")
            disk = {"total": du.total, "used": du.used, "free": du.free, "used_pct": round(du.used / du.total * 100, 1)}
        except OSError:
            disk = {}
        return {"root": root, "total_bytes": sum(x["bytes"] for x in items), "projects": items, "disk": disk}

    def cleanup(self, pid: str, dry_run: bool = True, keep_versions: int = 1) -> dict:
        keep_versions = max(0, min(10, int(keep_versions)))
        with db.tx() as c:
            if not c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                raise NotFound(f"没有这个项目：{pid}")
            shots = [dict(r) for r in c.execute("SELECT id, selected_job_id, dub_path, dub_json FROM shots WHERE project_id=?",
                                                (pid,))]
            jobs = [dict(r) for r in c.execute(
                "SELECT j.id, j.shot_id, j.version, j.status, j.review, j.local_path, j.poster_path FROM jobs j "
                "JOIN shots s ON s.id=j.shot_id WHERE s.project_id=? ORDER BY j.version DESC", (pid,))]
        by_shot: dict[str, list[dict]] = {}
        for j in jobs:
            by_shot.setdefault(j["shot_id"], []).append(j)
        drop_jobs, files = [], []
        for s in shots:
            others = 0
            cur = s["selected_job_id"] or next((j["id"] for j in by_shot.get(s["id"], [])), "")  # 没选用时界面显示最新版本
            for j in by_shot.get(s["id"], []):
                if j["status"] != "succeeded" or not j["local_path"]:
                    continue
                if j["id"] in (s["selected_job_id"], cur) or j["review"] == "approved":
                    continue
                if others < keep_versions:
                    others += 1
                    continue
                drop_jobs.append(j["id"])
                files += [p for p in (j["local_path"], j["poster_path"]) if p and os.path.exists(p)]
        qc_dir = os.path.dirname(media.path_for(pid, "qc", "x"))
        dropped = set(drop_jobs)
        for f in os.listdir(qc_dir) if os.path.isdir(qc_dir) else []:
            m = re.match(r"(job_[0-9a-f]+)_f\d+\.jpg$", f)
            if m and m.group(1) in dropped:
                files.append(os.path.join(qc_dir, f))
        dub_dir = os.path.dirname(media.path_for(pid, "dub", "x"))
        current_keys = {f"{s['id']}_{(db.load(s['dub_json'] or '', {}) or {}).get('key', '')}" for s in shots}
        for f in os.listdir(dub_dir) if os.path.isdir(dub_dir) else []:
            m = re.match(r"(shot_[0-9a-f]+_[0-9a-f]+)_\d\d\.wav$", f)
            if m and m.group(1) not in current_keys:
                files.append(os.path.join(dub_dir, f))
        files = sorted(set(files))
        total = sum(_size(f) for f in files)
        if not dry_run:
            for f in files:
                try:
                    os.remove(f)
                except OSError:
                    pass
            with db.tx() as c:
                for jid in drop_jobs:
                    c.execute("UPDATE jobs SET local_path='', poster_path='' WHERE id=?", (jid,))
            self.notify(pid, "info", f"已清理旧版本素材 {total / 1048576:.1f} MB",
                        f"{len(drop_jobs)} 个未选用的视频版本、{len(files)} 个文件")
        return {"project": pid, "dry_run": dry_run, "keep_versions": keep_versions, "jobs": len(drop_jobs),
                "files": len(files), "bytes": total, "mb": round(total / 1048576, 2),
                "sample": [os.path.relpath(f, media.root()) for f in files[:20]]}

    def ops_status(self) -> dict:
        with db.tx() as c:
            running = c.execute("SELECT COUNT(*) n FROM jobs WHERE status IN ('submitted','running')").fetchone()["n"]
            projects = c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"]
            tasks = c.execute("SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','running')").fetchone()["n"]
        bdir = os.path.join(os.path.dirname(db.DB_PATH), "backups") if hasattr(db, "DB_PATH") else ""
        backups = sorted(os.listdir(bdir)) if bdir and os.path.isdir(bdir) else []
        st = self.storage()
        return {"uptime_s": round(time.time() - self._started_at), "projects": projects, "running_jobs": running,
                "running_tasks": tasks, "media_bytes": st["total_bytes"], "disk": st["disk"],
                "last_backup": backups[-1] if backups else "", "backups": len(backups)}
