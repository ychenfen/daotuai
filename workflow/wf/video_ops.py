"""视频：报价 → 预算 → 提交（单镜参数可覆盖、参考素材可勾选、多版本、幂等）→ 回执 → 下载/封面 → 选版本 → 按集合成 → 批量下载。"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
import re
import tempfile
import zipfile

from . import db, llm, media
from .agg import AggError
from .errors import BudgetExceeded, Conflict, NotFound, WorkflowError
from .util import new_id, short_hash
from .qc_dub import qc_view

log = logging.getLogger("workflow.video")

ACTIVE_JOB = ("submitted", "running")


class VideoMixin:
    @staticmethod
    def _job_view(j: dict) -> dict:
        j["refs"] = db.load(j.pop("refs_json"), [])
        j["play_url"] = media.url_of(j["local_path"]) or ("" if media.is_mock_url(j["url"]) else j["url"])
        j["poster_url"] = media.url_of(j["poster_path"])
        j["qc"] = qc_view(j.pop("qc_json", "") or "")
        j["retake"] = db.load(j.pop("retake_json", "") or "", None)
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
            # 没手动指定时长时，按台词长度补足（老分镜卡也适用）
            dur = self._clamp_duration(duration or max(float(s["duration_s"] or 0),
                                                       -(-llm.speech_seconds(s.get("dialogue") or "") // 1)), m)
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
        elif mode == "chain":
            # 接上一镜尾帧：取同一集上一张卡已选版本的最后一帧当首帧，镜头之间动作与画面连贯
            prev = next((x for x in proj["shots"] if x["episode_id"] == s["episode_id"]
                         and x["shot_no"] == s["shot_no"] - 1), None)
            pj = prev["job"] if prev and prev.get("job") and prev["job"]["status"] == "succeeded" else None
            if pj and pj["local_path"] and os.path.exists(pj["local_path"]) and "首帧参考" in modes:
                dst = media.path_for(proj["id"], "frames", f"{s['id']}_tail_of_{pj['id']}.jpg")
                if not os.path.exists(dst):
                    media.last_frame(pj["local_path"], dst)
                images.append({"url": media.data_uri(dst), "role": "first_frame"})
                record.append({"chain_from": prev["code"], "job_id": pj["id"], "role": "first_frame"})
                mode = "first_frame"
            else:
                return self._video_refs(proj, s, "auto", m, form_ids, material_ids)
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
        if mode not in ("auto", "text", "first_frame", "reference", "chain"):
            raise WorkflowError("mode 只能是 auto/text/first_frame/reference/chain")
        shots = {s["id"]: s for s in proj["shots"]}
        submitted, skipped = [], list(quote["skipped"])
        for item in quote["items"]:
            s = shots[item["shot"]]
            used_mode, images, record = self._video_refs(proj, s, mode, m, form_ids, material_ids)
            version = (max((j["version"] for j in s["jobs"]), default=0)) + 1
            crid = short_hash(s["id"], version, s["prompt"]["rev"], model, res, item["seconds"], used_mode)
            prompt = self.upstream_prompt(s["prompt"]["positive"], used_mode, record, s["prompt"]["bindings"])
            if s["prompt"].get("negative"):
                prompt += f"\n避免：{s['prompt']['negative']}"
            try:
                r = await self._with_topup(lambda: self.agg.submit_video(
                    model, prompt, res, item["seconds"], aspect, images or None, idem=crid, project=proj["name"]))
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

    @staticmethod
    def upstream_prompt(prompt: str, mode: str, record: list[dict], bindings: list[dict]) -> str:
        """发给视频模型前改写提示词：模型认的是“第几张参考图”，不认我们的资产名。
        全能参考：@孔宣 → 图1（孔宣），并在开头写【参考】图N 是什么；音色 → 音频N。其他模式：去掉 @ 符号。"""
        import re as _re
        idx: dict[str, str] = {}
        legend: list[str] = []
        n_img = n_aud = 0
        if mode == "reference":
            for r in record:
                if r.get("role") == "reference_audio":
                    n_aud += 1
                    tag = f"音频{n_aud}"
                    legend.append(f"{tag}：{r.get('name', '')}（声线）")
                else:
                    n_img += 1
                    tag = f"图{n_img}"
                    if r.get("blocking"):
                        legend.append(f"{tag}：场次 {r['blocking']} 的站位图（只参考人物位置关系，不要照搬画面）")
                        continue
                    label = r.get("name", "") + (f"（{r['form']}形态）" if r.get("form") and r["form"] != "默认" else "")
                    legend.append(f"{tag}：{label}")
                if r.get("form_id"):
                    idx[r["form_id"]] = tag
                elif r.get("asset_id"):
                    idx[r["asset_id"]] = tag
        out = prompt
        for b in sorted(bindings or [], key=lambda x: -len(x.get("mention", ""))):
            m = b.get("mention") or ""
            if not m.startswith("@"):
                continue
            tag = idx.get(b.get("form_id") or "") or idx.get(b.get("asset_id") or "")
            name = m[1:]
            out = out.replace(m, f"{tag}（{name}）" if tag else name)
        out = _re.sub(r"@(?=[\u4e00-\u9fa5A-Za-z])", "", out)   # 没绑定上的 @ 一律去掉符号
        if legend:
            out = "【参考】" + "；".join(legend) + "\n" + out
        return out

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

    # ------------------------------------------------------------------
    # 视频工具：视频编辑（改已有成片）/ 动作迁移（角色图 + 参考动作视频）。结果都是该镜头的新版本。
    # ------------------------------------------------------------------

    TOOL_MODES = {"video_edit": "视频编辑", "motion_transfer": "动作迁移"}

    def _video_source(self, proj: dict, s: dict, source_job_id: str | None, source_material_id: str | None) -> dict:
        if source_material_id:
            mat = next((x for x in proj["materials"] if x["id"] == source_material_id), None)
            if not mat or mat["media_type"] != "video":
                raise WorkflowError("选的本地素材不是视频")
            return {"path": mat["local_path"], "url": "", "label": f"素材 {mat['name']}", "material_id": mat["id"]}
        jobs = [j for x in proj["shots"] for j in x["jobs"]]
        j = next((x for x in jobs if x["id"] == source_job_id), None) if source_job_id else s["job"]
        if not j or j["status"] != "succeeded":
            raise WorkflowError("没有可用的源视频：先给这个镜头出片，或上传一段视频素材")
        return {"path": j["local_path"], "url": j["url"], "label": f"v{j['version']}", "job_id": j["id"],
                "seconds": j["seconds"]}

    @staticmethod
    def _video_ref(src: dict) -> str:
        if src.get("url") and media.is_mock_url(src["url"]):
            return src["url"]
        if src.get("path") and os.path.exists(src["path"]):
            if os.path.getsize(src["path"]) > 20 * 1024 * 1024:
                if src.get("url"):
                    return src["url"]
                raise WorkflowError("源视频超过 20 MB，无法直接上传给模型；请先压缩或剪短")
            return media.data_uri(src["path"])
        if src.get("url"):
            return src["url"]
        raise WorkflowError("源视频文件不见了")

    async def submit_tool(self, pid: str, shot_id: str, tool: str, instruction: str = "", model: str | None = None,
                          resolution: str | None = None, source_job_id: str | None = None,
                          source_material_id: str | None = None, form_id: str | None = None) -> dict:
        if tool not in self.TOOL_MODES:
            raise WorkflowError("tool 只能是 video_edit / motion_transfer")
        proj = self.get(pid)
        s = next((x for x in proj["shots"] if x["id"] == shot_id), None)
        if not s:
            raise NotFound("镜头不存在")
        want = self.TOOL_MODES[tool]
        if not model:
            m = next((x for x in await self.agg.models() if want in (x.get("modes") or [])), None)
            if not m:
                raise WorkflowError(f"模型目录里没有支持「{want}」的模型")
            model = m["id"]
        m = await self.model_info(model)
        if want not in (m.get("modes") or []):
            raise WorkflowError(f"{model} 不支持「{want}」")
        res = (resolution or (m.get("specs") or ["720P"])[0]).upper()
        if m.get("specs") and res not in m["specs"]:
            res = m["specs"][0]
        src = self._video_source(proj, s, source_job_id, source_material_id)
        info = media.probe(src["path"]) if src.get("path") and os.path.exists(src["path"]) else {}
        seconds = self._clamp_duration(info.get("duration") or src.get("seconds") or s["duration_s"], m)
        images, record = [], [{"source": src["label"], "job_id": src.get("job_id"), "material_id": src.get("material_id")}]
        if tool == "motion_transfer":
            forms = {f["id"]: (a, f) for a in proj["assets"] for f in a["forms"]}
            if not form_id or form_id not in forms:
                raise WorkflowError("动作迁移要选一个角色形态（用它的图来演参考视频里的动作）")
            a, f = forms[form_id]
            if not (f["local_path"] or f["image_url"]):
                raise WorkflowError(f"{a['name']}（{f['name']}）还没有图")
            images.append({"url": self._ref_url(f["local_path"], f["image_url"]), "role": "reference_image"})
            record.append({"asset_id": a["id"], "form_id": f["id"], "name": a["name"], "form": f["name"],
                           "version": f["current_version"], "role": "reference_image"})
            prompt = f"让参考图中的{a['name']}按参考视频的动作与镜头运动表演，保持人物外观一致。{instruction or ''}".strip()
        else:
            if not (instruction or "").strip():
                raise WorkflowError("视频编辑要写修改要求，比如“把背景换成雨夜”")
            prompt = instruction.strip()
        q = await self.agg.quote(model, spec=res, seconds=seconds)
        spend = self._spend(pid)
        if q["rmb"] > spend["remaining_rmb"] + 1e-9:
            raise BudgetExceeded(f"超出项目预算：本次约 ¥{q['rmb']:.2f}，剩余 ¥{spend['remaining_rmb']:.2f}",
                                 {"total_rmb": q["rmb"], **spend})
        video_ref = self._video_ref(src)
        version = (max((j["version"] for j in s["jobs"]), default=0)) + 1
        crid = short_hash(s["id"], version, tool, model, res, prompt, src["label"], form_id)
        r = await self.agg.submit_video(model, prompt, res, seconds, proj["settings"]["aspect_ratio"],
                                        images or None, idem=crid, video=video_ref)
        jid = new_id("job")
        with db.tx() as c:
            c.execute("""INSERT INTO jobs(id,shot_id,version,client_request_id,agg_task_id,model,resolution,aspect_ratio,
                         duration_s,mode,refs_json,prompt_snapshot,status,quoted_rmb,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (jid, s["id"], version, crid, r["id"], model, res, proj["settings"]["aspect_ratio"], seconds,
                       tool, db.dump(record), prompt, "submitted",
                       float((r.get("billing") or {}).get("quoted_rmb") or q["rmb"]), db.now(), db.now()))
            c.execute("UPDATE shots SET status='video_submitted' WHERE id=?", (s["id"],))
        self.refresh_status(pid)
        self.notify(pid, "info", f"已提交{want}：镜头 {s['code']}", f"{model} · {res} · 源 {src['label']}")
        return {"job": jid, "version": version, "mode": tool, "model": model, "resolution": res,
                "seconds": seconds, "quoted_rmb": q["rmb"], "source": src["label"]}

    # ------------------------------------------------------------------
    # 剪辑工程导出：编号片段 + SRT + EDL + FCPXML（Premiere / 达芬奇 / Final Cut 可直接导入；剪映拖入片段与字幕）
    # ------------------------------------------------------------------

    def build_edit_package(self, pid: str, episode_no: int | None = None) -> tuple[str, str]:
        proj = self.get(pid)
        aspect = proj["settings"]["aspect_ratio"]
        w, h = media.aspect_size(aspect, 720)
        fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="edit_")
        os.close(fd)
        n_eps = 0
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for ep in proj["episodes"]:
                if episode_no is not None and ep["no"] != episode_no:
                    continue
                clips = []
                for s in [x for x in proj["shots"] if x["episode_id"] == ep["id"]]:
                    j = s["job"] if s["job"] and s["job"]["status"] == "succeeded" else None
                    if not (j and j["local_path"] and os.path.exists(j["local_path"])):
                        continue
                    full = float(media.probe(j["local_path"]).get("duration") or j["seconds"] or s["duration_s"])
                    t_in, t_out = float(s.get("trim_in") or 0), float(s.get("trim_out") or 0)
                    if full - t_in - t_out < 0.5:
                        t_in = t_out = 0.0
                    name = f"{len(clips) + 1:02d}_{s['code']}_v{j['version']}.mp4"
                    clips.append({"name": name, "path": j["local_path"], "seconds": full - t_in - t_out,
                                  "in": t_in, "source_seconds": full,
                                  "dialogue": self._subtitle_text(s["dialogue"]), "code": s["code"]})
                if not clips:
                    continue
                n_eps += 1
                base = f"第{ep['no']}集"
                for c_ in clips:
                    z.write(c_["path"], f"{base}/片段/{c_['name']}")
                t, subs = 0.0, []
                for c_ in clips:
                    if c_["dialogue"]:
                        subs.append((t + 0.1, t + c_["seconds"] - 0.1, c_["dialogue"]))
                    t += c_["seconds"]
                z.writestr(f"{base}/字幕.srt", media.build_srt(subs))
                z.writestr(f"{base}/时间线.edl", media.build_edl(f"EP{ep['no']:02d}", clips))
                z.writestr(f"{base}/时间线.fcpxml", media.build_fcpxml(f"EP{ep['no']:02d}", clips, w, h, "片段"))
            if not n_eps:
                raise WorkflowError("没有已出片的镜头可导出")
            z.writestr("导入说明.txt", (
                "每集一个文件夹：片段/ 按镜头顺序编号，字幕.srt 是台词字幕。\n"
                "Premiere / 达芬奇：导入 时间线.edl 或 时间线.fcpxml，提示找不到媒体时把路径指向“片段”文件夹。\n"
                "Final Cut Pro：文件 → 导入 → XML，选 时间线.fcpxml。\n"
                "剪映：新建草稿，把“片段”里的文件全选拖进时间线（文件名已按顺序编号），再 文本 → 本地字幕 导入 字幕.srt。\n"))
        return tmp, f"{proj['name']}-剪辑工程.zip"

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
        now = datetime.now(timezone.utc)
        for j in rows:
            code = f"{j['episode_no']}-{j['shot_no']}"
            limit = float(self.settings(j["project_id"]).get("job_timeout_min") or 45)
            try:
                age = (now - datetime.strptime(j["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
            except ValueError:
                age = None
            if age is not None and age.total_seconds() > limit * 60:
                # 卡住的任务：先在聚合层取消（退回预扣），再标成超时失败
                try:
                    await self.agg.cancel_video(j["agg_task_id"])
                except AggError as e:
                    log.warning("cancel stuck job %s failed: %s", j["id"], e)
                with db.tx() as c:
                    c.execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                              (f"超时（{limit:g} 分钟没出结果），已取消并退回预扣", db.now(), j["id"]))
                    has_ok = c.execute("SELECT 1 FROM jobs WHERE shot_id=? AND status='succeeded'",
                                       (j["shot_id"],)).fetchone()
                    c.execute("UPDATE shots SET status=? WHERE id=?",
                              ("video_done" if has_ok else "video_failed", j["shot_id"]))
                failed.append({"job": j["id"], "shot": j["shot_id"], "code": code, "timeout": True})
                projects.setdefault(j["project_id"], []).append(("bad", code))
                continue
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
                    c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id="
                              "(SELECT episode_id FROM shots WHERE id=?)", (j["shot_id"],))   # 换了版本，旧成片作废
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
        if done:
            with db.tx() as c:
                pids = {d["job"]: c.execute("SELECT s.project_id FROM jobs j JOIN shots s ON s.id=j.shot_id "
                                            "WHERE j.id=?", (d["job"],)).fetchone()["project_id"] for d in done}
            self._schedule_auto_qc([(p, jid) for jid, p in pids.items()])
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
            dub_mode = st.get("dub_mode") or "off"
            if dub_mode != "off":   # 配音模式：缺的/台词改过的镜头先补配音（已有且没变的会跳过）
                ready = [s["id"] for s in shots if s["job"] and s["job"]["status"] == "succeeded"]
                if ready:
                    await self.dub_shots(pid, shot_ids=ready)
                    shots = [s for s in self.get(pid)["shots"] if s["episode_id"] == ep["id"]]
            clips, missing = [], []
            for s in shots:
                j = s["job"] if s["job"] and s["job"]["status"] == "succeeded" else None
                if j and j["local_path"] and os.path.exists(j["local_path"]):
                    clip = {"path": j["local_path"], "seconds": j["seconds"],
                            "dialogue": self._subtitle_text(s["dialogue"]),
                            "trim_in": s.get("trim_in") or 0, "trim_out": s.get("trim_out") or 0,
                            "xfade": s.get("xfade_s") or 0}
                    if dub_mode != "off" and s.get("dub"):
                        clip["dub"] = s["dub_path"]
                        clip["subs"] = [(x["start"], x["end"], x["text"]) for x in s["dub"].get("lines", [])]
                    clips.append(clip)
                else:
                    missing.append(s["code"])
            if missing and not allow_partial:
                raise WorkflowError(f"第 {ep['no']} 集还有镜头没出片或没下载：{'、'.join(missing)}")
            if not clips:
                continue
            name = f"EP{ep['no']:02d}"
            final = media.path_for(pid, "exports", f"{name}.mp4")
            srt = media.path_for(pid, "exports", f"{name}.srt")
            bgm = next((x["local_path"] for x in proj["materials"] if x["id"] == st.get("bgm_material_id")
                        and x["media_type"] == "audio"), "")
            r = await media.assemble(clips, final, srt, st["aspect_ratio"], st["burn_subtitles"], bgm,
                                     float(st.get("bgm_volume") or 0.25), dub_mode,
                                     float(st.get("dub_orig_volume") if st.get("dub_orig_volume") is not None else 0.35))
            with db.tx() as c:
                c.execute("UPDATE episodes SET final_path=?, srt_path=?, final_seconds=?, final_at=? WHERE id=?",
                          (final, srt, r["seconds"], db.now(), ep["id"]))
            out.append({"episode": ep["no"], "url": media.url_of(final), "srt_url": media.url_of(srt),
                        "seconds": r["seconds"], "clips": r["clips"], "missing": missing,
                        "subtitles": r["subtitles"], "dubbed": r.get("dubbed", 0)})
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
