"""工作流编排（核心）：项目 → 剧本 → 分集/场次 → 资产 → 分镜 → 站位图 → 提示词(@绑定) → 视频 → 成片。

资产/出图在 assets_ops.py，视频/成片在 video_ops.py，对话/通知/导入导出/剧本文件在 extras.py。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3

from . import db, llm, media, refs
from .agg import Agg, AggError
from .assets_ops import AssetsMixin
from .errors import BudgetExceeded, Conflict, NotFound, WorkflowError  # noqa: F401  (re-export)
from .extras import ExtrasMixin
from .util import new_id, short_hash
from .qc_dub import QcDubMixin
from .ops import OpsMixin
from .produce import ProduceMixin
from .review import ReviewMixin
from .scriptwriter import ScriptMixin
from .video_ops import ACTIVE_JOB, VideoMixin

log = logging.getLogger("workflow.pipeline")

DEFAULT_SETTINGS = {
    "aspect_ratio": "9:16",
    "text_model": os.environ.get("WORKFLOW_TEXT_MODEL", "gpt-6.1-sol"),
    "image_model": os.environ.get("WORKFLOW_IMAGE_MODEL", "gpt-image-2.5"),
    "video_model": os.environ.get("WORKFLOW_VIDEO_MODEL", "seedance-2.5"),
    "resolution": os.environ.get("WORKFLOW_RESOLUTION", "480P"),
    "video_mode": "auto",            # auto | text | first_frame | reference
    "engine": "auto",                # auto（有模型用模型）| local（只用本地规则，零成本）
    "shot_seconds": 12,              # 每张视频卡的目标时长（会被视频模型的时长范围约束）
    "max_shots": 30,                 # 每集最多卡片数
    "prompt_format": "text",         # text | json
    "image_resolution": "2K",        # 1K | 2K | 4K
    "budget_rmb": float(os.environ.get("WORKFLOW_BUDGET_RMB", "50")),
    "gen_asset_images": True,
    "gen_blocking": True,            # 一键时每个场次生成站位图
    "use_blocking_ref": True,        # 出片时把站位图作为参考图
    "gen_frames": False,
    "auto_assemble": True,
    "burn_subtitles": False,
    "bgm_material_id": "",           # 成片配乐：本地素材里的音频
    "bgm_volume": 0.25,              # 配乐音量（0~1，压在镜头原声下面）
    "job_timeout_min": 45,           # 视频超过这么久没结果就取消并退回预扣
    "auto_qc": True,                 # 出片后自动质检（真实出片才调视觉模型，约 1 分钱/镜）
    "qc_model": "qwen3-vl-plus",
    "qc_pass_score": 75,
    "dub_mode": "off",               # off 保留原声 | mix 配音+原声垫底 | replace 只用配音
    "dub_orig_volume": 0.35,         # mix 时原声音量
    "narrator_voice": "Neil",        # 旁白音色
    "tts_model": "qwen3-tts-flash",
    "auto_retake": False,            # 自动质检不合格时按质检意见自动重抽（受预算约束）
    "retake_max": 1,                 # 每个镜头最多自动重抽几次
}
SETTING_TYPES = {k: type(v) for k, v in DEFAULT_SETTINGS.items()}
CHOICES = {"video_mode": ("auto", "text", "first_frame", "reference", "chain"), "engine": ("auto", "local"),
           "prompt_format": ("text", "json"), "image_resolution": ("1K", "2K", "4K"),
           "aspect_ratio": ("9:16", "16:9", "1:1", "3:4", "4:3"), "dub_mode": ("off", "mix", "replace")}

EDITABLE = {
    "shot": {"duration_s", "scene", "size", "angle", "movement", "picture", "performance", "blocking",
             "dialogue", "sfx", "transition", "characters", "scene_asset", "overlay"},
    "prompt": {"positive", "negative"},
    "asset": {"name", "aliases", "description", "notes"},
    "form": {"name", "description"},
    "scene": {"location", "blocking_note"},
}
TABLE = {"shot": "shots", "prompt": "prompts", "asset": "assets", "form": "asset_forms", "scene": "scenes"}
JSON_FIELDS = {"characters": "characters_json", "aliases": "aliases_json"}


_id = new_id
_h = short_hash


class Workflow(AssetsMixin, VideoMixin, QcDubMixin, ReviewMixin, OpsMixin, ProduceMixin, ScriptMixin, ExtrasMixin):
    def __init__(self, agg: Agg, auto_topup_rmb: float = 0.0):
        self.agg = agg
        self.auto_topup_rmb = auto_topup_rmb
        self._models: dict[str, dict] | None = None
        self._poll_lock: asyncio.Lock | None = None
        llm.charge_hook = self._add_text_spend

    @staticmethod
    def _add_text_spend(pid: str, rmb: float) -> None:
        with db.tx() as c:
            c.execute("UPDATE projects SET text_charged_rmb=text_charged_rmb+? WHERE id=?", (rmb, pid))

    @staticmethod
    async def gather_limited(coros, limit: int | None = None) -> list:
        """并发跑一批协程（默认同时 WORKFLOW_PARALLEL=4 路），结果按输入顺序返回；单个失败返回异常对象。"""
        sem = asyncio.Semaphore(limit or int(os.environ.get("WORKFLOW_PARALLEL", "4")))

        async def run(co):
            async with sem:
                return await co
        return await asyncio.gather(*(run(co) for co in coros), return_exceptions=True)

    # ==================================================================
    # 项目 / 设置 / 状态
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
        self.notify(pid, "info", f"新建项目：{name.strip()}")
        return self.get(pid)

    def list(self) -> list[dict]:
        with db.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT p.id, p.name, p.style, p.status, p.pinned, p.cover_path, p.created_at, p.updated_at,"
                " (SELECT COUNT(*) FROM episodes e WHERE e.project_id=p.id) episodes,"
                " (SELECT COUNT(*) FROM shots s WHERE s.project_id=p.id) shots,"
                " (SELECT COUNT(*) FROM assets a WHERE a.project_id=p.id) assets,"
                " (SELECT COUNT(*) FROM episodes e WHERE e.project_id=p.id AND e.final_path!='') finals"
                " FROM projects p ORDER BY p.pinned DESC, p.updated_at DESC").fetchall()]
            for r in rows:
                r["cover_url"] = media.url_of(r.pop("cover_path")) or self._auto_cover(c, r["id"])
        return rows

    @staticmethod
    def _auto_cover(c, pid: str) -> str:
        row = c.execute(
            "SELECT v.local_path FROM asset_versions v JOIN assets a ON a.id=v.asset_id "
            "JOIN asset_forms f ON f.id=v.form_id AND f.current_version=v.version "
            "WHERE a.project_id=? AND a.kind IN ('character','scene') AND v.media_type='image' AND v.local_path!='' "
            "ORDER BY CASE a.kind WHEN 'character' THEN 0 ELSE 1 END, a.created_at LIMIT 1", (pid,)).fetchone()
        return media.url_of(row["local_path"]) if row else ""

    def delete(self, pid: str) -> dict:
        self._project_row(pid)
        with db.tx() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))
            c.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
            c.execute("DELETE FROM changes WHERE project_id=?", (pid,))
            c.execute("DELETE FROM chat_sessions WHERE project_id=?", (pid,))
        return {"deleted": pid}

    def set_pinned(self, pid: str, pinned: bool) -> dict:
        self._project_row(pid)
        with db.tx() as c:
            c.execute("UPDATE projects SET pinned=? WHERE id=?", (1 if pinned else 0, pid))
        return {"id": pid, "pinned": pinned}

    def settings(self, pid: str) -> dict:
        p = self._project_row(pid)
        return {**DEFAULT_SETTINGS, **db.load(p["settings_json"], {})}

    def update_settings(self, pid: str, patch: dict) -> dict:
        cur = self.settings(pid)
        unknown = set(patch) - set(DEFAULT_SETTINGS) - {"style", "name"}
        if unknown:
            raise WorkflowError(f"未知设置项：{', '.join(sorted(unknown))}")
        merged = self._clean_settings({**cur, **{k: v for k, v in patch.items() if k in DEFAULT_SETTINGS}})
        with db.tx() as c:
            if "name" in patch and str(patch["name"]).strip():
                c.execute("UPDATE projects SET name=? WHERE id=?", (str(patch["name"]).strip(), pid))
            if "style" in patch and str(patch["style"]).strip():
                c.execute("UPDATE projects SET style=? WHERE id=?", (str(patch["style"]).strip(), pid))
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
        out["resolution"] = out["resolution"].upper()
        out["image_resolution"] = out["image_resolution"].upper()
        for k, allowed in CHOICES.items():
            if out[k] not in allowed:
                raise WorkflowError(f"{k} 只能是 {'/'.join(allowed)}")
        out["max_shots"] = max(1, min(80, out["max_shots"]))
        out["retake_max"] = max(0, min(3, out["retake_max"]))
        out["shot_seconds"] = max(2, min(30, out["shot_seconds"]))
        out["budget_rmb"] = max(0.0, out["budget_rmb"])
        out["bgm_volume"] = max(0.0, min(1.0, out["bgm_volume"]))
        out["job_timeout_min"] = max(5, min(600, out["job_timeout_min"]))
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
            scenes = [dict(r) for r in c.execute(
                "SELECT sc.*, e.no episode_no FROM scenes sc JOIN episodes e ON e.id=sc.episode_id "
                "WHERE sc.project_id=? ORDER BY e.no, sc.no", (pid,)).fetchall()]
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
            materials = [self._material_view(dict(r)) for r in c.execute(
                "SELECT * FROM materials WHERE project_id=? ORDER BY created_at DESC", (pid,)).fetchall()]
            task = c.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                             (pid,)).fetchone()
        forms_by_id = {f["id"]: f for a in assets for f in a["forms"]}
        scene_by_id = {s["id"]: s for s in scenes}
        for s in scenes:
            s["blocking"] = media.url_of(s["blocking_path"]) or s["blocking_url"]
            s["characters"] = []
        for s in shots:
            s["characters"] = db.load(s.pop("characters_json"), [])
            s["code"] = f"{s['episode_no']}-{s['shot_no']}"
            s["frame"] = media.url_of(s["frame_path"]) or s["frame_url"]
            sc = scene_by_id.get(s["scene_id"])
            s["scene_code"] = sc["code"] if sc else ""
            s["blocking_image"] = sc["blocking"] if sc else ""
            if sc:
                for ch in s["characters"]:
                    base = re.sub(r"[（(].*$", "", ch)
                    if base not in sc["characters"]:
                        sc["characters"].append(base)
            pr = prompts.get(s["id"])
            if pr:
                pr["bindings"] = db.load(pr.pop("bindings_json"), [])
                pr["unresolved"] = db.load(pr.pop("unresolved_json"), [])
                pr["warnings"] = db.load(pr.pop("warnings_json", "[]") or "[]", [])
                pr["stale"] = refs.stale(pr["bindings"], forms_by_id)
                pr["suggestions"] = refs.suggest(pr["unresolved"], assets) if pr["unresolved"] else {}
            s["prompt"] = pr
            s["density"] = llm.shot_density(s)
            dub = db.load(s.pop("dub_json", "") or "", {}) or {}
            s["dub"] = ({**dub, "url": media.url_of(s["dub_path"])}
                        if s.get("dub_path") and os.path.exists(s["dub_path"]) else None)
            s["jobs"] = jobs.get(s["id"], [])
            s["job"] = next((j for j in s["jobs"] if j["id"] == s["selected_job_id"]), None) \
                or (s["jobs"][-1] if s["jobs"] else None)
        vm = self.voice_map({"assets": assets})
        for a in assets:   # 配音实际会用的音色（没指定时是自动分配的）
            a["voice"] = vm[a["name"]]["voice"] if a["name"] in vm else ""
        self._size_rhythm(shots)
        kinds = {"character": "characters", "scene": "scenes", "prop": "props", "creature": "creatures"}
        for e in eps:
            e["cover_url"] = media.url_of(e.get("cover_path") or "")
            e["exports"] = self._exports_of(pid, e["no"])
            e["final_url"] = media.url_of(e["final_path"])
            e["srt_url"] = media.url_of(e["srt_path"])
            e["scenes"] = sum(1 for s in scenes if s["episode_id"] == e["id"])
            e_shots = [s for s in shots if s["episode_id"] == e["id"]]
            e["shots"] = len(e_shots)
            e["prompts"] = sum(1 for s in e_shots if s["prompt"])
            e["videos_done"] = sum(1 for s in e_shots if s["job"] and s["job"]["status"] == "succeeded")
            e["stats"] = {v: 0 for v in kinds.values()}
            for a in assets:
                if a["kind"] in kinds and e["no"] in a["episodes"]:
                    e["stats"][kinds[a["kind"]]] += 1
        p["bible"] = db.load(p.pop("bible_json", "") or "", {}) or {}
        p["script_review"] = db.load(p.pop("script_review_json", "") or "", {}) or {}
        return {**p, "cover_url": media.url_of(p.pop("cover_path")), "settings": settings, "episodes": eps,
                "scenes": scenes, "assets": assets, "shots": shots, "materials": materials,
                "spend": self._spend(pid), "task": self._task_view(dict(task)) if task else None}

    @staticmethod
    def _task_view(t: dict) -> dict:
        t["steps"] = db.load(t.pop("steps_json"), [])
        t["result"] = db.load(t.pop("result_json"), {})
        t["params"] = db.load(t.pop("params_json", "{}") or "{}", {})
        t["resumable"] = t["status"] == "failed" and "服务重启" in (t.get("error") or "") and t["kind"] in ("run_all", "produce")
        return t

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
            blocks = c.execute("SELECT COALESCE(SUM(blocking_charged_rmb),0) v FROM scenes WHERE project_id=?",
                               (pid,)).fetchone()["v"]
            prow = c.execute("SELECT text_charged_rmb, audio_charged_rmb FROM projects WHERE id=?", (pid,)).fetchone()
            text, audio = prow["text_charged_rmb"], prow["audio_charged_rmb"]
            covers = c.execute("SELECT COALESCE(SUM(cover_charged_rmb),0) v FROM episodes WHERE project_id=?",
                               (pid,)).fetchone()["v"]
        budget = float(self.settings(pid)["budget_rmb"])
        images = float(imgs) + float(frames) + float(blocks) + float(covers)
        total = round(float(jobs) + images + float(text) + float(audio), 4)
        return {"videos_rmb": round(float(jobs), 4), "images_rmb": round(images, 4), "text_rmb": round(float(text), 4),
                "audio_rmb": round(float(audio), 4),
                "total_rmb": total, "budget_rmb": budget, "remaining_rmb": round(budget - total, 4)}

    def refresh_status(self, pid: str) -> str:
        with db.tx() as c:
            shots = [r["status"] for r in c.execute("SELECT status FROM shots WHERE project_id=?", (pid,))]
            eps = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN final_path!='' THEN 1 ELSE 0 END) f "
                            "FROM episodes WHERE project_id=?", (pid,)).fetchone()
            n_assets = c.execute("SELECT COUNT(*) n FROM assets WHERE project_id=?", (pid,)).fetchone()["n"]
            p = c.execute("SELECT script_text FROM projects WHERE id=?", (pid,)).fetchone()
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
    # 剧本 / 分集 / 场次
    # ==================================================================

    def set_script(self, pid: str, text: str) -> dict:
        self._project_row(pid)
        if not (text or "").strip():
            raise WorkflowError("剧本是空的")
        with db.tx() as c:
            c.execute("UPDATE projects SET script_text=?, updated_at=? WHERE id=?", (text, db.now(), pid))
        self.refresh_status(pid)
        out = self.get(pid)
        out["script_check"] = self.script_check(text)
        return out

    def script_check(self, text: str) -> dict:
        eps = self.split_episodes(text)
        marked = bool(re.search(r"(?m)^\s*(?:第\s*[0-9一二三四五六七八九十百]+\s*集|EP\s*\d+)", text, re.I))
        n_scenes = sum(len(llm.split_scenes(i + 1, body)) for i, (_, body) in enumerate(eps))
        warnings = []
        if not marked:
            warnings.append("未检测到明确分集标题（将按 1 集处理）")
        if not any(llm.scene_heading(ln) for ln in text.splitlines()):
            warnings.append("未检测到场次标题（如“1-1 日 外 金鸡岭”），每集按一场处理")
        return {"chars": len(text), "episodes": len(eps), "scenes": n_scenes, "warnings": warnings}

    def parse(self, pid: str, force: bool = False) -> dict:
        p = self._project_row(pid)
        if not p["script_text"].strip():
            raise WorkflowError("剧本是空的")
        with db.tx() as c:
            n = c.execute("SELECT COUNT(*) n FROM shots WHERE project_id=?", (pid,)).fetchone()["n"]
        if n and not force:
            raise WorkflowError("已经有分镜了；重新解析会清空旧分镜、提示词和视频任务，确认请勾选后再解析（force=true）")
        parts = self.split_episodes(p["script_text"])
        with db.tx() as c:
            c.execute("DELETE FROM episodes WHERE project_id=?", (pid,))
            for no, (title, body) in enumerate(parts, start=1):
                eid = _id("ep")
                c.execute("INSERT INTO episodes(id,project_id,no,title,raw_text) VALUES(?,?,?,?,?)",
                          (eid, pid, no, title, body))
                for k, sc in enumerate(llm.split_scenes(no, body), start=1):
                    c.execute("INSERT INTO scenes(id,project_id,episode_id,no,code,heading,location,time_of_day,text,"
                              "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (_id("scene"), pid, eid, k, sc["code"], sc["heading"], sc["location"],
                               sc["time_of_day"], sc["text"], db.now()))
        self.refresh_status(pid)
        out = self.get(pid)
        out["script_check"] = self.script_check(p["script_text"])
        return out

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
    # 分镜（视频卡：按场次、按时间段）
    # ==================================================================

    async def _duration_range(self, model_id: str) -> tuple[int, int]:
        try:
            d = (await self.model_info(model_id)).get("duration") or {}
        except WorkflowError:
            d = {}
        return int(d.get("min", 4)), int(min(15, d.get("max", 15)))

    async def storyboard(self, pid: str, episode_no: int | None = None, force: bool = False,
                         max_shots: int | None = None, engine: str | None = None) -> dict:
        self._ensure_parsed(pid)
        proj = self.get(pid)
        st = proj["settings"]
        engine = engine or st["engine"]
        max_shots = max_shots or st["max_shots"]
        lo, hi = await self._duration_range(st["video_model"])
        target = max(lo, min(hi, st["shot_seconds"]))
        targets = [e for e in proj["episodes"] if episode_no is None or e["no"] == episode_no]
        if not targets:
            raise NotFound(f"第 {episode_no} 集不存在")
        llm.current_project.set(pid)
        char_names = [a["name"] for a in proj["assets"] if a["kind"] in ("character", "creature")]
        scene_assets = {a["name"] for a in proj["assets"] if a["kind"] == "scene"}
        results = []
        todo = [ep for ep in targets if force or not ep["shots"]]
        for ep in targets:
            if ep not in todo:
                results.append({"episode": ep["no"], "skipped": True, "shots": ep["shots"], "source": "cached"})
        # 每个场次单独一次模型调用、并发进行：输出短、不会被截断，长集也快
        jobs = [(ep, sc) for ep in todo for sc in proj["scenes"] if sc["episode_id"] == ep["id"]]
        outs = await self.gather_limited(
            llm.storyboard(self.agg, [sc], proj["style"], st["text_model"], max_shots, char_names, lo, hi, target, engine)
            for _, sc in jobs)
        per_ep: dict[str, list] = {}
        for (ep, sc), out in zip(jobs, outs):
            if isinstance(out, Exception):
                raise out
            per_ep.setdefault(ep["id"], []).append(out)
        for ep in todo:
            parts = per_ep.get(ep["id"], [])
            shots = [x for sh, _ in parts for x in sh][:max_shots]
            sources = {src for _, src in parts}
            source = "real" if sources == {"real"} else ("mock" if sources <= {"mock"} else "mixed")
            scenes = [s for s in proj["scenes"] if s["episode_id"] == ep["id"]]
            if not shots:
                raise WorkflowError(f"第 {ep['no']} 集没拆出镜头")
            by_code = {s["code"]: s for s in scenes}
            with db.tx() as c:
                c.execute("DELETE FROM shots WHERE episode_id=?", (ep["id"],))
                for n, s in enumerate(shots, start=1):
                    sc = by_code.get(s["scene_code"]) or (scenes[0] if scenes else None)
                    loc = sc["location"] if sc else ""
                    scene_asset = loc if loc in scene_assets else ""
                    c.execute("""INSERT INTO shots(id,project_id,episode_id,scene_id,shot_no,duration_s,scene,size,angle,
                                 movement,picture,performance,blocking,dialogue,sfx,transition,original_text,
                                 characters_json,scene_asset,status,source,updated_at,overlay)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (_id("shot"), pid, ep["id"], sc["id"] if sc else "", n,
                               llm.fit_duration(s["duration"], s["dialogue"], lo, hi),
                               loc or (sc["heading"] if sc else ""), s["size"], s["angle"], s["movement"],
                               s["picture"], s["performance"], s["blocking"], s["dialogue"], s["sfx"],
                               s["transition"], s.get("original_text", ""), db.dump(s["characters"]), scene_asset,
                               "storyboard_ready", source, db.now(), s.get("overlay", "")))
                c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id=?", (ep["id"],))
            long_lines = [f"{ep['no']}-{n}" for n, s in enumerate(shots, start=1) if llm.speech_seconds(s["dialogue"]) > hi]
            results.append({"episode": ep["no"], "shots": len(shots), "source": source, "long_dialogue": long_lines})
            if long_lines:
                self.notify(pid, "warning", f"第 {ep['no']} 集有 {len(long_lines)} 张卡台词超过 {hi} 秒",
                            "建议拆卡：" + "、".join(long_lines[:20]))
        results.sort(key=lambda r: r["episode"])
        self.refresh_status(pid)
        return {"project": pid, "results": results, "state": self.get(pid)}

    def add_shot(self, pid: str, episode_no: int, after_shot_no: int | None = None, fields: dict | None = None) -> dict:
        """新增卡片：插在 after_shot_no 后面（不给就放最后），后面的卡片顺延编号。"""
        fields = fields or {}
        with db.tx() as c:
            ep = c.execute("SELECT * FROM episodes WHERE project_id=? AND no=?", (pid, episode_no)).fetchone()
            if not ep:
                raise NotFound(f"第 {episode_no} 集不存在")
            last = c.execute("SELECT COALESCE(MAX(shot_no),0) n FROM shots WHERE episode_id=?",
                             (ep["id"],)).fetchone()["n"]
            pos = last + 1 if after_shot_no is None else max(1, min(last + 1, int(after_shot_no) + 1))
            c.execute("UPDATE shots SET shot_no=shot_no+1 WHERE episode_id=? AND shot_no>=?", (ep["id"], pos))
            sc = c.execute("SELECT id, location FROM scenes WHERE episode_id=? ORDER BY no LIMIT 1",
                           (ep["id"],)).fetchone()
            sid = _id("shot")
            c.execute("""INSERT INTO shots(id,project_id,episode_id,scene_id,shot_no,duration_s,scene,picture,dialogue,
                         characters_json,status,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (sid, pid, ep["id"], fields.get("scene_id") or (sc["id"] if sc else ""), pos,
                       float(fields.get("duration_s") or 5), fields.get("scene") or (sc["location"] if sc else ""),
                       fields.get("picture") or "", fields.get("dialogue") or "",
                       db.dump(fields.get("characters") or []), "storyboard_ready", "manual", db.now()))
        self.refresh_status(pid)
        return {"id": sid, "shot_no": pos}

    def delete_shot(self, sid: str) -> dict:
        with db.tx() as c:
            s = c.execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone()
            if not s:
                raise NotFound("镜头不存在")
            if c.execute("SELECT 1 FROM jobs WHERE shot_id=? AND status IN ('submitted','running')", (sid,)).fetchone():
                raise WorkflowError("这个镜头有视频在生成，先取消再删")
            c.execute("DELETE FROM shots WHERE id=?", (sid,))
            c.execute("UPDATE shots SET shot_no=shot_no-1 WHERE episode_id=? AND shot_no>?",
                      (s["episode_id"], s["shot_no"]))
        self.refresh_status(s["project_id"])
        return {"deleted": sid}

    # ==================================================================
    # 提示词（@绑定）
    # ==================================================================

    # 视频模型容易翻车的写法（OnlyShot 等实测 + 我们第 1 集）：写进提示词就提醒
    RISKY = [
        (re.compile(r"弹幕|标语|横幅|写着|印着|招牌上|告示|书法|写字|字幕滚动"), "画面里的文字会被画乱（改成后期贴字）"),
        (re.compile(r"人群|众人|一群|围观|满屋|千军万马|人山人海"), "多人场面角色一致性差（改成虚化背景或删掉）"),
        (re.compile(r"打字|弹琴|弹钢琴|数钱|手指特写|穿针|系鞋带"), "手部精细动作容易崩"),
        (re.compile(r"镜子里|镜中|倒影"), "镜像/倒影容易出错"),
        (re.compile(r"连续出拳|激烈打斗|翻滚|连环|空翻"), "复杂连续动作演不出来（拆成多张卡）"),
    ]

    @classmethod
    def _risky_words(cls, positive: str) -> list[str]:
        body = "\n".join(ln for ln in positive.replace(llm.SUFFIX, "").splitlines()
                         if not re.search(r"不出现|不要|避免|禁止|不能有", ln))
        out = []
        for pat, why in cls.RISKY:
            m = pat.search(body)
            if m:
                out.append(f"易翻车「{m.group(0)}」：{why}")
        return out

    @staticmethod
    def _size_rhythm(shots: list[dict]) -> None:
        """景别节奏（借鉴 Toonflow shotCraft）：同一集连续 3 张卡主景别相同就提示（看第一段子镜头，没有就看卡的景别）。"""
        def main(s):
            seg = llm.segments_full(s.get("picture") or "")
            if seg and seg[0][2]:
                return seg[0][2]
            return re.split(r"[、，,\s]+", s.get("size") or "")[0]
        run, last_ep = [], None
        for s in shots:
            m = main(s)
            if s["episode_id"] != last_ep:
                run, last_ep = [], s["episode_id"]
            run = run + [m] if run and m and run[-1] == m else ([m] if m else [])
            if len(run) >= 3:
                s["density"] = list(s.get("density") or []) + [f"连续 {len(run)} 张卡都是{m}开场，景别缺少变化"]

    @staticmethod
    def _prompt_warnings(positive: str, shot: dict, bible: dict, assets: list[dict] | None = None) -> list[str]:
        """道具一致性：故事圣经里的关键道具，提示词把它写成了“容易被误写成的东西”（实测：营业部电脑 → 手机），
        或分镜提到了它而提示词漏掉；屏幕文字写进了画面。"""
        out = []
        src = " ".join(str(shot.get(k) or "") for k in ("picture", "dialogue", "original_text", "performance"))
        for p in bible.get("props") or []:
            name = p.get("name") or ""
            if not name:
                continue
            in_src = name in src
            for w in p.get("avoid") or []:
                if w and w in positive and name not in positive:
                    out.append(f"道具写错：分镜/圣经里是「{name}」，提示词写成了「{w}」")
            if in_src and name not in positive and not any(w in positive for w in p.get("avoid") or []):
                out.append(f"提示词漏了关键道具「{name}」")
        for ov in (shot.get("overlay") or "").splitlines():
            if len(ov.strip()) >= 4 and ov.strip() in positive:
                out.append(f"屏幕文字「{ov.strip()[:12]}」写进了画面（应后期贴字）")
        if assets:   # 借鉴火宝：提示词只引用本镜头出场的人物；同框不超过 2 人（MovieAgent / OnlyShot）
            chars = {a["name"] for a in assets if a["kind"] in ("character", "creature")}
            on_shot = {re.sub(r"[（(].*$", "", c) for c in shot.get("characters") or []}
            used = {n for n in chars if f"@{n}" in positive}
            extra = sorted(used - on_shot) if on_shot else []
            if extra:
                out.append(f"引用了本镜头没有出场的人物：{'、'.join('@' + x for x in extra)}")
            if len(used) > 2:
                out.append(f"同框 {len(used)} 个人物，角色一致性会明显下降（建议拆卡或让次要人物虚化）")
        out += Workflow._risky_words(positive)
        return out[:8]

    def _voices(self, assets: list[dict]) -> dict[str, str]:
        """角色 → 音色资产名。约定：音频资产叫「角色名音色」或「角色名声线」。"""
        names = {a["name"] for a in assets if a["kind"] in ("character", "creature")}
        out = {}
        for a in assets:
            if a["kind"] != "audio" or not a["current_version"]:
                continue
            base = re.sub(r"(音色|声线|配音)$", "", a["name"])
            if base in names:
                out[base] = a["name"]
        return out

    async def prompts(self, pid: str, force: bool = False, shot_ids: list[str] | None = None,
                      episode_no: int | None = None, engine: str | None = None) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        engine = engine or st["engine"]
        shots = [s for s in proj["shots"] if (not shot_ids or s["id"] in shot_ids)
                 and (episode_no is None or s["episode_no"] == episode_no)
                 and (force or shot_ids or not s.get("prompt"))]
        if not shots:
            return {"project": pid, "generated": 0, "source": "cached", "state": proj}
        llm.current_project.set(pid)
        names = [a["name"] for a in proj["assets"]]
        voices = self._voices(proj["assets"])
        batches = [shots[i:i + 6] for i in range(0, len(shots), 6)]   # 小批量：输出不会被截断
        outs = await self.gather_limited(
            llm.prompts(self.agg, b, proj["style"], st["text_model"], st["aspect_ratio"], names, voices,
                        st["prompt_format"], st["video_model"], engine) for b in batches)
        data, sources = [], set()
        for out in outs:
            if isinstance(out, Exception):
                raise out
            data += out[0]
            sources.add(out[1])
        source = "real" if sources == {"real"} else ("mock" if sources <= {"mock"} else "mixed")
        assets = proj["assets"]
        with db.tx() as c:
            for shot, item in zip(shots, data):
                bindings, unresolved = refs.resolve(item["positive"], assets)
                warns = self._prompt_warnings(item["positive"], shot, proj.get("bible") or {}, assets)
                old = c.execute("SELECT * FROM prompts WHERE shot_id=?", (shot["id"],)).fetchone()
                if old:
                    rev = old["rev"] + 1
                    c.execute("UPDATE prompts SET positive=?, negative=?, model=?, bindings_json=?, unresolved_json=?,"
                              " warnings_json=?, rev=?, updated_at=? WHERE id=?",
                              (item["positive"], item["negative"], st["text_model"], db.dump(bindings),
                               db.dump(unresolved), db.dump(warns), rev, db.now(), old["id"]))
                    self._journal(c, pid, "prompt", old["id"], rev, ["positive", "negative"], "ai")
                else:
                    c.execute("INSERT INTO prompts(id,shot_id,positive,negative,model,bindings_json,unresolved_json,"
                              "warnings_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (_id("prompt"), shot["id"], item["positive"], item["negative"], st["text_model"],
                               db.dump(bindings), db.dump(unresolved), db.dump(warns), db.now(), db.now()))
                c.execute("UPDATE shots SET status='prompt_ready' WHERE id=? AND status='storyboard_ready'",
                          (shot["id"],))
        self.refresh_status(pid)
        return {"project": pid, "generated": len(data), "source": source, "state": self.get(pid)}

    async def prompts_from_script(self, pid: str, episode_no: int | None = None) -> dict:
        """剧本直转提示词：没有分镜的集先用本地规则拆卡，再转提示词。"""
        await self.storyboard(pid, episode_no, engine="local")
        return await self.prompts(pid, episode_no=episode_no)

    def resolve_references(self, pid: str, shot_ids: list[str] | None = None) -> dict:
        """按资产当前形态与版本重算所有绑定（资产改名/换图后点一次）。系统字段，不推进 rev。"""
        with db.tx() as c:
            assets = self._assets(c, pid)
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

    @staticmethod
    def _owner_project(c, kind: str, row: dict) -> str:
        if row.get("project_id"):
            return row["project_id"]
        if kind == "prompt":
            return c.execute("SELECT project_id FROM shots WHERE id=?", (row["shot_id"],)).fetchone()["project_id"]
        if kind == "form":
            return c.execute("SELECT project_id FROM assets WHERE id=?", (row["asset_id"],)).fetchone()["project_id"]
        raise WorkflowError("找不到所属项目")

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
            pid = self._owner_project(c, kind, row)
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
                if kind == "form" and f == "name" and row["name"] == "默认":
                    raise WorkflowError("默认形态不能改名")
                if v != current[f]:
                    new[f] = v
            merged = expected_rev is not None and int(expected_rev) != row["rev"]
            if merged:
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
                return {"changed": [], "rev": row["rev"], "merged": merged}
            rev = row["rev"] + 1
            sets = ", ".join(f"{JSON_FIELDS.get(f, f)}=?" for f in new)
            vals = [db.dump(v) if f in JSON_FIELDS else v for f, v in new.items()]
            try:
                c.execute(f"UPDATE {table} SET {sets}, rev=?, updated_at=? WHERE id=?", vals + [rev, db.now(), oid])
            except sqlite3.IntegrityError:
                raise WorkflowError("名字和已有的重复了")
            self._journal(c, pid, kind, oid, rev, list(new), "user")
            if kind == "prompt" and "positive" in new:
                assets_ = self._assets(c, pid)
                b, u = refs.resolve(new["positive"], assets_)
                srow = c.execute("SELECT s.* FROM shots s JOIN prompts p ON p.shot_id=s.id WHERE p.id=?", (oid,)).fetchone()
                shot_ = dict(srow) if srow else {}
                shot_["characters"] = db.load(shot_.get("characters_json") or "[]", [])
                prow = c.execute("SELECT bible_json FROM projects WHERE id=?", (pid,)).fetchone()
                warns = self._prompt_warnings(new["positive"], shot_, db.load(prow["bible_json"] or "", {}) or {}, assets_)
                c.execute("UPDATE prompts SET bindings_json=?, unresolved_json=?, warnings_json=? WHERE id=?",
                          (db.dump(b), db.dump(u), db.dump(warns), oid))
        return {"changed": sorted(new), "rev": rev, "merged": merged}

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
                self.notify(pid, "error", f"一键成片在「{name}」失败", str(e)[:300])
                raise
            if progress:
                await progress.done(name, r if isinstance(r, dict) else {})
            return r

        async def _note(msg):
            return {"note": msg}

        async def _brief(coro, fn):
            return fn(await coro)

        llm.current_project.set(pid)
        st = self.settings(pid)
        summary: dict = {}
        self._ensure_parsed(pid)
        with db.tx() as c:
            has_assets = c.execute("SELECT COUNT(*) n FROM assets WHERE project_id=?", (pid,)).fetchone()["n"]
        if has_assets:
            await step("资产抽取", _note("已有资产，跳过"))
        else:
            summary["assets"] = await step("资产抽取", _brief(self.extract_assets(pid), lambda r: {
                "source": r["source"], "added": len(r["added"])}))
        if st["gen_asset_images"]:
            summary["asset_images"] = await step("资产参考图", _brief(self.gen_asset_images(pid), lambda r: {
                "generated": len(r["generated"]), "failed": len(r["failed"])}))
        summary["storyboard"] = await step("分镜", _brief(self.storyboard(pid), lambda r: {"episodes": r["results"]}))
        if st["gen_blocking"]:
            summary["blocking"] = await step("站位图", _brief(self.gen_blocking(pid), lambda r: {
                "generated": len(r["generated"]), "failed": len(r["failed"])}))
        if st["gen_frames"]:
            summary["frames"] = await step("分镜图", _brief(self.gen_frames(pid), lambda r: {
                "generated": len(r["generated"]), "failed": len(r["failed"])}))
        summary["prompts"] = await step("提示词", _brief(self.prompts(pid), lambda r: {
            "generated": r["generated"], "source": r["source"]}))
        summary["videos"] = await step("提交视频", _brief(self.submit_videos(pid, resolution=resolution, accept_stale=True,
                                                                         allow_missing=True),
                                                       lambda r: {"submitted": len(r["submitted"]),
                                                                  "skipped": len(r["skipped"]),
                                                                  "quoted_rmb": r["quote"]["total_rmb"]}))
        summary["receipts"] = await step("等待出片", self.wait_videos(pid, wait_timeout_s, poll_interval_s))
        if st["auto_assemble"]:
            summary["final"] = await step("合成成片", _brief(
                self.assemble(pid, allow_partial=True), lambda r: {"episodes": r["episodes"]}))
        self.notify(pid, "success", "一键成片完成", f"提交 {summary['videos']['submitted']} 个镜头")
        return summary

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


__all__ = ["Workflow", "WorkflowError", "NotFound", "Conflict", "BudgetExceeded", "DEFAULT_SETTINGS",
           "ACTIVE_JOB", "_id", "_h"]
