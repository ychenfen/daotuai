"""LibTV 远程 MCP 接入：OAuth 2.1（动态注册 + PKCE + 刷新）+ MCP Streamable HTTP 客户端 + 出片编排。

官方服务：https://mcp.liblib.tv/mcp（libtv-remote-mcp-gateway）。登录一次，令牌存本机 data/libtv_auth.json（不进仓库）。

出片流程（按官方操作契约）：
    doctor（准入）→ 定位/创建画布（每个工作台项目一张）→ 上传参考图成画布节点
    → search_models/get_model（实时模型与参数契约）→ generate_video(run=true，稳定幂等键)
    → task_status 轮询 → read_node/inspect_media 取结果视频 URL。
工具参数一律按服务端返回的 inputSchema 拼装：只填 schema 里有的字段；缺必填字段时报错并带上 schema，
方便对照修正，不靠猜。授权前看不到工具 schema，所以这层在首次授权后需要对照真实 schema 校准一次。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import uuid
from typing import Any

import httpx

log = logging.getLogger("agg.libtv")

MCP_URL = os.environ.get("LIBTV_MCP_URL", "https://mcp.liblib.tv/mcp")
ISSUER = os.environ.get("LIBTV_ISSUER", MCP_URL.rsplit("/mcp", 1)[0] + "/")
REDIRECT_URI = os.environ.get("LIBTV_REDIRECT_URI", "http://127.0.0.1:8010/libtv/callback")
SCOPES = "libtv:account:read libtv:project:read libtv:project:write libtv:media:write libtv:task:write libtv:export:write"
PROTOCOL_VERSION = "2025-06-18"
TOOL_TIMEOUT_S = float(os.environ.get("LIBTV_TOOL_TIMEOUT_S", "600"))

# 测试时可注入 ASGI transport（假 LibTV 服务），不联网
_transport: httpx.AsyncBaseTransport | None = None


class LibTVError(Exception):
    def __init__(self, message: str, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def _auth_path() -> str:
    db = os.environ.get("AGG_DB", os.path.join(os.path.dirname(__file__), "..", "data", "agg.db"))
    return os.path.join(os.path.dirname(os.path.abspath(db)), "libtv_auth.json")


def _load() -> dict:
    try:
        with open(_auth_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    p = _auth_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def authorized() -> bool:
    d = _load()
    return bool(d.get("access_token") or d.get("refresh_token"))


def _client(**kw) -> httpx.AsyncClient:
    proxy = os.environ.get("AGG_PROXY") or None
    if _transport is not None:
        return httpx.AsyncClient(transport=_transport, timeout=kw.get("timeout", 60), trust_env=False)
    return httpx.AsyncClient(timeout=kw.get("timeout", 60), trust_env=False, proxy=proxy)


# ======================================================================
# OAuth
# ======================================================================

async def _metadata() -> dict:
    async with _client() as c:
        r = await c.get(urllib.parse.urljoin(ISSUER, ".well-known/oauth-authorization-server"))
        r.raise_for_status()
        return r.json()


async def begin_login() -> str:
    """返回浏览器要打开的授权 URL。首次会做动态客户端注册。"""
    meta = await _metadata()
    d = _load()
    if not d.get("client_id") or d.get("redirect_uri") != REDIRECT_URI:
        async with _client() as c:
            r = await c.post(meta["registration_endpoint"], json={
                "client_name": "短剧工作台（内部自用）", "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none", "scope": SCOPES})
        if r.status_code >= 400:
            raise LibTVError(f"LibTV 客户端注册失败 {r.status_code}: {r.text[:300]}", r.status_code)
        reg = r.json()
        d.update(client_id=reg["client_id"], client_secret=reg.get("client_secret", ""), redirect_uri=REDIRECT_URI)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    d["pending"] = {"state": state, "verifier": verifier, "at": time.time()}
    _save(d)
    q = {"response_type": "code", "client_id": d["client_id"], "redirect_uri": REDIRECT_URI, "scope": SCOPES,
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": MCP_URL}
    return meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)


async def _token_request(form: dict) -> dict:
    meta = await _metadata()
    d = _load()
    form = {**form, "client_id": d["client_id"], "resource": MCP_URL}
    if d.get("client_secret"):
        form["client_secret"] = d["client_secret"]
    async with _client() as c:
        r = await c.post(meta["token_endpoint"], data=form)
    if r.status_code >= 400:
        raise LibTVError(f"LibTV 令牌请求失败 {r.status_code}: {r.text[:300]}", r.status_code)
    tok = r.json()
    d.update(access_token=tok["access_token"], token_type=tok.get("token_type", "Bearer"),
             expires_at=time.time() + float(tok.get("expires_in") or 3600) - 60,
             refresh_token=tok.get("refresh_token") or d.get("refresh_token", ""), scope=tok.get("scope", SCOPES))
    d.pop("pending", None)
    _save(d)
    return d


async def finish_login(code: str, state: str) -> dict:
    d = _load()
    p = d.get("pending") or {}
    if not p or p.get("state") != state:
        raise LibTVError("登录状态不匹配或已过期，请回到工作台重新点“连接 LibTV”", 400)
    if time.time() - float(p.get("at", 0)) > 900:
        raise LibTVError("登录超时（超过 15 分钟），请重新连接", 400)
    await _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                          "code_verifier": p["verifier"]})
    return status_local()


async def access_token(force_refresh: bool = False) -> str:
    d = _load()
    if not d.get("access_token") and not d.get("refresh_token"):
        raise LibTVError("还没连接 LibTV：在工作台“生成设置”里点“连接 LibTV”完成授权", 401)
    if force_refresh or float(d.get("expires_at", 0)) < time.time():
        if not d.get("refresh_token"):
            raise LibTVError("LibTV 登录已过期，请重新连接", 401)
        d = await _token_request({"grant_type": "refresh_token", "refresh_token": d["refresh_token"]})
    return d["access_token"]


async def logout() -> dict:
    d = _load()
    try:
        if d.get("refresh_token"):
            meta = await _metadata()
            async with _client() as c:
                await c.post(meta["revocation_endpoint"], data={"token": d["refresh_token"],
                                                                "client_id": d.get("client_id", "")})
    except Exception as e:  # 撤销失败不影响本地退出
        log.warning("revoke failed: %s", e)
    keep = {k: d[k] for k in ("client_id", "client_secret", "redirect_uri") if k in d}
    _save(keep)
    return {"authorized": False}


def status_local() -> dict:
    d = _load()
    return {"authorized": bool(d.get("access_token") or d.get("refresh_token")), "mcp_url": MCP_URL,
            "redirect_uri": REDIRECT_URI, "scope": d.get("scope", ""),
            "expires_in_s": max(0, int(float(d.get("expires_at", 0)) - time.time())) if d.get("access_token") else 0}


# ======================================================================
# MCP 客户端（Streamable HTTP）
# ======================================================================

def _parse_rpc(r: httpx.Response, req_id: int) -> dict:
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        msg = None
        for block in r.text.split("\n\n"):
            data = "\n".join(ln[5:].lstrip() for ln in block.splitlines() if ln.startswith("data:"))
            if not data:
                continue
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("id") == req_id:
                msg = obj
        if msg is None:
            raise LibTVError("MCP 响应里没有对应的结果", r.status_code)
        return msg
    return r.json()


class MCP:
    """一次会话：initialize → notifications/initialized → tools/list → tools/call。401 时自动刷新令牌重试一次。"""

    def __init__(self):
        self.session_id: str | None = None
        self.tools: dict[str, dict] = {}
        self._id = 0

    async def _rpc(self, method: str, params: dict | None = None, notify: bool = False, timeout: float = 60) -> dict:
        for attempt in (1, 2):
            token = await access_token(force_refresh=attempt == 2)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION}
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
            body: dict = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                body["params"] = params
            if not notify:
                self._id += 1
                body["id"] = self._id
            async with _client(timeout=timeout) as c:
                r = await c.post(MCP_URL, json=body, headers=headers)
            if r.status_code == 401 and attempt == 1:
                continue
            if r.status_code == 404 and self.session_id and method != "initialize":
                # 会话过期：重新初始化后再试
                self.session_id = None
                await self.initialize()
                continue
            if r.status_code >= 400:
                raise LibTVError(f"LibTV MCP {method} 失败 {r.status_code}: {r.text[:400]}", r.status_code)
            if r.headers.get("mcp-session-id"):
                self.session_id = r.headers["mcp-session-id"]
            if notify:
                return {}
            msg = _parse_rpc(r, body["id"])
            if msg.get("error"):
                raise LibTVError(f"LibTV MCP {method} 出错：{msg['error'].get('message')}", 400, msg["error"])
            return msg.get("result") or {}
        raise LibTVError(f"LibTV MCP {method} 鉴权失败，请重新连接", 401)

    async def initialize(self) -> dict:
        res = await self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": {"name": "daotu-workbench", "version": "0.3"}})
        await self._rpc("notifications/initialized", notify=True)
        return res

    async def list_tools(self) -> dict[str, dict]:
        if not self.session_id:
            await self.initialize()
        tools, cursor = {}, None
        while True:
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            for t in res.get("tools") or []:
                tools[t["name"]] = t
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        return tools

    async def call(self, name: str, args: dict, timeout: float | None = None) -> Any:
        if not self.tools:
            await self.list_tools()
        if name not in self.tools:
            raise LibTVError(f"LibTV 当前没有暴露工具 {name}（可用：{', '.join(sorted(self.tools))}）", 400)
        res = await self._rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout or TOOL_TIMEOUT_S)
        payload = res.get("structuredContent")
        if payload is None:
            texts = [c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"]
            joined = "\n".join(texts)
            try:
                payload = json.loads(joined) if joined.strip().startswith(("{", "[")) else {"text": joined}
            except ValueError:
                payload = {"text": joined}
        if res.get("isError"):
            raise LibTVError(f"LibTV 工具 {name} 报错：{json.dumps(payload, ensure_ascii=False)[:500]}", 400, payload)
        return payload

    def build_args(self, name: str, wanted: dict[str, Any]) -> dict:
        """按工具 inputSchema 取交集：wanted 的键可以是“候选名|候选名”，命中 schema 里第一个存在的字段。"""
        schema = (self.tools.get(name) or {}).get("inputSchema") or {}
        props = schema.get("properties") or {}
        out = {}
        for key, val in wanted.items():
            if val is None:
                continue
            for cand in key.split("|"):
                if cand in props:
                    out[cand] = val
                    break
        missing = [k for k in schema.get("required") or [] if k not in out]
        if missing:
            raise LibTVError(f"调用 {name} 缺少必填字段 {missing}，需要按真实 schema 校准适配层", 400,
                             {"schema": schema, "built": list(out)})
        return out


# ======================================================================
# 结果解析小工具（字段名以真实返回为准，这里尽量宽容）
# ======================================================================

def find(obj: Any, *keys: str) -> Any:
    """在嵌套 dict/list 里找第一个出现的键。"""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, "", []):
                return obj[k]
        for v in obj.values():
            hit = find(v, *keys)
            if hit not in (None, "", []):
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find(v, *keys)
            if hit not in (None, "", []):
                return hit
    return None


def find_video_url(obj: Any) -> str:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("http") and (".mp4" in v.lower() or "video" in k.lower()):
                return v
        for v in obj.values():
            u = find_video_url(v)
            if u:
                return u
    elif isinstance(obj, list):
        for v in obj:
            u = find_video_url(v)
            if u:
                return u
    return ""


async def doctor(m: MCP | None = None) -> dict:
    m = m or MCP()
    tools = await m.list_tools()
    info: dict = {"tools": sorted(tools)}
    if "doctor" in tools:
        info["doctor"] = await m.call("doctor", m.build_args("doctor", {}))
    return info


# ======================================================================
# 出片编排（provider kind = libtv）
# ======================================================================

def _decode_image(url: str) -> tuple[bytes, str]:
    head, _, b64 = url.partition(",")
    mime = head[5:].split(";")[0] if head.startswith("data:") else "image/png"
    return base64.b64decode(b64), mime


async def _fetch_bytes(url: str) -> tuple[bytes, str]:
    if url.startswith("data:"):
        return _decode_image(url)
    async with _client(timeout=120) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/png").split(";")[0]


async def ensure_canvas(m: MCP, name: str) -> str:
    """每个工作台项目一张 LibTV 画布：先按名字找，没有再按稳定幂等键创建。"""
    if "project_list" in m.tools:
        res = await m.call("project_list", m.build_args("project_list", {"keyword|query|search|name": name}))
        items = find(res, "items", "projects", "list", "data") or []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and (it.get("name") or it.get("title")) == name:
                uid = it.get("uuid") or it.get("projectId") or it.get("id")
                if uid:
                    return str(uid)
    res = await m.call("create_project", m.build_args("create_project", {
        "name|title|projectName": name,
        "idempotencyKey|idempotency_key": "canvas-" + hashlib.sha1(name.encode()).hexdigest()[:16]}))
    uid = find(res, "uuid", "projectId", "project_id", "id")
    if not uid:
        raise LibTVError(f"create_project 没返回画布 UUID：{json.dumps(res, ensure_ascii=False)[:300]}")
    if "target_project" in m.tools:
        await m.call("target_project", m.build_args("target_project", {"projectId|project_id|uuid": uid}))
    return str(uid)


async def upload_node(m: MCP, project_id: str, url: str, name: str) -> str:
    """上传一张参考图成画布节点：upload_ticket → PUT 分片 → upload_complete → upload_media。返回 nodeId。"""
    data, mime = await _fetch_bytes(url)
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
    ticket = await m.call("upload_ticket", m.build_args("upload_ticket", {
        "projectId|project_id": project_id, "fileName|filename|name": name + ext, "contentType|mimeType|mime": mime,
        "size|fileSize|bytes": len(data)}))
    parts = find(ticket, "parts", "uploadParts", "urls") or []
    if isinstance(parts, dict):
        parts = [parts]
    if not parts and find(ticket, "uploadUrl", "url", "putUrl"):
        parts = [{"url": find(ticket, "uploadUrl", "url", "putUrl"), "partNumber": 1}]
    etags = []
    chunk = max(1, -(-len(data) // max(1, len(parts))))
    async with _client(timeout=300) as c:
        for i, p in enumerate(parts):
            purl = p.get("url") or p.get("uploadUrl") if isinstance(p, dict) else str(p)
            r = await c.put(purl, content=data[i * chunk:(i + 1) * chunk], headers={"Content-Type": mime})
            if r.status_code >= 400:
                raise LibTVError(f"上传分片失败 {r.status_code}")
            etags.append({"partNumber": (p.get("partNumber") if isinstance(p, dict) else None) or i + 1,
                          "etag": r.headers.get("etag", "").strip('"')})
    done = await m.call("upload_complete", m.build_args("upload_complete", {
        "projectId|project_id": project_id, "uploadId|upload_id|ticketId": find(ticket, "uploadId", "upload_id", "ticketId"),
        "fileRef|file_ref": find(ticket, "fileRef", "file_ref"), "parts|etags": etags}))
    file_ref = find(done, "fileRef", "file_ref") or find(ticket, "fileRef", "file_ref")
    media = await m.call("upload_media", m.build_args("upload_media", {
        "projectId|project_id": project_id, "fileRef|file_ref": file_ref, "name|title": name}))
    node = find(media, "nodeId", "node_id", "id")
    if not node:
        raise LibTVError(f"upload_media 没返回节点 ID：{json.dumps(media, ensure_ascii=False)[:300]}")
    return str(node)


async def pick_model(m: MCP, query: str) -> tuple[str, dict]:
    """search_models → get_model：返回（精确模型标识, inputContract.parameterSchemas）。"""
    res = await m.call("search_models", m.build_args("search_models", {
        "query|keyword|q|name": query, "kind|type|category": "video"}))
    items = find(res, "items", "models", "list", "data") or []
    items = items if isinstance(items, list) else [items]
    best = next((x for x in items if isinstance(x, dict) and query.lower() in json.dumps(x, ensure_ascii=False).lower()),
                items[0] if items else None)
    if not best:
        raise LibTVError(f"LibTV 没找到模型：{query}")
    model_id = best.get("model") or best.get("id") or best.get("key") or best.get("name")
    detail = await m.call("get_model", m.build_args("get_model", {"model|modelId|model_id|key|id": model_id}))
    model_exact = find(detail, "model", "modelId", "id") or model_id
    params = find(detail, "parameterSchemas", "params", "parameters") or {}
    return str(model_exact), params if isinstance(params, dict) else {}


def fit_params(param_schemas: dict, duration: int, resolution: str, ratio: str) -> dict:
    """只填契约里存在的创作参数；取值尽量匹配枚举。"""
    out = {}
    for key, sch in param_schemas.items():
        k = key.lower()
        enum = (sch or {}).get("enum") if isinstance(sch, dict) else None
        if "duration" in k or "second" in k:
            val: Any = duration
        elif "resolution" in k or k in ("quality", "size"):
            val = resolution.lower() if enum and resolution.lower() in [str(e).lower() for e in enum] else resolution
        elif "ratio" in k or "aspect" in k:
            val = ratio
        elif k in ("count", "n"):
            val = 1
        else:
            continue
        if enum:
            match = next((e for e in enum if str(e).lower() == str(val).lower()), None)
            if match is None:
                continue
            val = match
        out[key] = val
    return out


async def generate(payload: dict, model_query: str, spec: str) -> dict:
    """完整出片：返回 {task_id, node_id, project_id}。"""
    m = MCP()
    await m.list_tools()
    info = {}
    if "doctor" in m.tools:
        info = await m.call("doctor", m.build_args("doctor", {}))
        adm = find(info, "executionAdmission", "canvasRun")
        if adm is False or (isinstance(adm, dict) and adm.get("allowed") is False):
            raise LibTVError(f"LibTV 当前不允许执行生成：{json.dumps(adm, ensure_ascii=False)[:300]}")
    project_id = await ensure_canvas(m, payload.get("project") or "短剧工作台")
    images = payload.get("images") or []
    nodes: list[tuple[str, str]] = []   # (role, nodeId)
    for i, img in enumerate(images, start=1):
        if img.get("role") == "reference_audio":
            continue
        nodes.append((img.get("role", "reference_image"), await upload_node(m, project_id, img["url"], f"ref{i}")))
    model_exact, pschemas = await pick_model(m, model_query)
    prompt = payload.get("prompt", "")
    ref_nodes = [n for r, n in nodes if r == "reference_image"]
    first = next((n for r, n in nodes if r == "first_frame"), None)
    for k, n in enumerate(ref_nodes, start=1):          # 我们的“图N”→ LibTV 的 {{Node id}}
        prompt = prompt.replace(f"图{k}（", f"{{{{Node {n}}}}}（").replace(f"图{k}：", f"{{{{Node {n}}}}}：")
    key = payload.get("idempotency_key") or "vid-" + uuid.uuid4().hex
    args = m.build_args("generate_video", {
        "projectId|project_id": project_id, "intent": "generate", "prompt": prompt, "model": model_exact,
        "params": fit_params(pschemas, int(round(float(payload.get("duration", 5)))), spec,
                             payload.get("aspect_ratio", "9:16")),
        "referenceImageNodes": ref_nodes or None, "startFrameNode": first, "run": True,
        "idempotencyKey|idempotency_key": key})
    res = await m.call("generate_video", args)
    return {"task_id": find(res, "taskId", "task_id"), "node_id": find(res, "nodeId", "node_id"),
            "project_id": project_id, "raw": res, "doctor": info}


async def poll(project_id: str, task_id: str | None, node_id: str | None) -> dict:
    m = MCP()
    await m.list_tools()
    st: Any = {}
    if task_id and "task_status" in m.tools:
        st = await m.call("task_status", m.build_args("task_status", {"projectId|project_id": project_id,
                                                                       "taskId|task_id": task_id}))
    state = str(find(st, "status", "state") or "").lower()
    url = find_video_url(st)
    if (not url or state in ("succeeded", "success", "completed", "done")) and node_id and "read_node" in m.tools:
        node = await m.call("read_node", m.build_args("read_node", {"projectId|project_id": project_id,
                                                                     "nodeId|node_id": node_id}))
        url = url or find_video_url(node)
        state = state or str(find(node, "status", "state") or "").lower()
    if state in ("failed", "error", "cancelled", "canceled"):
        return {"state": "failed", "error": str(find(st, "message", "error", "reason") or "LibTV 生成失败")[:300]}
    if url:
        secs = find(st, "duration", "seconds", "outputDuration")
        return {"state": "succeeded", "url": url, "seconds": float(secs) if secs else None}
    return {"state": "running"}


