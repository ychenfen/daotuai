"""周边功能：通知中心、AI 对话、剧本文件导入与一键剧本、项目导出/导入、封面。"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from xml.etree import ElementTree

from . import db, llm, media
from .errors import NotFound, WorkflowError
from .util import new_id

PATH_COLUMNS = {"projects": ["cover_path"], "episodes": ["final_path", "srt_path"], "scenes": ["blocking_path"],
                "asset_versions": ["local_path"], "shots": ["frame_path"], "materials": ["local_path"],
                "jobs": ["local_path", "poster_path"]}
EXPORT_TABLES = [("projects", "id=?"), ("episodes", "project_id=?"), ("scenes", "project_id=?"),
                 ("assets", "project_id=?"),
                 ("asset_forms", "asset_id IN (SELECT id FROM assets WHERE project_id=?)"),
                 ("asset_versions", "asset_id IN (SELECT id FROM assets WHERE project_id=?)"),
                 ("shots", "project_id=?"),
                 ("prompts", "shot_id IN (SELECT id FROM shots WHERE project_id=?)"),
                 ("materials", "project_id=?"),
                 ("jobs", "shot_id IN (SELECT id FROM shots WHERE project_id=?)")]


def _docx_text(data: bytes) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{ns}p"):
        paras.append("".join(t.text or "" for t in p.iter(f"{ns}t")))
    return "\n".join(paras)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        raise WorkflowError("读 PDF 需要先安装 pypdf：py -3.12 -m pip install pypdf（或把剧本另存为 TXT/DOCX）")
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)


def decode_script_file(data: bytes, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".docx":
        try:
            text = _docx_text(data)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            raise WorkflowError("DOCX 文件读不出来（可能损坏，或不是 Word 2007+ 格式）")
    elif ext == ".pdf":
        text = _pdf_text(data)
    elif ext in (".txt", ".md", ".fountain", ""):
        for enc in ("utf-8-sig", "gb18030", "big5"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise WorkflowError("文本编码认不出来，请另存为 UTF-8")
    else:
        raise WorkflowError("只支持 TXT / DOCX / PDF / MD")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class ExtrasMixin:
    # ------------------------------------------------------------------
    # 通知中心
    # ------------------------------------------------------------------

    @staticmethod
    def notify(pid: str, level: str, title: str, body: str = "") -> None:
        with db.tx() as c:
            c.execute("INSERT INTO notifications(project_id,level,title,body,created_at) VALUES(?,?,?,?,?)",
                      (pid or "", level, title[:200], body[:1000], db.now()))

    @staticmethod
    def notifications(limit: int = 50, unread_only: bool = False) -> dict:
        with db.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT n.*, COALESCE(p.name,'') project_name FROM notifications n LEFT JOIN projects p "
                "ON p.id=n.project_id " + ("WHERE n.read=0 " if unread_only else "") +
                "ORDER BY n.id DESC LIMIT ?", (limit,)).fetchall()]
            unread = c.execute("SELECT COUNT(*) n FROM notifications WHERE read=0").fetchone()["n"]
        return {"items": rows, "unread": unread}

    @staticmethod
    def mark_read(ids: list[int] | None = None) -> dict:
        with db.tx() as c:
            if ids:
                q = ",".join("?" * len(ids))
                n = c.execute(f"UPDATE notifications SET read=1 WHERE id IN ({q})", ids).rowcount
            else:
                n = c.execute("UPDATE notifications SET read=1 WHERE read=0").rowcount
        return {"marked": n}

    # ------------------------------------------------------------------
    # AI 对话
    # ------------------------------------------------------------------

    @staticmethod
    def chat_agents() -> list[dict]:
        return [{"id": k, "name": v["name"], "context": v.get("context", "")} for k, v in llm.AGENTS.items()]

    def chat_create(self, agent: str, project_id: str = "", title: str = "", model: str = "") -> dict:
        if agent not in llm.AGENTS:
            raise WorkflowError(f"没有这个智能体：{agent}")
        if project_id:
            self._project_row(project_id)
        sid = new_id("chat")
        with db.tx() as c:
            c.execute("INSERT INTO chat_sessions(id,project_id,agent,title,model,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?)", (sid, project_id or "", agent, title or llm.AGENTS[agent]["name"],
                                                model, db.now(), db.now()))
        return self.chat_get(sid)

    @staticmethod
    def chat_list(project_id: str | None = None) -> list[dict]:
        with db.tx() as c:
            sql = ("SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) messages "
                   "FROM chat_sessions s")
            args: tuple = ()
            if project_id is not None:
                sql += " WHERE s.project_id IN (?, '')"
                args = (project_id,)
            return [dict(r) for r in c.execute(sql + " ORDER BY s.updated_at DESC", args).fetchall()]

    @staticmethod
    def chat_get(sid: str) -> dict:
        with db.tx() as c:
            s = c.execute("SELECT * FROM chat_sessions WHERE id=?", (sid,)).fetchone()
            if not s:
                raise NotFound("对话不存在")
            msgs = [dict(r) for r in c.execute("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id",
                                               (sid,)).fetchall()]
        for m in msgs:
            m["meta"] = db.load(m.pop("meta_json"), {})
        return {**dict(s), "messages": msgs}

    @staticmethod
    def chat_delete(sid: str | None = None) -> dict:
        with db.tx() as c:
            n = c.execute("DELETE FROM chat_sessions" + (" WHERE id=?" if sid else ""), (sid,) if sid else ()).rowcount
        return {"deleted": n}

    def _chat_context(self, agent: str, project_id: str) -> str:
        if not project_id:
            return ""
        kind = llm.AGENTS.get(agent, {}).get("context")
        p = self.get(project_id)
        if kind == "script":
            return f"【项目剧本：{p['name']}】\n{p['script_text'][:12000]}"
        if kind == "assets":
            names = "；".join(f"{a['kind_cn']} {a['name']}（形态：{'、'.join(f['name'] for f in a['forms'])}）"
                             f"：{a['description'][:80]}" for a in p["assets"])
            return f"【项目资产表：{p['name']}，画风 {p['style']}，画幅 {p['settings']['aspect_ratio']}】\n{names}"
        return ""

    async def chat_send(self, sid: str, content: str, model: str | None = None) -> dict:
        s = self.chat_get(sid)
        content = (content or "").strip()
        if not content:
            raise WorkflowError("消息不能为空")
        agent = llm.AGENTS.get(s["agent"], llm.AGENTS["assistant"])
        model = model or s["model"] or (self.settings(s["project_id"])["text_model"] if s["project_id"]
                                        else os.environ.get("WORKFLOW_TEXT_MODEL", "gpt-6.1-sol"))
        context = self._chat_context(s["agent"], s["project_id"])
        system = agent["system"] + (f"\n\n{context}" if context else "")
        history = [{"role": m["role"], "content": m["content"]} for m in s["messages"][-20:]]
        meta: dict = {"model": model}
        if llm.mode() == "mock":
            reply = llm.mock_chat_reply(s["agent"], content, context)
            meta["source"] = "mock"
        else:
            res = await self.agg.chat(model, [{"role": "system", "content": system}, *history,
                                              {"role": "user", "content": content}], max_tokens=2000, temperature=0.7)
            reply = res["choices"][0]["message"]["content"]
            if s["project_id"]:
                llm.current_project.set(s["project_id"])
                llm.record_charge(res)
            meta.update(source="real", billing=res.get("billing"), routing=res.get("routing"))
            if reply.startswith("[mock:"):
                meta["source"] = "mock"
        with db.tx() as c:
            c.execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                      (sid, "user", content, db.now()))
            c.execute("INSERT INTO chat_messages(session_id,role,content,meta_json,created_at) VALUES(?,?,?,?,?)",
                      (sid, "assistant", reply, db.dump(meta), db.now()))
            title = s["title"]
            if not s["messages"] and title == agent["name"]:
                title = content[:24]
            c.execute("UPDATE chat_sessions SET title=?, model=?, updated_at=? WHERE id=?",
                      (title, model, db.now(), sid))
        return self.chat_get(sid)

    # ------------------------------------------------------------------
    # 剧本文件 / 一键剧本
    # ------------------------------------------------------------------

    def import_script_file(self, pid: str, data: bytes, filename: str, mode: str = "replace") -> dict:
        p = self._project_row(pid)
        text = decode_script_file(data, filename)
        if not text:
            raise WorkflowError("文件里没有读到文字")
        if mode == "append" and p["script_text"].strip():
            text = p["script_text"].rstrip() + "\n\n" + text
        return self.set_script(pid, text)

    async def generate_script(self, pid: str, idea: str, episodes: int = 3, mode: str = "replace",
                              seconds_per_ep: int = 90) -> dict:
        p = self._project_row(pid)
        st = self.settings(pid)
        if not (idea or "").strip():
            raise WorkflowError("先写一句灵感、故事梗概或贴一段小说")
        episodes = max(1, min(30, int(episodes)))
        llm.current_project.set(pid)
        text, source = await llm.generate_script(self.agg, st["text_model"], idea, episodes, p["style"],
                                                 3, seconds_per_ep, st["engine"])
        if mode == "append" and p["script_text"].strip():
            text = p["script_text"].rstrip() + "\n\n" + text
        out = self.set_script(pid, text)
        out["generated_source"] = source
        self.notify(pid, "success", f"一键剧本完成：{episodes} 集", f"来源：{'模型' if source == 'real' else '本地模板'}")
        return out

    # ------------------------------------------------------------------
    # 项目导出 / 导入 / 封面
    # ------------------------------------------------------------------

    def export_project(self, pid: str) -> tuple[str, str]:
        p = self._project_row(pid)
        root = os.path.abspath(media.root())
        tables: dict[str, list[dict]] = {}
        with db.tx() as c:
            for t, where in EXPORT_TABLES:
                rows = [dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE {where}", (pid,)).fetchall()]
                for r in rows:
                    for col in PATH_COLUMNS.get(t, []):
                        v = r.get(col) or ""
                        if v and os.path.abspath(v).startswith(root):
                            r[col] = "@media/" + os.path.relpath(os.path.abspath(v), root).replace(os.sep, "/")
                tables[t] = rows
        fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="export_")
        os.close(fd)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.json", json.dumps({"format": "daotu-workflow", "version": db.SCHEMA_VERSION,
                                                   "exported_at": db.now(), "tables": tables},
                                                  ensure_ascii=False, indent=1))
            pdir = os.path.join(root, pid)
            if os.path.isdir(pdir):
                for base, _, names in os.walk(pdir):
                    for n in names:
                        full = os.path.join(base, n)
                        z.write(full, "media/" + os.path.relpath(full, root).replace(os.sep, "/"),
                                compress_type=zipfile.ZIP_STORED)
        return tmp, f"{p['name']}.daotu.zip"

    def import_project(self, data: bytes) -> dict:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            meta = json.loads(z.read("project.json"))
        except (zipfile.BadZipFile, KeyError, ValueError):
            raise WorkflowError("不是有效的项目包（需要导出的 .daotu.zip）")
        if meta.get("format") != "daotu-workflow":
            raise WorkflowError("不是本工作台导出的项目包")
        tables = meta.get("tables") or {}
        proj = (tables.get("projects") or [{}])[0]
        pid = proj.get("id")
        if not pid:
            raise WorkflowError("项目包里没有项目")
        with db.tx() as c:
            if c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                raise WorkflowError("这个项目已经在本机了（同一项目不能重复导入），要覆盖请先删除本机的")
        root = os.path.abspath(media.root())
        for name in z.namelist():
            if name.startswith("media/") and not name.endswith("/"):
                rel = name[len("media/"):]
                dst = os.path.abspath(os.path.join(root, rel))
                if not dst.startswith(root + os.sep) or not rel.startswith(pid + "/"):
                    continue  # 防路径穿越：只解到本项目目录
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(name) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
        with db.tx() as c:
            for t, _ in EXPORT_TABLES:
                cols = {r["name"] for r in c.execute(f"PRAGMA table_info({t})")}
                for r in tables.get(t) or []:
                    r = {k: v for k, v in r.items() if k in cols}
                    for col in PATH_COLUMNS.get(t, []):
                        v = r.get(col) or ""
                        if v.startswith("@media/"):
                            r[col] = os.path.join(root, *v[len("@media/"):].split("/"))
                    if t == "projects":
                        r["name"] = r.get("name") or "导入的项目"
                    keys = list(r)
                    c.execute(f"INSERT INTO {t}({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
                              [r[k] for k in keys])
        self.notify(pid, "success", f"已导入项目：{proj.get('name')}")
        return self.get(pid)

    def set_cover(self, pid: str, data: bytes, filename: str) -> dict:
        self._project_row(pid)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            raise WorkflowError("封面只支持 png/jpg/webp")
        path = media.path_for(pid, "cover", f"cover{ext}")
        with open(path, "wb") as f:
            f.write(data)
        with db.tx() as c:
            c.execute("UPDATE projects SET cover_path=? WHERE id=?", (path, pid))
        return {"cover_url": media.url_of(path)}
