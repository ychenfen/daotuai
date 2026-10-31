"""工作流编排：剧本 → 分集 → 资产(参考图) → 分镜(站位) → 分镜图 → 提示词(@绑定) → 视频(多版本) → 成片。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import uuid

from . import db, llm, media, refs
from .agg import Agg, AggError

log = logging.getLogger("workflow.pipeline")

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
ACTIVE_JOB = ("submitted", "running")
ASSET_KINDS = ("character", "scene", "prop")

DEFAULT_SETTINGS = {
    "aspect_ratio": "9:16",
    "text_model": os.environ.get("WORKFLOW_TEXT_MODEL", "gpt-6.1-sol"),
    "image_model": os.environ.get("WORKFLOW_IMAGE_MODEL", "gpt-image-2.5"),
    "video_model": os.environ.get("WORKFLOW_VIDEO_MODEL", "seedance-2.5"),
    "resolution": os.environ.get("WORKFLOW_RESOLUTION", "480P"),
    "video_mode": "auto",            # auto | text | first_frame | reference
    "max_shots": 8,
    "budget_rmb": float(os.environ.get("WORKFLOW_BUDGET_RMB", "50")),
    "gen_asset_images": True,
    "gen_frames": False,
    "auto_assemble": True,
    "burn_subtitles": False,
}
SETTING_TYPES = {k: type(v) for k, v in DEFAULT_SETTINGS.items()}

EDITABLE = {
    "shot": {"duration_s", "scene", "size", "angle", "movement", "picture", "blocking", "dialogue", "sfx",
             "characters", "scene_asset"},
    "prompt": {"positive", "negative"},
    "asset": {"name", "aliases", "description"},
}
TABLE = {"shot": "shots", "prompt": "prompts", "asset": "assets"}
JSON_FIELDS = {"characters": "characters_json", "aliases": "aliases_json"}


class WorkflowError(Exception):
    status = 400


class NotFound(WorkflowError):
    status = 404


class Conflict(WorkflowError):
    status = 409

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


class BudgetExceeded(WorkflowError):
    status = 402

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


class Workflow:
    def __init__(self, agg: Agg, auto_topup_rmb: float = 0.0):
        self.agg = agg
        self.auto_topup_rmb = auto_topup_rmb
        self._models: dict[str, dict] | None = None
        self._poll_lock: asyncio.Lock | None = None

    # ==================================================================
    # 基础：项目 / 设置 / 状态
    # ==================================================================

    def create(self, name: str, style: str = "真人", settings: dict | None = None) -> dict:
        if not (name or "").strip():
            raise WorkflowError("项目名不能为空")
        pid = _id("proj")
        merged = self._clean_settings({**DEFAULT_SETTINGS, **(settings or {})})
        with db.tx() as c:
            c.execute("INSERT INTO projects(id,name,style,status,settings_json,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?)", (pid, name.strip(), style or "真人", "draft", db.dump(merged),
                                                db.now(), db.now()))
        return self.get(pid)

    def list(self) -> list[dict]:
        with db.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT p.id, p.name, p.style, p.status, p.created_at, p.updated_at,"
                " (SELECT COUNT(*) FROM episodes e WHERE e.project_id=p.id) episodes,"
                " (SELECT COUNT(*) FROM shots s WHERE s.project_id=p.id) shots,"
                " (SELECT COUNT(*) FROM assets a WHERE a.project_id=p.id) assets"
                " FROM projects p ORDER BY p.created_at DESC").fetchall()]
        return rows

    def delete(self, pid: str) -> dict:
        self._project_row(pid)
        with db.tx() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))
            c.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
            c.execute("DELETE FROM changes WHERE project_id=?", (pid,))
        return {"deleted": pid}

    def settings(self, pid: str) -> dict:
        p = self._project_row(pid)
        return {**DEFAULT_SETTINGS, **db.load(p["settings_json"], {})}

    def update_settings(self, pid: str, patch: dict) -> dict:
        cur = self.settings(pid)
        unknown = set(patch) - set(DEFAULT_SETTINGS) - {"style", "name"}
        if unknown:
            raise WorkflowError(f"未知设置项：{', '.join(sorted(unknown))}")
        with db.tx() as c:
            if "name" in patch and str(patch["name"]).strip():
                c.execute("UPDATE projects SET name=? WHERE id=?", (str(patch["name"]).strip(), pid))
            if "style" in patch and str(patch["style"]).strip():
                c.execute("UPDATE projects SET style=? WHERE id=?", (str(patch["style"]).strip(), pid))
            merged = self._clean_settings({**cur, **{k: v for k, v in patch.items() if k in DEFAULT_SETTINGS}})
            c.execute("UPDATE projects SET settings_json=?, updated_at=? WHERE id=?",
                      (db.dump(merged), db.now(), pid))
        return self.get(pid)

    @staticmethod
    def _clean_settings(s: dict) -> dict:
        out = {}
        for k, default in DEFAULT_SETTINGS.items():
            v = s.get(k, default)
            t = SETTING_TYPES[k]
            try:
                if t is bool:
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                else:
                    v = t(v)
            except (TypeError, ValueError):
                raise WorkflowError(f"设置项 {k} 类型不对：{v!r}")
            out[k] = v
        if out["video_mode"] not in ("auto", "text", "first_frame", "reference"):
            raise WorkflowError("video_mode 只能是 auto/text/first_frame/reference")
        out["max_shots"] = max(1, min(40, out["max_shots"]))
        out["budget_rmb"] = max(0.0, out["budget_rmb"])
        out["resolution"] = out["resolution"].upper()
        return out

    def _project_row(self, pid: str):
        with db.tx() as c:
            p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            raise NotFound(f"项目不存在：{pid}")
        return p

    def get(self, pid: str) -> dict:
        p = dict(self._project_row(pid))
        settings = {**DEFAULT_SETTINGS, **db.load(p.pop("settings_json"), {})}
        with db.tx() as c:
            eps = [dict(r) for r in c.execute(
                "SELECT * FROM episodes WHERE project_id=? ORDER BY no", (pid,)).fetchall()]
            assets = self._assets(c, pid)
            shots = [dict(r) for r in c.execute(
                "SELECT s.*, e.no episode_no FROM shots s JOIN episodes e ON e.id=s.episode_id "
                "WHERE s.project_id=? ORDER BY e.no, s.shot_no", (pid,)).fetchall()]
            prompts = {r["shot_id"]: dict(r) for r in c.execute(
                "SELECT p.* FROM prompts p JOIN shots s ON s.id=p.shot_id WHERE s.project_id=?", (pid,)).fetchall()}
            jobs: dict[str, list[dict]] = {}
            for r in c.execute("SELECT j.* FROM jobs j JOIN shots s ON s.id=j.shot_id WHERE s.project_id=? "
                               "ORDER BY j.version", (pid,)).fetchall():
                jobs.setdefault(r["shot_id"], []).append(self._job_view(dict(r)))
            task = c.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                             (pid,)).fetchone()
        by_id = {a["id"]: a for a in assets}
        for e in eps:
            e["final_url"] = media.url_of(e["final_path"])
            e["srt_url"] = media.url_of(e["srt_path"])
        for s in shots:
            s["characters"] = db.load(s.pop("characters_json"), [])
            s["frame"] = media.url_of(s["frame_path"]) or s["frame_url"]
            pr = prompts.get(s["id"])
            if pr:
                pr["bindings"] = db.load(pr.pop("bindings_json"), [])
                pr["unresolved"] = db.load(pr.pop("unresolved_json"), [])
                pr["stale"] = refs.stale(pr["bindings"], by_id)
            s["prompt"] = pr
            s["jobs"] = jobs.get(s["id"], [])
            s["job"] = next((j for j in s["jobs"] if j["id"] == s["selected_job_id"]), None) \
                or (s["jobs"][-1] if s["jobs"] else None)
        out = {**p, "settings": settings, "episodes": eps, "assets": assets, "shots": shots,
               "spend": self._spend(pid), "task": self._task_view(dict(task)) if task else None}
        return out

    @staticmethod
    def _job_view(j: dict) -> dict:
        j["refs"] = db.load(j.pop("refs_json"), [])
        j["play_url"] = media.url_of(j["local_path"]) or ("" if media.is_mock_url(j["url"]) else j["url"])
        return j

    @staticmethod
    def _task_view(t: dict) -> dict:
        t["steps"] = db.load(t.pop("steps_json"), [])
        t["result"] = db.load(t.pop("result_json"), {})
        return t

    def _assets(self, c, pid: str) -> list[dict]:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM assets WHERE project_id=? ORDER BY CASE kind WHEN 'character' THEN 0 "
            "WHEN 'scene' THEN 1 ELSE 2 END, created_at", (pid,)).fetchall()]
        if not rows:
            return []
        vers: dict[str, list[dict]] = {}
        q = ",".join("?" * len(rows))
        for v in c.execute(f"SELECT * FROM asset_versions WHERE asset_id IN ({q}) ORDER BY version",
                           [r["id"] for r in rows]).fetchall():
            v = dict(v)
            v["display_url"] = media.url_of(v["local_path"]) or ("" if media.is_mock_url(v["image_url"])
                                                                 else v["image_url"])
            vers.setdefault(v["asset_id"], []).append(v)
        for a in rows:
            a["aliases"] = db.load(a.pop("aliases_json"), [])
            a["versions"] = vers.get(a["id"], [])
            cur = next((v for v in a["versions"] if v["version"] == a["current_version"]), None)
            a["image_url"] = cur["image_url"] if cur else ""
            a["local_path"] = cur["local_path"] if cur else ""
            a["display_url"] = cur["display_url"] if cur else ""
        return rows

    def _spend(self, pid: str) -> dict:
        with db.tx() as c:
            jobs = c.execute(
                "SELECT COALESCE(SUM(CASE WHEN j.status='succeeded' THEN j.charged_rmb "
                " WHEN j.status IN ('submitted','running') THEN j.quoted_rmb ELSE 0 END),0) v "
                "FROM jobs j JOIN shots s ON s.id=j.shot_id WHERE s.project_id=?", (pid,)).fetchone()["v"]
            imgs = c.execute(
                "SELECT COALESCE(SUM(v.charged_rmb),0) v FROM asset_versions v JOIN assets a ON a.id=v.asset_id "
                "WHERE a.project_id=?", (pid,)).fetchone()["v"]
            frames = c.execute("SELECT COALESCE(SUM(frame_charged_rmb),0) v FROM shots WHERE project_id=?",
                               (pid,)).fetchone()["v"]
        budget = float(self.settings(pid)["budget_rmb"])
        total = round(float(jobs) + float(imgs) + float(frames), 4)
        return {"videos_rmb": round(float(jobs), 4), "images_rmb": round(float(imgs) + float(frames), 4),
                "total_rmb": total, "budget_rmb": budget, "remaining_rmb": round(budget - total, 4)}

    def refresh_status(self, pid: str) -> str:
        with db.tx() as c:
            shots = [r["status"] for r in c.execute("SELECT status FROM shots WHERE project_id=?", (pid,))]
            eps = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN final_path!='' THEN 1 ELSE 0 END) f "
                            "FROM episodes WHERE project_id=?", (pid,)).fetchone()
            n_assets = c.execute("SELECT COUNT(*) n FROM assets WHERE project_id=?", (pid,)).fetchone()["n"]
            p = c.execute("SELECT status, script_text FROM projects WHERE id=?", (pid,)).fetchone()
            if not p:
                return ""
            if eps["n"] and eps["f"] == eps["n"]:
                status = "final_ready"
            elif shots and all(s in ("video_done", "video_failed") for s in shots):
                status = "videos_done"
            elif any(s in ("video_submitted", "video_done", "video_failed") for s in shots):
                status = "videos_running"
            elif shots and all(s == "prompt_ready" for s in shots):
                status = "prompts_done"
            elif shots:
                status = "storyboard_done"
            elif n_assets:
                status = "assets_ready"
            elif eps["n"]:
                status = "parsed"
            elif p["script_text"].strip():
                status = "script_ready"
            else:
                status = "draft"
            c.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?", (status, db.now(), pid))
        return status

    # ==================================================================
    # 剧本 / 分集
    # ==================================================================

    def set_script(self, pid: str, text: str) -> dict:
        self._project_row(pid)
        if not (text or "").strip():
            raise WorkflowError("剧本是空的")
        with db.tx() as c:
            c.execute("UPDATE projects SET script_text=?, updated_at=? WHERE id=?", (text, db.now(), pid))
        self.refresh_status(pid)
        return self.get(pid)

    def parse(self, pid: str, force: bool = False) -> dict:
        p = self._project_row(pid)
        if not p["script_text"].strip():
            raise WorkflowError("剧本是空的")
        with db.tx() as c:
            n = c.execute("SELECT COUNT(*) n FROM shots WHERE project_id=?", (pid,)).fetchone()["n"]
        if n and not force:
            raise WorkflowError("已经有分镜了；重新分集会清空分镜和视频，确认请加 force=true")
        parts = self.split_episodes(p["script_text"])
        with db.tx() as c:
            c.execute("DELETE FROM episodes WHERE project_id=?", (pid,))
            for no, (title, body) in enumerate(parts, start=1):
                c.execute("INSERT INTO episodes(id,project_id,no,title,raw_text) VALUES(?,?,?,?,?)",
                          (_id("ep"), pid, no, title, body))
        self.refresh_status(pid)
        return self.get(pid)

    @staticmethod
    def split_episodes(text: str) -> list[tuple[str, str]]:
        pattern = re.compile(r"(?m)^\s*(?:第\s*([0-9一二三四五六七八九十百]+)\s*集|EP\s*(\d+)\b)[^\n]*$", re.I)
        marks = list(pattern.finditer(text))
        if not marks:
            return [("第1集", text.strip())]
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end():end].strip()
            if body:
                out.append((m.group(0).strip(), body))
        return out or [("第1集", text.strip())]

    def _ensure_parsed(self, pid: str) -> None:
        with db.tx() as c:
            n = c.execute("SELECT COUNT(*) n FROM episodes WHERE project_id=?", (pid,)).fetchone()["n"]
        if not n:
            self.parse(pid)

    # ==================================================================
    # 资产库
    # ==================================================================

    async def extract_assets(self, pid: str) -> dict:
        p = self._project_row(pid)
        if not p["script_text"].strip():
            raise WorkflowError("剧本是空的")
        st = self.settings(pid)
        data, source = await llm.extract_assets(self.agg, p["script_text"], p["style"], st["text_model"])
        added = []
        with db.tx() as c:
            existing = {(r["kind"], r["name"]) for r in c.execute(
                "SELECT kind, name FROM assets WHERE project_id=?", (pid,))}
            for key, kind in (("characters", "character"), ("scenes", "scene"), ("props", "prop")):
                for a in data.get(key) or []:
                    if (kind, a["name"]) in existing:
                        continue
                    aid = _id("asset")
                    c.execute("INSERT INTO assets(id,project_id,kind,name,aliases_json,description,"
                              "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                              (aid, pid, kind, a["name"], db.dump(a.get("aliases") or []),
                               a.get("description") or "", db.now(), db.now()))
                    added.append({"id": aid, "kind": kind, "name": a["name"]})
        self.refresh_status(pid)
        return {"source": source, "added": added, "state": self.get(pid)}

    def add_asset(self, pid: str, kind: str, name: str, description: str = "",
                  aliases: list[str] | None = None) -> dict:
        self._project_row(pid)
        if kind not in ASSET_KINDS:
            raise WorkflowError(f"资产类型只能是 {'/'.join(ASSET_KINDS)}")
        if not (name or "").strip():
            raise WorkflowError("资产名不能为空")
        aid = _id("asset")
        with db.tx() as c:
            if c.execute("SELECT 1 FROM assets WHERE project_id=? AND kind=? AND name=?",
                         (pid, kind, name.strip())).fetchone():
                raise WorkflowError(f"已有同名资产：{name}")
            c.execute("INSERT INTO assets(id,project_id,kind,name,aliases_json,description,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?)", (aid, pid, kind, name.strip(), db.dump(aliases or []),
                                                   description, db.now(), db.now()))
        self.refresh_status(pid)
        return self.asset(aid)

    def asset(self, aid: str) -> dict:
        with db.tx() as c:
            row = c.execute("SELECT project_id FROM assets WHERE id=?", (aid,)).fetchone()
            if not row:
                raise NotFound(f"资产不存在：{aid}")
            return next(a for a in self._assets(c, row["project_id"]) if a["id"] == aid)

    def delete_asset(self, aid: str) -> dict:
        a = self.asset(aid)
        with db.tx() as c:
            c.execute("DELETE FROM assets WHERE id=?", (aid,))
        return {"deleted": aid, "name": a["name"]}

    async def gen_asset_image(self, aid: str) -> dict:
        a = self.asset(aid)
        p = self._project_row(a["project_id"])
        st = self.settings(a["project_id"])
        prompt = llm.asset_image_prompt(a["kind"], a["name"], a["description"], p["style"])
        version = (max((v["version"] for v in a["versions"]), default=0)) + 1
        size = "1024x1792" if a["kind"] == "character" else "1792x1024"
        res = await self._with_topup(lambda: self.agg.image(st["image_model"], prompt, size=size,
                                                            idem=f"asset:{aid}:v{version}"))
        url = (res.get("data") or [{}])[0].get("url") or ""
        local, source = await self._store_image(a["project_id"], "assets", f"{aid}_v{version}", url,
                                                seed=f"{a['name']}{version}")
        charged = float((res.get("billing") or {}).get("charged_rmb") or 0)
        with db.tx() as c:
            c.execute("INSERT INTO asset_versions(id,asset_id,version,prompt,image_url,local_path,source,"
                      "charged_rmb,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (_id("av"), aid, version, prompt, "" if url.startswith("data:") else url, local, source,
                       charged, db.now()))
            c.execute("UPDATE assets SET current_version=?, updated_at=? WHERE id=?", (version, db.now(), aid))
        return self.asset(aid)

    async def gen_asset_images(self, pid: str, only_missing: bool = True) -> dict:
        with db.tx() as c:
            todo = [r["id"] for r in c.execute(
                "SELECT id FROM assets WHERE project_id=?" + (" AND current_version=0" if only_missing else ""),
                (pid,)).fetchall()]
        done, failed = [], []
        for aid in todo:
            try:
                a = await self.gen_asset_image(aid)
                done.append({"id": aid, "name": a["name"], "version": a["current_version"]})
            except (AggError, media.MediaError, WorkflowError) as e:
                failed.append({"id": aid, "error": str(e)[:200]})
        return {"generated": done, "failed": failed}

    def upload_asset_image(self, aid: str, data: bytes, filename: str = "upload.png") -> dict:
        a = self.asset(aid)
        if not data:
            raise WorkflowError("上传的文件是空的")
        ext = os.path.splitext(filename)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            raise WorkflowError("只支持 png/jpg/webp")
        version = (max((v["version"] for v in a["versions"]), default=0)) + 1
        path = media.path_for(a["project_id"], "assets", f"{aid}_v{version}{ext}")
        with open(path, "wb") as f:
            f.write(data)
        with db.tx() as c:
            c.execute("INSERT INTO asset_versions(id,asset_id,version,prompt,image_url,local_path,source,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?)", (_id("av"), aid, version, "", "", path, "uploaded", db.now()))
            c.execute("UPDATE assets SET current_version=?, updated_at=? WHERE id=?", (version, db.now(), aid))
        return self.asset(aid)

    def select_asset_version(self, aid: str, version: int) -> dict:
        a = self.asset(aid)
        if not any(v["version"] == version for v in a["versions"]):
            raise WorkflowError(f"没有这个版本：v{version}")
        with db.tx() as c:
            c.execute("UPDATE assets SET current_version=?, updated_at=? WHERE id=?", (version, db.now(), aid))
        return self.asset(aid)

    async def _store_image(self, pid: str, sub: str, stem: str, url: str, seed: str) -> tuple[str, str]:
        """把上游图片落到本地。返回 (local_path, source)。mock URL 生成占位图。"""
        if not url or media.is_mock_url(url):
            path = media.path_for(pid, sub, stem + ".png")
            media.placeholder_png(path, seed)
            return path, "mock"
        if url.startswith("data:"):
            import base64
            head, _, b64 = url.partition(",")
            ext = ".jpg" if "jpeg" in head else ".png"
            path = media.path_for(pid, sub, stem + ext)
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            return path, "generated"
        try:
            data = await self.agg.fetch(url)
        except Exception as e:  # 下载失败不致命：保留上游 URL
            log.warning("download image failed %s: %s", url[:80], e)
            return "", "generated"
        ext = ".jpg" if url.lower().split("?")[0].endswith((".jpg", ".jpeg")) else ".png"
        path = media.path_for(pid, sub, stem + ext)
        with open(path, "wb") as f:
            f.write(data)
        return path, "generated"

    # ==================================================================
    # 分镜（含站位）
    # ==================================================================

    async def storyboard(self, pid: str, episode_no: int | None = None, force: bool = False,
                         max_shots: int | None = None) -> dict:
        self._ensure_parsed(pid)
        proj = self.get(pid)
        st = proj["settings"]
        max_shots = max_shots or st["max_shots"]
        targets = [e for e in proj["episodes"] if episode_no is None or e["no"] == episode_no]
        if not targets:
            raise NotFound(f"第 {episode_no} 集不存在")
        char_names = [a["name"] for a in proj["assets"] if a["kind"] == "character"]
        scene_names = [a["name"] for a in proj["assets"] if a["kind"] == "scene"]
        results = []
        for ep in targets:
            with db.tx() as c:
                existing = c.execute("SELECT COUNT(*) n FROM shots WHERE episode_id=?", (ep["id"],)).fetchone()["n"]
            if existing and not force:
                results.append({"episode": ep["no"], "skipped": True, "shots": existing, "source": "cached"})
                continue
            shots, source = await llm.storyboard(self.agg, ep["raw_text"], proj["style"], st["text_model"],
                                                 max_shots, char_names, scene_names)
            if not shots:
                raise WorkflowError(f"第 {ep['no']} 集没拆出镜头")
            with db.tx() as c:
                c.execute("DELETE FROM shots WHERE episode_id=?", (ep["id"],))
                for s in shots:
                    scene_asset = s["scene"] if s["scene"] in scene_names else ""
                    c.execute("""INSERT INTO shots(id,project_id,episode_id,shot_no,duration_s,scene,size,angle,
                                 movement,picture,blocking,dialogue,sfx,characters_json,scene_asset,status,updated_at)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (_id("shot"), pid, ep["id"], s["shot_no"], s["duration"], s["scene"], s["size"],
                               s["angle"], s["movement"], s["picture"], s["blocking"], s["dialogue"], s["sfx"],
                               db.dump(s["characters"]), scene_asset, "storyboard_ready", db.now()))
                c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id=?", (ep["id"],))
            results.append({"episode": ep["no"], "shots": len(shots), "source": source})
        self.refresh_status(pid)
        return {"project": pid, "results": results, "state": self.get(pid)}

    # ==================================================================
    # 分镜图（站位图）
    # ==================================================================

    def _ref_url(self, local_path: str, url: str) -> str:
        """给上游的参考图：mock URL 原样；本地文件转 data URI（上游临时链接会过期）；否则用 URL。"""
        if url and media.is_mock_url(url):
            return url
        if local_path and os.path.exists(local_path):
            return media.data_uri(local_path)
        return url

    async def gen_frames(self, pid: str, shot_ids: list[str] | None = None, only_missing: bool = True) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        assets = {a["name"]: a for a in proj["assets"]}
        done, failed = [], []
        for s in proj["shots"]:
            if shot_ids and s["id"] not in shot_ids:
                continue
            if only_missing and s["frame"] and not shot_ids:
                continue
            ref_urls = []
            for name in (s["characters"] or [])[:3] + ([s["scene_asset"]] if s["scene_asset"] else []):
                a = assets.get(name)
                if a and (a["local_path"] or a["image_url"]):
                    ref_urls.append(self._ref_url(a["local_path"], a["image_url"]))
            prompt = llm.frame_image_prompt(s, proj["style"], st["aspect_ratio"])
            size = "1024x1792" if st["aspect_ratio"] == "9:16" else "1792x1024"
            try:
                res = await self._with_topup(lambda: self.agg.image(
                    st["image_model"], prompt, size=size, refs=ref_urls or None,
                    idem=f"frame:{s['id']}:{_h(prompt, len(ref_urls))}"))
                url = (res.get("data") or [{}])[0].get("url") or ""
                local, _ = await self._store_image(pid, "frames", f"{s['id']}_{_h(prompt)[:6]}", url,
                                                   seed=s["id"])
                charged = float((res.get("billing") or {}).get("charged_rmb") or 0)
                with db.tx() as c:
                    c.execute("UPDATE shots SET frame_url=?, frame_path=?, "
                              "frame_charged_rmb=frame_charged_rmb+? WHERE id=?",
                              ("" if url.startswith("data:") else url, local, charged, s["id"]))
                done.append({"shot": s["id"], "episode": s["episode_no"], "shot_no": s["shot_no"]})
            except (AggError, media.MediaError) as e:
                failed.append({"shot": s["id"], "error": str(e)[:200]})
        return {"generated": done, "failed": failed}

    # ==================================================================
    # 提示词（@绑定）
    # ==================================================================

    def _asset_index(self, pid: str) -> list[dict]:
        with db.tx() as c:
            return self._assets(c, pid)

    async def prompts(self, pid: str, force: bool = False, shot_ids: list[str] | None = None) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        shots = [s for s in proj["shots"] if (not shot_ids or s["id"] in shot_ids)
                 and (force or shot_ids or not s.get("prompt"))]
        if not shots:
            return {"project": pid, "generated": 0, "source": "cached", "state": proj}
        names = [a["name"] for a in proj["assets"]]
        data, source = await llm.prompts(self.agg, shots, proj["style"], st["text_model"],
                                         st["aspect_ratio"], names)
        assets = proj["assets"]
        with db.tx() as c:
            for shot, item in zip(shots, data):
                bindings, unresolved = refs.resolve(item["positive"], assets)
                old = c.execute("SELECT * FROM prompts WHERE shot_id=?", (shot["id"],)).fetchone()
                if old:
                    rev = old["rev"] + 1
                    c.execute("UPDATE prompts SET positive=?, negative=?, model=?, bindings_json=?, unresolved_json=?,"
                              " rev=?, updated_at=? WHERE id=?",
                              (item["positive"], item["negative"], st["text_model"], db.dump(bindings),
                               db.dump(unresolved), rev, db.now(), old["id"]))
                    self._journal(c, pid, "prompt", old["id"], rev, ["positive", "negative"], "ai")
                else:
                    c.execute("INSERT INTO prompts(id,shot_id,positive,negative,model,bindings_json,unresolved_json,"
                              "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (_id("prompt"), shot["id"], item["positive"], item["negative"], st["text_model"],
                               db.dump(bindings), db.dump(unresolved), db.now(), db.now()))
                c.execute("UPDATE shots SET status='prompt_ready' WHERE id=? AND status='storyboard_ready'",
                          (shot["id"],))
        self.refresh_status(pid)
        return {"project": pid, "generated": len(data), "source": source, "state": self.get(pid)}

    def resolve_references(self, pid: str, shot_ids: list[str] | None = None) -> dict:
        """按资产当前版本重算所有绑定（资产改名/换图后点一次）。系统字段，不推进 rev。"""
        assets = self._asset_index(pid)
        changed = 0
        with db.tx() as c:
            rows = c.execute("SELECT p.* FROM prompts p JOIN shots s ON s.id=p.shot_id WHERE s.project_id=?",
                             (pid,)).fetchall()
            for r in rows:
                if shot_ids and r["shot_id"] not in shot_ids:
                    continue
                b, u = refs.resolve(r["positive"], assets)
                if db.dump(b) != r["bindings_json"] or db.dump(u) != r["unresolved_json"]:
                    changed += 1
                    c.execute("UPDATE prompts SET bindings_json=?, unresolved_json=? WHERE id=?",
                              (db.dump(b), db.dump(u), r["id"]))
        return {"updated": changed, "state": self.get(pid)}

    # ==================================================================
    # 编辑（对象 + 字段级冲突检测）
    # ==================================================================

    @staticmethod
    def _journal(c, pid: str, kind: str, oid: str, rev: int, fields: list[str], actor: str) -> None:
        c.execute("INSERT INTO changes(project_id,object_type,object_id,rev,fields_json,actor,created_at)"
                  " VALUES(?,?,?,?,?,?,?)", (pid, kind, oid, rev, db.dump(sorted(fields)), actor, db.now()))

    def edit(self, kind: str, oid: str, fields: dict, expected_rev: int | None = None) -> dict:
        """只有当"别人改过的字段"和"你这次要改的字段"重叠且值不同，才报冲突；否则自动合并。"""
        if kind not in EDITABLE:
            raise WorkflowError(f"不支持编辑：{kind}")
        bad = set(fields) - EDITABLE[kind]
        if bad:
            raise WorkflowError(f"这些字段不能改：{', '.join(sorted(bad))}")
        table = TABLE[kind]
        with db.tx() as c:
            row = c.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
            if not row:
                raise NotFound(f"{kind} 不存在：{oid}")
            row = dict(row)
            pid = row.get("project_id") or c.execute(
                "SELECT s.project_id FROM shots s WHERE s.id=?", (row.get("shot_id"),)).fetchone()["project_id"]
            current = {}
            for f in fields:
                col = JSON_FIELDS.get(f, f)
                current[f] = db.load(row[col], []) if f in JSON_FIELDS else row[col]
            new = {}
            for f, v in fields.items():
                if f == "duration_s":
                    try:
                        v = max(1.0, min(30.0, float(v)))
                    except (TypeError, ValueError):
                        raise WorkflowError("duration_s 必须是数字")
                elif f in JSON_FIELDS:
                    v = [str(x).strip() for x in (v or []) if str(x).strip()]
                else:
                    v = str(v if v is not None else "")
                if v != current[f]:
                    new[f] = v
            if expected_rev is not None and int(expected_rev) != row["rev"]:
                touched: set[str] = set()
                for ch in c.execute("SELECT fields_json FROM changes WHERE object_id=? AND rev>?",
                                    (oid, int(expected_rev))).fetchall():
                    touched |= set(db.load(ch["fields_json"], []))
                overlap = sorted(f for f in new if f in touched)
                if overlap:
                    raise Conflict(f"「{kind}」的 {'、'.join(overlap)} 已被改过，请核对后再保存",
                                   {"conflict_fields": overlap, "current": {f: current[f] for f in overlap},
                                    "yours": {f: new[f] for f in overlap}, "rev": row["rev"],
                                    "expected_rev": int(expected_rev)})
            if not new:
                return {"changed": [], "rev": row["rev"], "merged": expected_rev is not None
                        and int(expected_rev) != row["rev"]}
            rev = row["rev"] + 1
            sets = ", ".join(f"{JSON_FIELDS.get(f, f)}=?" for f in new)
            vals = [db.dump(v) if f in JSON_FIELDS else v for f, v in new.items()]
            try:
                c.execute(f"UPDATE {table} SET {sets}, rev=?, updated_at=? WHERE id=?",
                          vals + [rev, db.now(), oid])
            except sqlite3.IntegrityError:
                raise WorkflowError("和已有资产重名了")
            self._journal(c, pid, kind, oid, rev, list(new), "user")
            if kind == "prompt" and "positive" in new:
                assets = self._assets(c, pid)
                b, u = refs.resolve(new["positive"], assets)
                c.execute("UPDATE prompts SET bindings_json=?, unresolved_json=? WHERE id=?",
                          (db.dump(b), db.dump(u), oid))
        return {"changed": sorted(new), "rev": rev,
                "merged": expected_rev is not None and int(expected_rev) != row["rev"]}

    # ==================================================================
    # 视频：报价 → 预算 → 提交（多版本、幂等）→ 回执 → 下载
    # ==================================================================

    async def model_info(self, model_id: str) -> dict:
        if self._models is None or model_id not in self._models:
            self._models = {m["id"]: m for m in await self.agg.models()}
        m = self._models.get(model_id)
        if not m:
            raise WorkflowError(f"聚合层没有这个模型：{model_id}")
        return m

    def _clamp_duration(self, seconds: float, m: dict) -> float:
        d = m.get("duration") or {}
        lo, hi = float(d.get("min", 4)), float(d.get("max", 15))
        return float(max(lo, min(hi, round(seconds or 5))))

    def _pick_targets(self, proj: dict, shot_ids: list[str] | None, force: bool) -> tuple[list[dict], list[dict]]:
        targets, skipped = [], []
        for s in proj["shots"]:
            if shot_ids and s["id"] not in shot_ids:
                continue
            if not s.get("prompt") or not s["prompt"].get("positive"):
                skipped.append({"shot": s["id"], "shot_no": s["shot_no"], "reason": "还没有提示词"})
                continue
            active = [j for j in s["jobs"] if j["status"] in ACTIVE_JOB]
            if active and not force:
                skipped.append({"shot": s["id"], "shot_no": s["shot_no"], "reason": "已经在跑"})
                continue
            done = [j for j in s["jobs"] if j["status"] == "succeeded"]
            if done and not force and not shot_ids:
                skipped.append({"shot": s["id"], "shot_no": s["shot_no"], "reason": "已经有成片（重做请单独指定镜头）"})
                continue
            targets.append(s)
        return targets, skipped

    async def quote_videos(self, pid: str, shot_ids: list[str] | None = None, force: bool = False,
                           resolution: str | None = None, model: str | None = None) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        model = model or st["video_model"]
        res = (resolution or st["resolution"]).upper()
        m = await self.model_info(model)
        if m.get("specs") and res not in m["specs"]:
            raise WorkflowError(f"{model} 不支持 {res}，可选：{'/'.join(m['specs'])}")
        targets, skipped = self._pick_targets(proj, shot_ids, force)
        items, total = [], 0.0
        for s in targets:
            dur = self._clamp_duration(s["duration_s"], m)
            q = await self.agg.quote(model, spec=res, seconds=dur)
            items.append({"shot": s["id"], "episode": s["episode_no"], "shot_no": s["shot_no"],
                          "seconds": dur, "rmb": q["rmb"]})
            total += q["rmb"]
        spend = proj["spend"]
        return {"model": model, "resolution": res, "live": bool(m.get("live")), "items": items,
                "skipped": skipped, "total_rmb": round(total, 4), "spent_rmb": spend["total_rmb"],
                "budget_rmb": spend["budget_rmb"], "remaining_rmb": spend["remaining_rmb"],
                "within_budget": total <= spend["remaining_rmb"] + 1e-9}

    def _video_refs(self, s: dict, assets_by_id: dict, mode: str, m: dict) -> tuple[str, list[dict], list[dict]]:
        """决定出片模式与参考图。返回 (mode, images_for_upstream, refs_for_record)。"""
        modes = m.get("modes") or []
        bindings = (s.get("prompt") or {}).get("bindings") or []
        bound = []
        for b in bindings:
            a = assets_by_id.get(b["asset_id"])
            if a and (a["local_path"] or a["image_url"]):
                bound.append(a)
        if mode == "auto":
            if bound and "全能参考" in modes:
                mode = "reference"
            elif s.get("frame") and "首帧参考" in modes:
                mode = "first_frame"
            else:
                mode = "text"
        images, record = [], []
        if mode == "reference":
            for a in bound[:4]:
                images.append({"url": self._ref_url(a["local_path"], a["image_url"]), "role": "reference_image"})
                record.append({"asset_id": a["id"], "name": a["name"], "version": a["current_version"],
                               "role": "reference_image"})
        elif mode == "first_frame":
            if s.get("frame_path") or s.get("frame_url"):
                images.append({"url": self._ref_url(s.get("frame_path", ""), s.get("frame_url", "")),
                               "role": "first_frame"})
                record.append({"frame": True, "role": "first_frame"})
            else:
                mode = "text"
        return mode, images, record

    async def submit_videos(self, pid: str, shot_ids: list[str] | None = None, force: bool = False,
                            resolution: str | None = None, mode: str | None = None,
                            model: str | None = None) -> dict:
        quote = await self.quote_videos(pid, shot_ids, force, resolution, model)
        if quote["items"] and not quote["within_budget"]:
            raise BudgetExceeded(
                f"超出项目预算：本次约 ¥{quote['total_rmb']:.2f}，剩余 ¥{quote['remaining_rmb']:.2f}"
                f"（预算 ¥{quote['budget_rmb']:.2f}）。调高预算或少选几个镜头。", quote)
        self.resolve_references(pid, shot_ids)
        proj = self.get(pid)
        st = proj["settings"]
        model, res = quote["model"], quote["resolution"]
        m = await self.model_info(model)
        mode = mode or st["video_mode"]
        by_id = {a["id"]: a for a in proj["assets"]}
        shots = {s["id"]: s for s in proj["shots"]}
        submitted, skipped = [], list(quote["skipped"])
        for item in quote["items"]:
            s = shots[item["shot"]]
            used_mode, images, record = self._video_refs(s, by_id, mode, m)
            version = (max((j["version"] for j in s["jobs"]), default=0)) + 1
            crid = _h(s["id"], version, s["prompt"]["rev"], model, res)
            prompt = s["prompt"]["positive"]
            if s["prompt"].get("negative"):
                prompt += f"\n避免：{s['prompt']['negative']}"
            try:
                r = await self._with_topup(lambda: self.agg.submit_video(
                    model, prompt, res, item["seconds"], st["aspect_ratio"], images or None, idem=crid))
            except AggError as e:
                skipped.append({"shot": s["id"], "shot_no": s["shot_no"], "reason": str(e)[:200]})
                continue
            jid = _id("job")
            with db.tx() as c:
                c.execute("""INSERT INTO jobs(id,shot_id,version,client_request_id,agg_task_id,model,resolution,
                             duration_s,mode,refs_json,prompt_snapshot,status,quoted_rmb,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (jid, s["id"], version, crid, r["id"], model, res, item["seconds"], used_mode,
                           db.dump(record), prompt, "submitted",
                           float((r.get("billing") or {}).get("quoted_rmb") or item["rmb"]), db.now(), db.now()))
                c.execute("UPDATE shots SET status='video_submitted' WHERE id=?", (s["id"],))
            submitted.append({"shot": s["id"], "episode": s["episode_no"], "shot_no": s["shot_no"],
                              "job": jid, "version": version, "mode": used_mode, "task": r["id"],
                              "quoted_rmb": (r.get("billing") or {}).get("quoted_rmb")})
        self.refresh_status(pid)
        return {"project": pid, "submitted": submitted, "skipped": skipped, "quote": quote}

    async def poll_videos(self, pid: str | None = None) -> dict:
        """推进所有在跑的视频任务（pid=None 时跨项目）。成功即下载到本地，并设为该镜头的选用版本。
        后台轮询器和一键任务可能同时调用，这里串行化，避免同一任务被结算/下载两次。"""
        if self._poll_lock is None:
            self._poll_lock = asyncio.Lock()
        async with self._poll_lock:
            return await self._poll_videos(pid)

    async def _poll_videos(self, pid: str | None) -> dict:
        sql =("SELECT j.*, s.project_id, s.selected_job_id FROM jobs j JOIN shots s ON s.id=j.shot_id "
               "WHERE j.status IN ('submitted','running')")
        args: tuple = ()
        if pid:
            sql += " AND s.project_id=?"
            args = (pid,)
        with db.tx() as c:
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
        done, failed, running = [], [], []
        projects = set()
        for j in rows:
            projects.add(j["project_id"])
            try:
                r = await self.agg.poll_video(j["agg_task_id"])
            except AggError as e:
                with db.tx() as c:
                    c.execute("UPDATE jobs SET error=?, updated_at=? WHERE id=?", (str(e)[:300], db.now(), j["id"]))
                running.append({"job": j["id"], "error": str(e)[:120]})
                continue
            status = r.get("status")
            if status == "succeeded":
                url = r.get("url") or ""
                seconds = float(r.get("seconds") or j["duration_s"])
                local = await self._store_video(j["project_id"], j, url, seconds)
                charged = float((r.get("billing") or {}).get("charged_rmb") or 0)
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='succeeded', url=?, local_path=?, seconds=?, charged_rmb=?,"
                              " error='', updated_at=? WHERE id=?",
                              (url, local, seconds, charged, db.now(), j["id"]))
                    c.execute("UPDATE shots SET status='video_done', selected_job_id=? WHERE id=?",
                              (j["id"], j["shot_id"]))
                done.append({"job": j["id"], "shot": j["shot_id"], "url": url, "local": bool(local),
                             "charged_rmb": charged})
            elif status in ("failed", "cancelled"):
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                              (status, str(r.get("error") or "上游失败，已退回预扣"), db.now(), j["id"]))
                    has_ok = c.execute("SELECT 1 FROM jobs WHERE shot_id=? AND status='succeeded'",
                                       (j["shot_id"],)).fetchone()
                    c.execute("UPDATE shots SET status=? WHERE id=?",
                              ("video_done" if has_ok else "video_failed", j["shot_id"]))
                failed.append({"job": j["id"], "shot": j["shot_id"]})
            else:
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (db.now(), j["id"]))
                running.append({"job": j["id"], "shot": j["shot_id"]})
        for p in projects:
            self.refresh_status(p)
        return {"project": pid, "done": done, "failed": failed, "running": running}

    async def _store_video(self, pid: str, j: dict, url: str, seconds: float) -> str:
        path = media.path_for(pid, "videos", f"{j['shot_id']}_v{j['version']}.mp4")
        try:
            if not url or media.is_mock_url(url):
                ok = await media.placeholder_mp4(path, seconds, seed=f"{j['shot_id']}{j['version']}",
                                                 aspect=self.settings(pid)["aspect_ratio"])
                return path if ok else ""
            data = await self.agg.fetch(url)
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception as e:  # 下载失败不影响任务成功；界面上仍可用上游 URL 播放
            log.warning("download video failed %s: %s", url[:80], e)
            return ""

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

    # ==================================================================
    # 成片
    # ==================================================================

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
                    clips.append({"path": j["local_path"], "seconds": j["seconds"], "dialogue": s["dialogue"]})
                else:
                    missing.append(s["shot_no"])
            if missing and not allow_partial:
                raise WorkflowError(f"第 {ep['no']} 集还有镜头没出片或没下载：{missing}")
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
            raise WorkflowError("没有可合成的剧集")
        self.refresh_status(pid)
        return {"project": pid, "episodes": out}

    # ==================================================================
    # 一键全流程（由 runner 放到后台跑）
    # ==================================================================

    async def run_all(self, pid: str, progress=None, resolution: str | None = None,
                      wait_timeout_s: float | None = None, poll_interval_s: float | None = None) -> dict:
        async def step(name: str, coro):
            if progress:
                await progress.start(name)
            try:
                r = await coro
            except Exception as e:
                if progress:
                    await progress.fail(name, str(e))
                raise
            if progress:
                await progress.done(name, r if isinstance(r, dict) else {})
            return r

        async def _noop(msg):
            return {"note": msg}

        st = self.settings(pid)
        summary: dict = {}
        self._ensure_parsed(pid)
        with db.tx() as c:
            has_assets = c.execute("SELECT COUNT(*) n FROM assets WHERE project_id=?", (pid,)).fetchone()["n"]
        if has_assets:
            await step("资产抽取", _noop("已有资产，跳过"))
        else:
            r = await step("资产抽取", self._brief(self.extract_assets(pid), lambda r: {
                "source": r["source"], "added": len(r["added"])}))
            summary["assets"] = r
        if st["gen_asset_images"]:
            summary["asset_images"] = await step("资产参考图", self._brief(self.gen_asset_images(pid), lambda r: {
                "generated": len(r["generated"]), "failed": len(r["failed"])}))
        summary["storyboard"] = await step("分镜", self._brief(self.storyboard(pid), lambda r: {
            "episodes": r["results"]}))
        if st["gen_frames"]:
            summary["frames"] = await step("分镜图", self._brief(self.gen_frames(pid), lambda r: {
                "generated": len(r["generated"]), "failed": len(r["failed"])}))
        summary["prompts"] = await step("提示词", self._brief(self.prompts(pid), lambda r: {
            "generated": r["generated"], "source": r["source"]}))
        summary["videos"] = await step("提交视频", self._brief(self.submit_videos(pid, resolution=resolution),
                                                           lambda r: {"submitted": len(r["submitted"]),
                                                                      "skipped": len(r["skipped"]),
                                                                      "quoted_rmb": r["quote"]["total_rmb"]}))
        summary["receipts"] = await step("等待出片", self.wait_videos(pid, wait_timeout_s, poll_interval_s))
        if st["auto_assemble"]:
            summary["final"] = await step("合成成片", self._brief(
                self.assemble(pid, allow_partial=True), lambda r: {"episodes": r["episodes"]}))
        return summary

    @staticmethod
    async def _brief(coro, fn):
        return fn(await coro)

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

    # ==================================================================
    # 内部
    # ==================================================================

    async def _with_topup(self, fn):
        """聚合层钱包是内部记账：余额不足时按 WORKFLOW_AUTO_TOPUP 自动充值后重试一次。
        真正的花钱上限由项目预算（budget_rmb）控制。"""
        try:
            return await fn()
        except AggError as e:
            if e.status != 402 or self.auto_topup_rmb <= 0:
                raise
            await self.agg.topup(self.auto_topup_rmb)
            return await fn()
