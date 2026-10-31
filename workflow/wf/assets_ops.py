"""资产库与出图：资产(角色/场景/道具/生物/音频) → 形态 → 版本；AI 润色、改图；场次站位图；分镜图；本地素材；音色截取。"""
from __future__ import annotations

import base64
import logging
import os
import re

from . import db, llm, media
from .agg import AggError
from .errors import NotFound, WorkflowError
from .util import image_size, new_id, short_hash

log = logging.getLogger("workflow.assets")

ASSET_KINDS = ("character", "scene", "prop", "creature", "audio")
KIND_CN = {"character": "角色", "scene": "场景", "prop": "道具", "creature": "生物", "audio": "音频"}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv")


def _media_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    raise WorkflowError(f"不支持的文件类型：{ext or filename}（图片 png/jpg/webp，音频 mp3/wav/m4a，视频 mp4/mov）")


class AssetsMixin:
    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _assets(self, c, pid: str) -> list[dict]:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM assets WHERE project_id=? ORDER BY CASE kind WHEN 'character' THEN 0 WHEN 'creature' THEN 1 "
            "WHEN 'scene' THEN 2 WHEN 'prop' THEN 3 ELSE 4 END, created_at", (pid,)).fetchall()]
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        q = ",".join("?" * len(ids))
        forms: dict[str, list[dict]] = {}
        for f in c.execute(f"SELECT * FROM asset_forms WHERE asset_id IN ({q}) ORDER BY sort, created_at", ids):
            forms.setdefault(f["asset_id"], []).append(dict(f))
        vers: dict[str, list[dict]] = {}
        for v in c.execute(f"SELECT * FROM asset_versions WHERE asset_id IN ({q}) ORDER BY version", ids):
            v = dict(v)
            v["display_url"] = media.url_of(v["local_path"]) or ("" if media.is_mock_url(v["image_url"])
                                                                 else v["image_url"])
            vers.setdefault(v["form_id"], []).append(v)
        eps = [dict(r) for r in c.execute("SELECT no, raw_text FROM episodes WHERE project_id=? ORDER BY no", (pid,))]
        usage: dict[str, int] = {}
        for s in c.execute("SELECT characters_json, scene_asset FROM shots WHERE project_id=?", (pid,)):
            for n in db.load(s["characters_json"], []):
                base = re.sub(r"[（(].*$", "", n)
                usage[base] = usage.get(base, 0) + 1
            if s["scene_asset"]:
                usage[s["scene_asset"]] = usage.get(s["scene_asset"], 0) + 1
        for a in rows:
            a["aliases"] = db.load(a.pop("aliases_json"), [])
            a["kind_cn"] = KIND_CN.get(a["kind"], a["kind"])
            a["forms"] = []
            for f in forms.get(a["id"], []):
                f["is_default"] = f["name"] == "默认"
                f["versions"] = vers.get(f["id"], [])
                cur = next((v for v in f["versions"] if v["version"] == f["current_version"]), None)
                f["image_url"] = cur["image_url"] if cur else ""
                f["local_path"] = cur["local_path"] if cur else ""
                f["display_url"] = cur["display_url"] if cur else ""
                f["media_type"] = cur["media_type"] if cur else ("audio" if a["kind"] == "audio" else "image")
                a["forms"].append(f)
            d = next((f for f in a["forms"] if f["is_default"]), a["forms"][0] if a["forms"] else {})
            a["default_form_id"] = d.get("id", "")
            a["current_version"] = d.get("current_version", 0)
            a["image_url"] = d.get("image_url", "")
            a["local_path"] = d.get("local_path", "")
            a["display_url"] = d.get("display_url", "")
            keys = [a["name"]] + a["aliases"]
            a["episodes"] = [e["no"] for e in eps if any(k and k in e["raw_text"] for k in keys)]
            a["usage"] = usage.get(a["name"], 0)
            a["ready"] = all(f["current_version"] for f in a["forms"])
        return rows

    def asset(self, aid: str) -> dict:
        with db.tx() as c:
            row = c.execute("SELECT project_id FROM assets WHERE id=?", (aid,)).fetchone()
            if not row:
                raise NotFound(f"资产不存在：{aid}")
            return next(a for a in self._assets(c, row["project_id"]) if a["id"] == aid)

    def _form(self, fid: str) -> tuple[dict, dict]:
        with db.tx() as c:
            row = c.execute("SELECT asset_id FROM asset_forms WHERE id=?", (fid,)).fetchone()
        if not row:
            raise NotFound(f"形态不存在：{fid}")
        a = self.asset(row["asset_id"])
        return a, next(f for f in a["forms"] if f["id"] == fid)

    # ------------------------------------------------------------------
    # 新增 / 抽取 / 删除
    # ------------------------------------------------------------------

    def _insert_asset(self, c, pid: str, kind: str, name: str, description: str = "",
                      aliases: list[str] | None = None, forms: list[dict] | None = None) -> str:
        aid = new_id("asset")
        c.execute("INSERT INTO assets(id,project_id,kind,name,aliases_json,description,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?)", (aid, pid, kind, name, db.dump(aliases or []), description,
                                               db.now(), db.now()))
        c.execute("INSERT INTO asset_forms(id,asset_id,name,sort,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  (new_id("form"), aid, "默认", 0, db.now(), db.now()))
        for k, f in enumerate(forms or [], start=1):
            if f.get("name") and f["name"] != "默认":
                c.execute("INSERT OR IGNORE INTO asset_forms(id,asset_id,name,description,sort,created_at,updated_at)"
                          " VALUES(?,?,?,?,?,?,?)", (new_id("form"), aid, f["name"], f.get("description") or "", k,
                                                     db.now(), db.now()))
        return aid

    async def extract_assets(self, pid: str, engine: str | None = None) -> dict:
        p = self._project_row(pid)
        if not p["script_text"].strip():
            raise WorkflowError("剧本是空的")
        st = self.settings(pid)
        data, source = await llm.extract_assets(self.agg, p["script_text"], p["style"], st["text_model"],
                                                engine or st["engine"])
        added, forms_added = [], []
        with db.tx() as c:
            existing = {(r["kind"], r["name"]): r["id"] for r in c.execute(
                "SELECT id, kind, name FROM assets WHERE project_id=?", (pid,))}
            for key, kind in (("characters", "character"), ("creatures", "creature"), ("scenes", "scene"),
                              ("props", "prop")):
                for a in data.get(key) or []:
                    if (kind, a["name"]) in existing:
                        aid = existing[(kind, a["name"])]
                        for k, f in enumerate(a.get("forms") or [], start=1):
                            cur = c.execute("INSERT OR IGNORE INTO asset_forms(id,asset_id,name,description,sort,"
                                            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                            (new_id("form"), aid, f["name"], f.get("description") or "", 100 + k,
                                             db.now(), db.now()))
                            if cur.rowcount:
                                forms_added.append(f"{a['name']}·{f['name']}")
                        continue
                    aid = self._insert_asset(c, pid, kind, a["name"], a.get("description") or "",
                                             a.get("aliases"), a.get("forms"))
                    added.append({"id": aid, "kind": kind, "name": a["name"],
                                  "forms": [f["name"] for f in a.get("forms") or []]})
        self.refresh_status(pid)
        return {"source": source, "added": added, "forms_added": forms_added, "state": self.get(pid)}

    def add_asset(self, pid: str, kind: str, name: str, description: str = "",
                  aliases: list[str] | None = None, forms: list[str] | None = None) -> dict:
        self._project_row(pid)
        if kind not in ASSET_KINDS:
            raise WorkflowError(f"资产类型只能是 {'/'.join(ASSET_KINDS)}")
        name = (name or "").strip()
        if not name:
            raise WorkflowError("资产名不能为空")
        with db.tx() as c:
            if c.execute("SELECT 1 FROM assets WHERE project_id=? AND kind=? AND name=?", (pid, kind, name)).fetchone():
                raise WorkflowError(f"已有同名{KIND_CN[kind]}：{name}")
            aid = self._insert_asset(c, pid, kind, name, description, aliases,
                                     [{"name": f} for f in (forms or [])])
        self.refresh_status(pid)
        return self.asset(aid)

    def delete_asset(self, aid: str) -> dict:
        a = self.asset(aid)
        with db.tx() as c:
            c.execute("DELETE FROM assets WHERE id=?", (aid,))
        return {"deleted": aid, "name": a["name"]}

    def add_form(self, aid: str, name: str, description: str = "") -> dict:
        a = self.asset(aid)
        name = (name or "").strip().removesuffix("形态")
        if not name or name == "默认":
            raise WorkflowError("形态名不能为空，也不能叫“默认”")
        if any(f["name"] == name for f in a["forms"]):
            raise WorkflowError(f"{a['name']} 已有形态：{name}")
        with db.tx() as c:
            c.execute("INSERT INTO asset_forms(id,asset_id,name,description,sort,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?)", (new_id("form"), aid, name, description, len(a["forms"]),
                                                 db.now(), db.now()))
        return self.asset(aid)

    def delete_form(self, fid: str) -> dict:
        a, f = self._form(fid)
        if f["is_default"]:
            raise WorkflowError("默认形态不能删")
        with db.tx() as c:
            c.execute("DELETE FROM asset_forms WHERE id=?", (fid,))
        return self.asset(a["id"])

    # ------------------------------------------------------------------
    # 出图 / 改图 / 润色 / 上传 / 切换版本
    # ------------------------------------------------------------------

    async def _new_version(self, a: dict, f: dict, prompt: str, refs: list[str] | None, source: str,
                           size: str) -> dict:
        p = self._project_row(a["project_id"])
        st = self.settings(a["project_id"])
        version = (max((v["version"] for v in f["versions"]), default=0)) + 1
        res = await self._with_topup(lambda: self.agg.image(st["image_model"], prompt, size=size, refs=refs or None,
                                                            idem=f"form:{f['id']}:v{version}:{short_hash(prompt)}"))
        url = (res.get("data") or [{}])[0].get("url") or ""
        local, how = await self._store_image(a["project_id"], "assets", f"{f['id']}_v{version}", url,
                                             seed=f"{a['name']}{f['name']}{version}")
        charged = float((res.get("billing") or {}).get("charged_rmb") or 0)
        with db.tx() as c:
            c.execute("INSERT INTO asset_versions(id,asset_id,form_id,version,media_type,prompt,image_url,local_path,"
                      "source,model,charged_rmb,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (new_id("av"), a["id"], f["id"], version, "image", prompt,
                       "" if url.startswith("data:") else url, local, source if how != "mock" else "mock",
                       st["image_model"], charged, db.now()))
            c.execute("UPDATE asset_forms SET current_version=? WHERE id=?", (version, f["id"]))
            c.execute("UPDATE assets SET updated_at=? WHERE id=?", (db.now(), a["id"]))
        del p
        return self.asset(a["id"])

    async def gen_form_image(self, fid: str) -> dict:
        a, f = self._form(fid)
        if a["kind"] == "audio":
            raise WorkflowError("音频资产不能出图；请上传音频，或从已出片镜头里截取音色")
        p = self._project_row(a["project_id"])
        st = self.settings(a["project_id"])
        prompt = llm.asset_image_prompt(a["kind"], a["name"], a["description"], p["style"],
                                        "" if f["is_default"] else f["name"], f["description"])
        refs = None
        if not f["is_default"]:  # 非默认形态参考默认形态，保证是同一个人
            d = next((x for x in a["forms"] if x["is_default"]), None)
            if d and (d["local_path"] or d["image_url"]):
                refs = [self._ref_url(d["local_path"], d["image_url"])]
        return await self._new_version(a, f, prompt, refs, "generated", image_size("16:9", st["image_resolution"]))

    async def gen_asset_image(self, aid: str, form_id: str | None = None) -> dict:
        a = self.asset(aid)
        return await self.gen_form_image(form_id or a["default_form_id"])

    async def edit_form_image(self, fid: str, instruction: str) -> dict:
        a, f = self._form(fid)
        if not (instruction or "").strip():
            raise WorkflowError("改图要求不能为空")
        if not f["current_version"]:
            raise WorkflowError("这个形态还没有图，先出图或上传")
        p = self._project_row(a["project_id"])
        st = self.settings(a["project_id"])
        prompt = llm.edit_image_prompt(a["kind"], a["name"], instruction.strip(), p["style"])
        return await self._new_version(a, f, prompt, [self._ref_url(f["local_path"], f["image_url"])], "edited",
                                       image_size("16:9", st["image_resolution"]))

    async def upscale_form(self, fid: str) -> dict:
        """4K 高清：以当前图为参考重出 4K 版本（新版本，原图保留在历史里）。"""
        a, f = self._form(fid)
        if a["kind"] == "audio":
            raise WorkflowError("音频资产不能放大")
        if not f["current_version"]:
            raise WorkflowError("这个形态还没有图")
        p = self._project_row(a["project_id"])
        prompt = (f"{p['style']}风格，{a['name']}{'' if f['is_default'] else '（' + f['name'] + '形态）'}高清放大："
                  f"严格保持参考图的构图、人物身份、服装、配色和所有细节不变，只提升清晰度与纹理细节到 4K，无文字无水印。")
        return await self._new_version(a, f, prompt, [self._ref_url(f["local_path"], f["image_url"])], "upscaled",
                                       image_size("16:9", "4K"))

    async def gen_asset_images(self, pid: str, only_missing: bool = True, kinds: list[str] | None = None) -> dict:
        with db.tx() as c:
            assets = self._assets(c, pid)
        done, failed = [], []
        for a in assets:
            if a["kind"] == "audio" or (kinds and a["kind"] not in kinds):
                continue
            # 先出默认形态，其他形态才有参考
            for f in sorted(a["forms"], key=lambda x: (not x["is_default"], x["sort"])):
                if only_missing and f["current_version"]:
                    continue
                try:
                    na = await self.gen_form_image(f["id"])
                    nf = next(x for x in na["forms"] if x["id"] == f["id"])
                    done.append({"asset": a["name"], "form": f["name"], "version": nf["current_version"]})
                except (AggError, media.MediaError, WorkflowError) as e:
                    failed.append({"asset": a["name"], "form": f["name"], "error": str(e)[:200]})
        if done or failed:
            self.notify(pid, "error" if failed else "success", f"资产出图完成：{len(done)} 张"
                        + (f"，失败 {len(failed)}" if failed else ""))
        return {"generated": done, "failed": failed}

    async def polish_asset(self, aid: str) -> dict:
        a = self.asset(aid)
        p = self._project_row(a["project_id"])
        st = self.settings(a["project_id"])
        text, source = await llm.polish(self.agg, st["text_model"], a["kind"], a["name"], a["description"],
                                        p["style"], st["engine"])
        self.edit("asset", aid, {"description": text})
        return {"source": source, "asset": self.asset(aid)}

    def upload_form_media(self, fid: str, data: bytes, filename: str = "upload.png") -> dict:
        a, f = self._form(fid)
        if not data:
            raise WorkflowError("上传的文件是空的")
        mt = _media_type(filename)
        if a["kind"] == "audio" and mt != "audio":
            raise WorkflowError("音频资产只能上传音频文件")
        if a["kind"] != "audio" and mt != "image":
            raise WorkflowError("图片资产只能上传图片")
        ext = os.path.splitext(filename)[1].lower()
        version = (max((v["version"] for v in f["versions"]), default=0)) + 1
        path = media.path_for(a["project_id"], "assets", f"{fid}_v{version}{ext}")
        with open(path, "wb") as fh:
            fh.write(data)
        with db.tx() as c:
            c.execute("INSERT INTO asset_versions(id,asset_id,form_id,version,media_type,local_path,source,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?)", (new_id("av"), a["id"], fid, version, mt, path, "uploaded",
                                                   db.now()))
            c.execute("UPDATE asset_forms SET current_version=? WHERE id=?", (version, fid))
        return self.asset(a["id"])

    def upload_asset_image(self, aid: str, data: bytes, filename: str = "upload.png") -> dict:
        return self.upload_form_media(self.asset(aid)["default_form_id"], data, filename)

    def select_version(self, fid: str, version: int) -> dict:
        a, f = self._form(fid)
        if not any(v["version"] == version for v in f["versions"]):
            raise WorkflowError(f"没有这个版本：v{version}")
        with db.tx() as c:
            c.execute("UPDATE asset_forms SET current_version=? WHERE id=?", (version, fid))
        return self.asset(a["id"])

    def select_asset_version(self, aid: str, version: int) -> dict:
        return self.select_version(self.asset(aid)["default_form_id"], version)

    def bulk_upload(self, pid: str, files: list[tuple[str, bytes]]) -> dict:
        """批量上传：按文件名匹配资产（「孔宣.png」→ 孔宣·默认；「孔宣-蛋.png」「孔宣·蛋.png」→ 孔宣·蛋）。"""
        with db.tx() as c:
            assets = self._assets(c, pid)
        matched, unmatched = [], []
        for filename, data in files:
            stem = os.path.splitext(os.path.basename(filename))[0].strip()
            name, _, form = re.split(r"([-·_（(])", stem, maxsplit=1)[0], None, ""
            m = re.match(r"^(.+?)[-·_（(](.+?)[）)]?$", stem)
            if m:
                name, form = m.group(1), m.group(2).removesuffix("形态")
            a = next((x for x in assets if x["name"] == name or name in x["aliases"]), None)
            f = None
            if a:
                f = next((x for x in a["forms"] if x["name"] == (form or "默认")), None)
            if not a or not f:
                unmatched.append(filename)
                continue
            try:
                self.upload_form_media(f["id"], data, filename)
                matched.append({"file": filename, "asset": a["name"], "form": f["name"]})
            except WorkflowError as e:
                unmatched.append(f"{filename}（{e}）")
        return {"matched": matched, "unmatched": unmatched}

    async def _store_image(self, pid: str, sub: str, stem: str, url: str, seed: str) -> tuple[str, str]:
        """把上游图片落到本地。返回 (local_path, source)。mock URL 生成占位图。"""
        if not url or media.is_mock_url(url):
            path = media.path_for(pid, sub, stem + ".png")
            media.placeholder_png(path, seed)
            return path, "mock"
        if url.startswith("data:"):
            head, _, b64 = url.partition(",")
            path = media.path_for(pid, sub, stem + (".jpg" if "jpeg" in head else ".png"))
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(b64))
            return path, "generated"
        try:
            data = await self.agg.fetch(url)
        except Exception as e:  # 下载失败不致命：保留上游 URL
            log.warning("download image failed %s: %s", url[:80], e)
            return "", "generated"
        ext = ".jpg" if url.lower().split("?")[0].endswith((".jpg", ".jpeg")) else ".png"
        path = media.path_for(pid, sub, stem + ext)
        with open(path, "wb") as fh:
            fh.write(data)
        return path, "generated"

    def _ref_url(self, local_path: str, url: str) -> str:
        """给上游的参考素材：mock URL 原样；本地文件转 data URI（上游临时链接会过期）；否则用 URL。"""
        if url and media.is_mock_url(url):
            return url
        if local_path and os.path.exists(local_path):
            return media.data_uri(local_path)
        return url

    # ------------------------------------------------------------------
    # 站位图（每个场次一张，同场景的镜头共用）
    # ------------------------------------------------------------------

    async def gen_blocking(self, pid: str, scene_ids: list[str] | None = None, only_missing: bool = True,
                           episode_no: int | None = None) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        by_name = {a["name"]: a for a in proj["assets"]}
        done, failed = [], []
        for sc in proj["scenes"]:
            if scene_ids and sc["id"] not in scene_ids:
                continue
            if episode_no is not None and sc["episode_no"] != episode_no:
                continue
            if only_missing and sc["blocking"] and not scene_ids:
                continue
            chars = sc["characters"] or llm.scene_characters(
                sc["text"], [a["name"] for a in proj["assets"] if a["kind"] in ("character", "creature")])
            refs = []
            for name in [sc["location"]] + chars[:4]:
                a = by_name.get(name)
                if a and (a["local_path"] or a["image_url"]) and a["kind"] != "audio":
                    refs.append(self._ref_url(a["local_path"], a["image_url"]))
            prompt = llm.blocking_prompt(sc, chars, proj["style"], "16:9")
            try:
                res = await self._with_topup(lambda: self.agg.image(
                    st["image_model"], prompt, size=image_size("16:9", st["image_resolution"]), refs=refs or None,
                    idem=f"blocking:{sc['id']}:{short_hash(prompt, len(refs))}"))
                url = (res.get("data") or [{}])[0].get("url") or ""
                local, _ = await self._store_image(pid, "blocking", f"{sc['id']}_{short_hash(prompt)[:6]}", url,
                                                   seed=sc["id"])
                charged = float((res.get("billing") or {}).get("charged_rmb") or 0)
                with db.tx() as c:
                    c.execute("UPDATE scenes SET blocking_url=?, blocking_path=?, blocking_prompt=?, "
                              "blocking_charged_rmb=blocking_charged_rmb+? WHERE id=?",
                              ("" if url.startswith("data:") else url, local, prompt, charged, sc["id"]))
                done.append({"scene": sc["id"], "code": sc["code"], "refs": len(refs)})
            except (AggError, media.MediaError) as e:
                failed.append({"scene": sc["id"], "code": sc["code"], "error": str(e)[:200]})
        return {"generated": done, "failed": failed}

    # ------------------------------------------------------------------
    # 分镜图（每镜一张预览，可选当首帧）
    # ------------------------------------------------------------------

    async def gen_frames(self, pid: str, shot_ids: list[str] | None = None, only_missing: bool = True) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        by_name = {a["name"]: a for a in proj["assets"]}
        done, failed = [], []
        for s in proj["shots"]:
            if shot_ids and s["id"] not in shot_ids:
                continue
            if only_missing and s["frame"] and not shot_ids:
                continue
            refs = []
            for name in [re.sub(r"[（(].*$", "", x) for x in (s["characters"] or [])[:3]] + \
                    ([s["scene_asset"]] if s["scene_asset"] else []):
                a = by_name.get(name)
                if a and (a["local_path"] or a["image_url"]):
                    refs.append(self._ref_url(a["local_path"], a["image_url"]))
            prompt = llm.frame_image_prompt(s, proj["style"], st["aspect_ratio"])
            try:
                res = await self._with_topup(lambda: self.agg.image(
                    st["image_model"], prompt, size=image_size(st["aspect_ratio"], "1K"), refs=refs or None,
                    idem=f"frame:{s['id']}:{short_hash(prompt, len(refs))}"))
                url = (res.get("data") or [{}])[0].get("url") or ""
                local, _ = await self._store_image(pid, "frames", f"{s['id']}_{short_hash(prompt)[:6]}", url,
                                                   seed=s["id"])
                charged = float((res.get("billing") or {}).get("charged_rmb") or 0)
                with db.tx() as c:
                    c.execute("UPDATE shots SET frame_url=?, frame_path=?, frame_charged_rmb=frame_charged_rmb+? "
                              "WHERE id=?", ("" if url.startswith("data:") else url, local, charged, s["id"]))
                done.append({"shot": s["id"], "code": s["code"]})
            except (AggError, media.MediaError) as e:
                failed.append({"shot": s["id"], "error": str(e)[:200]})
        return {"generated": done, "failed": failed}

    # ------------------------------------------------------------------
    # 本地素材 / 音色截取
    # ------------------------------------------------------------------

    @staticmethod
    def _material_view(m: dict) -> dict:
        m["url"] = media.url_of(m["local_path"])
        return m

    def upload_material(self, pid: str, data: bytes, filename: str, name: str = "") -> dict:
        self._project_row(pid)
        if not data:
            raise WorkflowError("上传的文件是空的")
        mt = _media_type(filename)
        mid = new_id("mat")
        path = media.path_for(pid, "materials", f"{mid}{os.path.splitext(filename)[1].lower()}")
        with open(path, "wb") as fh:
            fh.write(data)
        with db.tx() as c:
            c.execute("INSERT INTO materials(id,project_id,name,media_type,local_path,size,created_at)"
                      " VALUES(?,?,?,?,?,?,?)", (mid, pid, name or os.path.splitext(filename)[0], mt, path,
                                                 len(data), db.now()))
            row = c.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
        return self._material_view(dict(row))

    def delete_material(self, mid: str) -> dict:
        with db.tx() as c:
            if not c.execute("DELETE FROM materials WHERE id=?", (mid,)).rowcount:
                raise NotFound("素材不存在")
        return {"deleted": mid}

    async def extract_voice(self, job_id: str, character: str, start: float = 0.0, seconds: float = 5.0) -> dict:
        """从已出片镜头里截一段音频，作为角色音色（音频资产「角色名音色」的新版本），后续提示词自动写声线参考。"""
        with db.tx() as c:
            j = c.execute("SELECT j.*, s.project_id FROM jobs j JOIN shots s ON s.id=j.shot_id WHERE j.id=?",
                          (job_id,)).fetchone()
        if not j:
            raise NotFound("视频任务不存在")
        if j["status"] != "succeeded" or not j["local_path"] or not os.path.exists(j["local_path"]):
            raise WorkflowError("这个版本还没有本地视频文件")
        character = (character or "").strip()
        if not character:
            raise WorkflowError("要写是谁的音色")
        pid = j["project_id"]
        seconds = max(1.0, min(30.0, float(seconds)))
        mid = new_id("mat")
        path = media.path_for(pid, "materials", f"{mid}.m4a")
        await media.extract_audio(j["local_path"], path, float(start), seconds)
        name = f"{character}音色"
        with db.tx() as c:
            c.execute("INSERT INTO materials(id,project_id,name,media_type,local_path,source,source_job_id,clip_start,"
                      "clip_seconds,size,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (mid, pid, name, "audio", path, "extracted", job_id, float(start), seconds,
                       os.path.getsize(path), db.now()))
            row = c.execute("SELECT id FROM assets WHERE project_id=? AND kind='audio' AND name=?", (pid, name)).fetchone()
            aid = row["id"] if row else self._insert_asset(c, pid, "audio", name, f"{character}的声线参考")
            f = c.execute("SELECT * FROM asset_forms WHERE asset_id=? AND name='默认'", (aid,)).fetchone()
            ver = c.execute("SELECT COALESCE(MAX(version),0)+1 v FROM asset_versions WHERE form_id=?",
                            (f["id"],)).fetchone()["v"]
            c.execute("INSERT INTO asset_versions(id,asset_id,form_id,version,media_type,local_path,source,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?)", (new_id("av"), aid, f["id"], ver, "audio", path, "extracted",
                                                   db.now()))
            c.execute("UPDATE asset_forms SET current_version=? WHERE id=?", (ver, f["id"]))
        self.notify(pid, "success", f"已截取 {name}", f"{seconds:g} 秒，来自镜头视频 v{j['version']}")
        return {"material": mid, "asset": self.asset(aid)}
