"""假的 LibTV 远程 MCP 服务（测试用）：OAuth 动态注册 + PKCE + 刷新 + 撤销，MCP Streamable HTTP（含 SSE 响应、会话 ID），
上传分片、画布节点、模型契约、视频任务（先 running 后 succeeded）。只在进程内被 httpx ASGITransport 调用。"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid

import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

BASE = "http://libtv.test"
app = FastAPI()
S: dict = {"clients": {}, "codes": {}, "tokens": set(), "refresh": {}, "sessions": set(), "calls": [],
           "uploads": {}, "nodes": {}, "projects": {}, "tasks": {}, "polls": {}}

TOOLS = {
    "doctor": {"type": "object", "properties": {}},
    "project_list": {"type": "object", "properties": {"keyword": {"type": "string"}, "page": {"type": "integer"}}},
    "create_project": {"type": "object", "properties": {"name": {"type": "string"}, "idempotencyKey": {"type": "string"}},
                       "required": ["name", "idempotencyKey"]},
    "target_project": {"type": "object", "properties": {"projectId": {"type": "string"}}, "required": ["projectId"]},
    "upload_ticket": {"type": "object", "properties": {"projectId": {"type": "string"}, "fileName": {"type": "string"},
                                                        "contentType": {"type": "string"}, "size": {"type": "integer"}},
                      "required": ["projectId", "fileName", "size"]},
    "upload_complete": {"type": "object", "properties": {"projectId": {"type": "string"}, "uploadId": {"type": "string"},
                                                          "parts": {"type": "array"}}, "required": ["uploadId", "parts"]},
    "upload_media": {"type": "object", "properties": {"projectId": {"type": "string"}, "fileRef": {"type": "string"},
                                                       "name": {"type": "string"}}, "required": ["projectId", "fileRef"]},
    "search_models": {"type": "object", "properties": {"query": {"type": "string"}, "kind": {"type": "string"}}},
    "get_model": {"type": "object", "properties": {"model": {"type": "string"}}, "required": ["model"]},
    "generate_video": {"type": "object", "properties": {
        "projectId": {"type": "string"}, "intent": {"type": "string"}, "prompt": {"type": "string"},
        "model": {"type": "string"}, "params": {"type": "object"}, "referenceImageNodes": {"type": "array"},
        "startFrameNode": {"type": "string"}, "run": {"type": "boolean"}, "idempotencyKey": {"type": "string"}},
        "required": ["projectId", "intent", "prompt", "model", "idempotencyKey"]},
    "task_status": {"type": "object", "properties": {"projectId": {"type": "string"}, "taskId": {"type": "string"}},
                    "required": ["taskId"]},
    "read_node": {"type": "object", "properties": {"projectId": {"type": "string"}, "nodeId": {"type": "string"}},
                  "required": ["nodeId"]},
}


@app.get("/.well-known/oauth-authorization-server")
def meta():
    return {"issuer": BASE + "/", "authorization_endpoint": BASE + "/authorize", "token_endpoint": BASE + "/token",
            "registration_endpoint": BASE + "/register", "revocation_endpoint": BASE + "/revoke",
            "code_challenge_methods_supported": ["S256"]}


@app.post("/register")
async def register(request: Request):
    body = await request.json()
    cid = "cli_" + uuid.uuid4().hex[:8]
    S["clients"][cid] = body["redirect_uris"][0]
    return {"client_id": cid}


@app.get("/authorize")
def authorize(client_id: str, redirect_uri: str, state: str, code_challenge: str, code_challenge_method: str,
              response_type: str, scope: str = "", resource: str = ""):
    assert S["clients"].get(client_id) == redirect_uri and code_challenge_method == "S256" and response_type == "code"
    code = "code_" + uuid.uuid4().hex[:8]
    S["codes"][code] = {"challenge": code_challenge, "client": client_id, "resource": resource}
    return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)


def _issue():
    at, rt = "AT_" + uuid.uuid4().hex[:8], "RT_" + uuid.uuid4().hex[:8]
    S["tokens"].add(at)
    S["refresh"][rt] = True
    return {"access_token": at, "refresh_token": rt, "expires_in": 3600, "token_type": "Bearer"}


@app.post("/token")
async def token(request: Request):
    f = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode()).items()}
    if f["grant_type"] == "authorization_code":
        c = S["codes"].pop(f["code"], None)
        if not c:
            return JSONResponse({"error": "invalid_grant"}, 400)
        calc = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if calc != c["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "pkce"}, 400)
        return _issue()
    if f["grant_type"] == "refresh_token" and S["refresh"].pop(f["refresh_token"], None):
        S["calls"].append(("refresh", {}))
        return _issue()
    return JSONResponse({"error": "invalid_grant"}, 400)


@app.post("/revoke")
async def revoke(request: Request):
    f = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode()).items()}
    S["refresh"].pop(f.get("token", ""), None)
    return {}


@app.put("/upload/{uid}/{part}")
async def put_part(uid: str, part: int, request: Request):
    S["uploads"].setdefault(uid, {})[part] = await request.body()
    return Response(headers={"ETag": f'"etag-{uid}-{part}"'})


@app.get("/v/{name}")
def video(name: str):
    return Response(b"fake-mp4", media_type="video/mp4")


def _tool(name: str, a: dict) -> dict:
    S["calls"].append((name, a))
    if name == "doctor":
        return {"user": "测试账号", "executionAdmission": {"allowed": True}, "credits": 1000}
    if name == "project_list":
        return {"items": [{"uuid": k, "name": v} for k, v in S["projects"].items()]}
    if name == "create_project":
        pid = "cv_" + hashlib.sha1(a["idempotencyKey"].encode()).hexdigest()[:8]
        S["projects"][pid] = a["name"]
        return {"project": {"uuid": pid, "name": a["name"]}}
    if name == "target_project":
        return {"projectContext": "ctx", "uuid": a["projectId"]}
    if name == "upload_ticket":
        uid = "up_" + uuid.uuid4().hex[:6]
        return {"uploadId": uid, "fileRef": "ref_" + uid,
                "parts": [{"partNumber": 1, "url": f"{BASE}/upload/{uid}/1"}]}
    if name == "upload_complete":
        assert S["uploads"].get(a["uploadId"]), "没有收到上传的字节"
        return {"fileRef": "ref_" + a["uploadId"]}
    if name == "upload_media":
        nid = "n_" + uuid.uuid4().hex[:6]
        S["nodes"][nid] = a["fileRef"]
        return {"node": {"nodeId": nid}}
    if name == "search_models":
        return {"items": [{"model": "kling-4.0", "name": "Kling 4.0"}, {"model": "wan-3.0-video", "name": "Wan 3.0"},
                          {"model": "seedance-2.5", "name": "Seedance 2.5"}]}
    if name == "get_model":
        return {"model": a["model"], "inputContract": {"parameterSchemas": {
            "duration": {"type": "integer", "enum": [5, 10, 15]}, "resolution": {"type": "string", "enum": ["480p", "720p"]},
            "aspectRatio": {"type": "string", "enum": ["9:16", "16:9"]}}}}
    if name == "generate_video":
        for n in a.get("referenceImageNodes") or []:
            assert n in S["nodes"], f"引用了不存在的节点 {n}"
        tid = "t_" + hashlib.sha1(a["idempotencyKey"].encode()).hexdigest()[:8]
        S["tasks"][tid] = a
        return {"taskId": tid, "nodeId": "vn_" + tid}
    if name == "task_status":
        n = S["polls"][a["taskId"]] = S["polls"].get(a["taskId"], 0) + 1
        if n < 2:
            return {"status": "running"}
        return {"status": "succeeded", "result": {"videoUrl": f"{BASE}/v/{a['taskId']}.mp4", "duration": 5}}
    if name == "read_node":
        return {"node": {"id": a["nodeId"]}}
    raise ValueError(name)


@app.post("/mcp")
async def mcp(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] not in S["tokens"]:
        return JSONResponse({"error": "invalid_token"}, 401)
    body = await request.json()
    method, rid = body.get("method"), body.get("id")
    if method == "initialize":
        sid = "sess_" + uuid.uuid4().hex[:6]
        S["sessions"].add(sid)
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18",
                                                                       "capabilities": {"tools": {}}}},
                            headers={"Mcp-Session-Id": sid})
    if request.headers.get("mcp-session-id") not in S["sessions"]:
        return JSONResponse({"error": "no session"}, 404)
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        res = {"tools": [{"name": k, "description": k, "inputSchema": v} for k, v in TOOLS.items()]}
    elif method == "tools/call":
        p = body["params"]
        missing = [r for r in TOOLS[p["name"]].get("required", []) if r not in p["arguments"]]
        if missing:
            res = {"isError": True, "content": [{"type": "text", "text": json.dumps({"missing": missing})}]}
        else:
            out = _tool(p["name"], p["arguments"])
            res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}
        # 用 SSE 形式回结果（真实服务两种都可能）
        data = json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}, ensure_ascii=False)
        return Response(f"event: message\ndata: {data}\n\n", media_type="text/event-stream")
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no method"}})
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": res})
