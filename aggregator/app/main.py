"""统一网关：OpenAI 兼容的 chat（含 SSE 流式）/ image + 异步 video，全部带预扣与账本。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import billing, concurrency, db, providers as prov, router
from .config import models, providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agg")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    try:
        db.backup()
    except Exception:
        log.exception("账本库备份失败")
    log.info("aggregator ready: %d providers / %d models", len(providers()), len(models()))
    reaper = None
    if os.environ.get("AGG_REAP", "on") != "off":
        async def _loop():
            while True:
                await asyncio.sleep(float(os.environ.get("AGG_REAP_INTERVAL_S", "300")))
                try:
                    res = billing.reap_orphans(60)
                    if res["eligible"]:
                        log.warning("reaped %d orphan holds, refunded %.4f CNY",
                                    res["eligible"], res["refunded_rmb"])
                except Exception:
                    log.exception("reaper failed")
        reaper = asyncio.create_task(_loop())
    try:
        yield
    finally:
        if reaper:
            reaper.cancel()


app = FastAPI(title="Daotu Model Aggregator", version="0.3.0", lifespan=lifespan)
db.init()  # 导入即建表：ASGI 测试/无 lifespan 的宿主也能直接用


# ---------------- 基础设施 ----------------

@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or "req_" + uuid.uuid4().hex[:12]
    request.state.rid = rid
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    response.headers["X-Elapsed-Ms"] = str(round((time.perf_counter() - started) * 1000, 1))
    log.info("%s %s %s %s", rid, request.method, request.url.path, response.status_code)
    return response


def user_of(x_user_id: str | None) -> str:
    return (x_user_id or "demo").strip() or "demo"


def idem_get(key: str | None, user: str, raw: bytes) -> dict | None:
    if not key:
        return None
    h = hashlib.sha256(raw).hexdigest()[:32]
    with db.tx() as c:
        row = c.execute("SELECT * FROM idem WHERE key=?", (key,)).fetchone()
        if row and row["user_id"] == user and row["request_hash"] == h:
            return json.loads(row["response_json"])
    return None


def idem_put(key: str | None, user: str, raw: bytes, payload: dict) -> None:
    if not key:
        return
    h = hashlib.sha256(raw).hexdigest()[:32]
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO idem(key,user_id,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                  (key, user, h, db.dump(payload), db.now()))


def bad(msg: str, status: int = 400, code: str | None = None, **extra):
    err = {"message": msg}
    if code:
        err["code"] = code
    err.update(extra)
    return JSONResponse(status_code=status, content={"error": err})


def est_tokens(messages: list[dict]) -> int:
    """中文近似 2 字符 ≈ 1 token，宁高勿低；多模态消息里每张图按 1500 token 估（不能把 base64 长度当文字算）。"""
    chars, images = 0, 0
    for m in messages or []:
        content = (m or {}).get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "input_image", "image"):
                    images += 1
                elif isinstance(part, dict):
                    chars += len(str(part.get("text", "")))
        else:
            chars += len(str(content))
    return max(1, chars // 2 + images * 1500)


# ---------------- 基础 ----------------

@app.get("/healthz")
def healthz():
    return {"ok": True, "providers": len(providers()), "models": len(models()), "version": app.version}


@app.get("/v1/models")
def list_models():
    data = []
    plist = providers()
    for m in models().values():
        routes = [{"provider": r.provider, "api_id": r.api_id, "spec": r.spec,
                   "group": r.group, "weight": r.weight,
                   "live": bool(r.provider in plist and plist[r.provider].live)} for r in m.routes]
        data.append({
            "id": m.id, "type": m.type, "unit": m.unit, "price": m.price,
            "specs": m.specs, "modes": m.modes, "duration": m.duration,
            "live": any(r["live"] for r in routes),
            "routes": routes,
        })
    return {"object": "list", "data": data}


@app.post("/v1/quote")
async def quote_api(request: Request):
    body = await request.json()
    if body.get("model") not in models():
        raise HTTPException(404, f"unknown model {body.get('model')}")
    return billing.quote(body["model"], spec=body.get("spec"), seconds=body.get("seconds"),
                         images=int(body.get("n", 1)),
                         in_tokens=int(body.get("in_tokens", 0)),
                         out_tokens=int(body.get("out_tokens", 0)))


# ---------------- 钱包 ----------------

@app.get("/v1/wallet")
def wallet_get(x_user_id: str | None = Header(default=None)):
    return billing.wallet_summary(user_of(x_user_id))


@app.post("/v1/wallet/topup")
async def wallet_topup(request: Request, x_user_id: str | None = Header(default=None)):
    u = user_of(x_user_id)
    body = await request.json()
    amount = float(body.get("amount_rmb", 0))
    if amount <= 0:
        raise HTTPException(400, "amount_rmb must be > 0")
    return {"user": u, "balance_rmb": billing.topup(u, amount)}


@app.get("/v1/ledger")
def ledger_get(limit: int = 50, x_user_id: str | None = Header(default=None)):
    return {"items": billing.ledger(user_of(x_user_id), limit)}


# ---------------- 文本 ----------------

def _chat_payload(route, messages, max_out, body):
    return {"model": route.api_id, "messages": messages, "max_tokens": max_out,
            "temperature": body.get("temperature", 0.7)}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def _chat_stream(user: str, body: dict, messages: list[dict], max_out: int, est_in: int):
    model_id = body["model"]
    q = billing.quote(model_id, in_tokens=est_in, out_tokens=max_out)
    try:
        hold = billing.reserve(user, q["rmb"], ref=f"chat-stream:{model_id}", model=model_id)
    except billing.InsufficientFunds as e:
        yield _sse({"error": {"message": str(e), "code": "insufficient_funds", "quote": q}})
        yield "data: [DONE]\n\n"
        return

    collected: list[str] = []
    usage = {"prompt_tokens": est_in, "completion_tokens": 0}
    route_used = None
    try:
        async for route, event in router.stream(
                model_id, None, lambda r: prov.chat_stream(providers()[r.provider], r.api_id,
                                                           _chat_payload(r, messages, max_out, body))):
            route_used = route
            if event.get("delta"):
                collected.append(event["delta"])
                yield _sse({"object": "chat.completion.chunk", "model": model_id,
                            "choices": [{"delta": {"content": event["delta"]}}]})
            if event.get("done"):
                usage = event.get("usage") or usage
    except router.RouterExhausted as e:
        if not collected:
            billing.refund(hold, "stream failed before first token")
            yield _sse({"error": {"message": str(e), "code": "upstream_failed"}})
            yield "data: [DONE]\n\n"
            return
        log.warning("stream broke after %d chunks: %s", len(collected), e)

    if not usage.get("completion_tokens"):
        usage["completion_tokens"] = max(1, len("".join(collected)) // 2)
    actual = billing.quote(model_id, in_tokens=usage.get("prompt_tokens", 0),
                           out_tokens=usage.get("completion_tokens", 0), route=route_used)
    cost = billing.quote(model_id, in_tokens=usage.get("prompt_tokens", 0),
                         out_tokens=usage.get("completion_tokens", 0), route=route_used, basis="cost")["rmb"]
    if route_used:
        billing.attach_provider(hold, route_used.provider, cost)
    settled = billing.settle(hold, actual["rmb"], cost_rmb=cost,
                             detail=f"chat-stream:{model_id}:{route_used.provider if route_used else '?'}")
    yield _sse({"object": "chat.completion.final", "usage": usage,
                "billing": {"charged_rmb": settled["charged_rmb"],
                            "points": billing.rmb_to_points(settled["charged_rmb"]),
                            "balance_rmb": settled["balance_rmb"], "hold_id": hold},
                "routing": {"provider": route_used.provider, "api_id": route_used.api_id}})
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, x_user_id: str | None = Header(default=None),
                           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    raw = await request.body()
    u = user_of(x_user_id)
    body: dict[str, Any] = json.loads(raw or b"{}")
    if body.get("model") not in models():
        return bad(f"unknown model {body.get('model')}", 404, code="model_not_found")
    if models()[body["model"]].type != "chat":
        return bad(f"not a chat model: {body['model']}", 400, code="wrong_model_type")

    messages = body.get("messages") or []
    if not messages:
        return bad("messages is required", 400, code="invalid_request")
    max_out = int(body.get("max_tokens") or 512)

    if body.get("stream"):
        est_in = est_tokens(messages)
        return StreamingResponse(_chat_stream(u, body, messages, max_out, est_in),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    cached = idem_get(idempotency_key, u, raw)
    if cached:
        return cached

    model_id = body["model"]
    est_in = est_tokens(messages)
    q = billing.quote(model_id, in_tokens=est_in, out_tokens=max_out)
    try:
        hold = billing.reserve(u, q["rmb"], ref=f"chat:{model_id}", model=model_id)
    except billing.InsufficientFunds as e:
        return bad(str(e), 402, code="insufficient_funds", quote=q)

    try:
        route, result, meta = await router.call(
            model_id, None, lambda r: prov.chat(providers()[r.provider], r.api_id,
                                                _chat_payload(r, messages, max_out, body)))
    except router.RouterExhausted as e:
        billing.refund(hold, "chat failed")
        return bad(str(e), 502, code="upstream_failed", attempts=e.attempts)

    usage = result.get("usage") or {}
    actual = billing.quote(model_id, in_tokens=usage.get("prompt_tokens", 0),
                           out_tokens=usage.get("completion_tokens", 0), route=route)
    cost = billing.quote(model_id, in_tokens=usage.get("prompt_tokens", 0),
                         out_tokens=usage.get("completion_tokens", 0), route=route, basis="cost")["rmb"]
    billing.attach_provider(hold, route.provider, cost)
    settled = billing.settle(hold, actual["rmb"], detail=f"chat:{model_id}:{route.provider}", cost_rmb=cost)

    payload = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:16],
        "object": "chat.completion",
        "model": model_id,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result.get("content", "")},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                  "completion_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)},
        "billing": {"quoted_rmb": q["rmb"], "charged_rmb": settled["charged_rmb"],
                    "points": billing.rmb_to_points(settled["charged_rmb"]),
                    "balance_rmb": settled["balance_rmb"], "hold_id": hold},
        "routing": {"provider": route.provider, "api_id": route.api_id, "group": route.group,
                    "attempt": meta["attempt"], "latency_ms": meta["latency_ms"]},
    }
    idem_put(idempotency_key, u, raw, payload)
    return payload


# ---------------- 图像 ----------------

def _as_list(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v]) if x]


@app.post("/v1/images/generations")
async def image_generations(request: Request, x_user_id: str | None = Header(default=None),
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    raw = await request.body()
    u = user_of(x_user_id)
    cached = idem_get(idempotency_key, u, raw)
    if cached:
        return cached

    body = json.loads(raw or b"{}")
    model_id = body.get("model")
    if model_id not in models() or models()[model_id].type != "image":
        return bad(f"not an image model: {model_id}", 404, code="model_not_found")

    n = int(body.get("n", 1))
    q = billing.quote(model_id, images=n)
    try:
        hold = billing.reserve(u, q["rmb"], ref=f"image:{model_id}", model=model_id)
    except billing.InsufficientFunds as e:
        return bad(str(e), 402, code="insufficient_funds", quote=q)

    try:
        route, result, meta = await router.call(
            model_id, None, lambda r: prov.image(providers()[r.provider], r.api_id,
                                                 {"model": r.api_id, "prompt": body.get("prompt", ""),
                                                  "n": n, "size": body.get("size", "1024x1024"),
                                                  "image": _as_list(body.get("image"))}))
    except router.RouterExhausted as e:
        billing.refund(hold, "image failed")
        return bad(str(e), 502, code="upstream_failed", attempts=e.attempts)

    produced = len(result.get("images") or []) or n
    actual = billing.quote(model_id, images=produced, route=route)
    cost = billing.quote(model_id, images=produced, route=route, basis="cost")["rmb"]
    billing.attach_provider(hold, route.provider, cost)
    settled = billing.settle(hold, actual["rmb"], detail=f"image:{model_id}:{route.provider}", cost_rmb=cost)

    payload = {
        "created": int(time.time()),
        "data": [{"url": url} for url in result.get("images", [])],
        "billing": {"quoted_rmb": q["rmb"], "charged_rmb": settled["charged_rmb"],
                    "points": billing.rmb_to_points(settled["charged_rmb"]),
                    "balance_rmb": settled["balance_rmb"], "hold_id": hold},
        "routing": {"provider": route.provider, "api_id": route.api_id,
                    "attempt": meta["attempt"], "latency_ms": meta["latency_ms"]},
    }
    idem_put(idempotency_key, u, raw, payload)
    return payload


# ---------------- 语音合成 ----------------

@app.post("/v1/audio/speech")
async def audio_speech(request: Request, x_user_id: str | None = Header(default=None),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """{model, input, voice, language_type?} → {url, characters, billing}。按字符计费、先预扣后结算。"""
    raw = await request.body()
    u = user_of(x_user_id)
    cached = idem_get(idempotency_key, u, raw)
    if cached:
        return cached
    body = json.loads(raw or b"{}")
    model_id = body.get("model")
    if model_id not in models() or models()[model_id].type != "tts":
        return bad(f"not a tts model: {model_id}", 404, code="model_not_found")
    text = str(body.get("input") or "").strip()
    if not text:
        return bad("input is required", 400, code="invalid_request")
    if len(text) > 600:
        return bad("单次最多 600 字", 400, code="too_long")
    q = billing.quote(model_id, chars=len(text))
    try:
        hold = billing.reserve(u, q["rmb"], ref=f"tts:{model_id}", model=model_id)
    except billing.InsufficientFunds as e:
        return bad(str(e), 402, code="insufficient_funds", quote=q)
    try:
        route, result, meta = await router.call(
            model_id, None, lambda r: prov.tts(providers()[r.provider], r.api_id,
                                               {"text": text, "voice": body.get("voice") or "Cherry",
                                                "language_type": body.get("language_type") or "Chinese"}))
    except router.RouterExhausted as e:
        billing.refund(hold, "tts failed")
        return bad(str(e), 502, code="upstream_failed", attempts=e.attempts)
    n = int(result.get("characters") or len(text))
    actual = billing.quote(model_id, chars=n, route=route)
    cost = billing.quote(model_id, chars=n, route=route, basis="cost")["rmb"]
    billing.attach_provider(hold, route.provider, cost)
    settled = billing.settle(hold, actual["rmb"], detail=f"tts:{model_id}:{route.provider}", cost_rmb=cost)
    payload = {"url": result.get("url"), "characters": n, "voice": body.get("voice"),
               "billing": {"quoted_rmb": q["rmb"], "charged_rmb": settled["charged_rmb"], "hold_id": hold},
               "routing": {"provider": route.provider, "api_id": route.api_id}}
    idem_put(idempotency_key, u, raw, payload)
    return payload


# ---------------- 视频（异步） ----------------

@app.post("/v1/videos/generations")
async def video_generations(request: Request, x_user_id: str | None = Header(default=None),
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    raw = await request.body()
    u = user_of(x_user_id)
    cached = idem_get(idempotency_key, u, raw)
    if cached:
        return cached

    body = json.loads(raw or b"{}")
    model_id = body.get("model")
    if model_id not in models() or models()[model_id].type not in ("video", "video_edit"):
        return bad(f"not a video model: {model_id}", 404, code="model_not_found")

    m = models()[model_id]
    spec = (body.get("resolution") or "480P").upper()
    if spec not in (m.specs or [spec]):
        return bad(f"unsupported resolution {spec} for {model_id}", 400, code="bad_resolution", specs=m.specs)

    seconds = float(body.get("duration") or 5)
    d = m.duration or {}
    if d:
        if seconds < d.get("min", 1) or seconds > d.get("max", 30):
            return bad(f"duration must be {d.get('min')}-{d.get('max')}s", 400, code="bad_duration")

    q = billing.quote(model_id, spec=spec, seconds=seconds)
    try:
        hold = billing.reserve(u, q["rmb"], ref=f"video:{model_id}:{spec}", model=model_id)
    except billing.InsufficientFunds as e:
        return bad(str(e), 402, code="insufficient_funds", quote=q)

    try:
        route, result, meta = await router.call(
            model_id, spec, lambda r: prov.submit_video(providers()[r.provider], r.api_id, r.spec or spec, body))
    except router.RouterExhausted as e:
        billing.refund(hold, "video submit failed")
        return bad(str(e), 502, code="upstream_failed", attempts=e.attempts)

    est_cost = billing.quote(model_id, spec=spec, seconds=seconds, route=route, basis="cost")["rmb"]
    billing.attach_provider(hold, route.provider, est_cost)

    task_id = "vtask_" + uuid.uuid4().hex[:16]
    with db.tx() as c:
        c.execute("""INSERT INTO tasks(task_id,user_id,model,provider,api_id,spec,status,hold_id,
                     request_json,result_json,error,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (task_id, u, model_id, route.provider, route.api_id, spec, "submitted", hold,
                   db.dump(body), db.dump({"provider_task_id": result["provider_task_id"]}), "", db.now(), db.now()))

    payload = {"id": task_id, "object": "video.task", "status": "submitted", "model": model_id,
               "resolution": spec, "duration": seconds,
               "billing": {"quoted_rmb": q["rmb"], "points": billing.rmb_to_points(q["rmb"]), "hold_id": hold},
               "routing": {"provider": route.provider, "api_id": route.api_id,
                           "group": route.group, "attempt": meta["attempt"]}}
    idem_put(idempotency_key, u, raw, payload)
    return payload


def _load_task(task_id: str, user: str):
    with db.tx() as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=? AND user_id=?", (task_id, user)).fetchone()
    return row


async def _advance_task(row) -> dict:
    task_id = row["task_id"]
    if row["status"] in ("succeeded", "failed"):
        return {"id": task_id, "status": row["status"], "result": json.loads(row["result_json"] or "{}")}

    provider_task_id = json.loads(row["result_json"] or "{}").get("provider_task_id")
    info = await prov.poll_video(providers()[row["provider"]], provider_task_id)
    state = info.get("state", "running")

    if state == "succeeded":
        # 优先用上游返回的实际输出秒数结算
        actual_seconds = float((info.get("usage") or {}).get("output_seconds") or info.get("seconds") or 5)
        actual = billing.quote(row["model"], spec=row["spec"], seconds=actual_seconds)
        cost_per_s = billing.quote(row["model"], spec=row["spec"], seconds=1, basis="cost")["rmb"]
        cost = round(cost_per_s * actual_seconds, 6)
        settled = billing.settle(row["hold_id"], actual["rmb"], cost_rmb=cost,
                                 detail=f"video:{row['model']}:{row['provider']}")
        with db.tx() as c:
            c.execute("UPDATE tasks SET status='succeeded', result_json=?, updated_at=? WHERE task_id=?",
                      (db.dump({"url": info.get("url"), "seconds": actual_seconds, "billing": settled}),
                       db.now(), task_id))
        return {"id": task_id, "status": "succeeded", "url": info.get("url"),
                "seconds": actual_seconds, "billing": settled}

    if state == "failed":
        refunded = billing.refund(row["hold_id"], "video failed")
        with db.tx() as c:
            c.execute("UPDATE tasks SET status='failed', error=?, updated_at=? WHERE task_id=?",
                      ("upstream failed", db.now(), task_id))
        return {"id": task_id, "status": "failed", "refund": refunded}

    return {"id": task_id, "status": "running"}


@app.get("/v1/videos/{task_id}")
async def video_status(task_id: str, x_user_id: str | None = Header(default=None)):
    row = _load_task(task_id, user_of(x_user_id))
    if not row:
        raise HTTPException(404, "task not found")
    return await _advance_task(row)


@app.get("/v1/videos/{task_id}/wait")
async def video_wait(task_id: str, timeout_s: float = 30.0, interval_s: float = 0.5,
                     x_user_id: str | None = Header(default=None)):
    user = user_of(x_user_id)
    deadline = time.time() + max(0.0, min(timeout_s, 120.0))
    last: dict = {"id": task_id, "status": "running"}
    while time.time() < deadline:
        row = _load_task(task_id, user)
        if not row:
            raise HTTPException(404, "task not found")
        last = await _advance_task(row)
        if last["status"] in ("succeeded", "failed"):
            return last
        await asyncio.sleep(interval_s)
    return {**last, "timed_out": True}


@app.post("/v1/videos/{task_id}/cancel")
async def video_cancel(task_id: str, x_user_id: str | None = Header(default=None)):
    """本地取消并退回预扣。注意：上游任务可能仍在跑，这里只保证我方账务不亏。"""
    row = _load_task(task_id, user_of(x_user_id))
    if not row:
        raise HTTPException(404, "task not found")
    if row["status"] in ("succeeded", "failed", "cancelled"):
        return {"id": task_id, "status": row["status"], "refund": None}
    refunded = billing.refund(row["hold_id"], "client cancelled")
    with db.tx() as c:
        c.execute("UPDATE tasks SET status='cancelled', error=?, updated_at=? WHERE task_id=?",
                  ("cancelled by client", db.now(), task_id))
    return {"id": task_id, "status": "cancelled", "refund": refunded}


# ---------------- LibTV 连接（OAuth 登录在浏览器里完成） ----------------

from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402

from . import libtv  # noqa: E402


@app.get("/libtv/status")
def libtv_status():
    return libtv.status_local()


@app.get("/libtv/login")
async def libtv_login():
    try:
        return RedirectResponse(await libtv.begin_login())
    except Exception as e:  # 网络/注册失败给出可读页面
        return HTMLResponse(f"<h3>连接 LibTV 失败</h3><p>{str(e)[:500]}</p>", status_code=502)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                        f"<body style='font-family:sans-serif;padding:40px;background:#0d1620;color:#e6edf3'>"
                        f"<h2>{title}</h2><p>{body}</p></body>")


@app.get("/libtv/callback")
async def libtv_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return _page("LibTV 授权没有完成", f"{error}：{error_description}")
    try:
        await libtv.finish_login(code, state)
    except libtv.LibTVError as e:
        return _page("LibTV 授权失败", str(e))
    return _page("LibTV 已连接 ✓", "可以关掉这个页面，回到工作台。视频模型里选 LibTV 开头的模型即可用 LibTV 出片。")


@app.post("/libtv/logout")
async def libtv_logout():
    return await libtv.logout()


@app.get("/libtv/doctor")
async def libtv_doctor():
    """只读：列出 LibTV 暴露的工具，并调用 doctor（身份、连接、执行准入）。"""
    try:
        return {"ok": True, **(await libtv.doctor())}
    except libtv.LibTVError as e:
        return JSONResponse(status_code=e.status or 502, content={"ok": False, "error": str(e)})


@app.get("/libtv/tools")
async def libtv_tools():
    """只读：工具的完整 inputSchema，用于对照校准适配层。"""
    m = libtv.MCP()
    try:
        tools = await m.list_tools()
    except libtv.LibTVError as e:
        return JSONResponse(status_code=e.status or 502, content={"error": str(e)})
    return {"tools": [{"name": t["name"], "description": t.get("description", "")[:500],
                       "inputSchema": t.get("inputSchema")} for t in tools.values()]}


# ---------------- 运维 ----------------

@app.get("/admin/health")
def admin_health():
    return {"providers": router.snapshot(), "concurrency": concurrency.snapshot()}


@app.get("/admin/stats")
def admin_stats():
    with db.tx() as c:
        tasks = {r["status"]: int(r["n"]) for r in c.execute(
            "SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()}
        holds = {r["status"]: int(r["n"]) for r in c.execute(
            "SELECT status, COUNT(*) n FROM holds GROUP BY status").fetchall()}
        calls = c.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]
    return {"tasks": tasks, "holds": holds, "ledger_entries": calls,
            "providers": router.snapshot(), "concurrency": concurrency.snapshot()}


@app.get("/admin/reconcile")
def admin_reconcile(orphan_minutes: int = 30):
    return billing.reconcile(orphan_minutes)


@app.get("/admin/report")
def admin_report(days: int = 7):
    return billing.report(days)


@app.post("/admin/reconcile")
async def admin_reconcile(request: Request, account: str = "aliyun"):
    """上传费用中心导出的账单 CSV（请求体就是文件内容），按天对比我们账本里的估算成本。"""
    from . import reconcile
    try:
        return reconcile.reconcile(await request.body(), account)
    except reconcile.BillError as e:
        return bad(str(e), 400, code="bad_bill")


@app.post("/admin/reap")
def admin_reap(older_than_minutes: int = 60, dry_run: bool = False):
    return billing.reap_orphans(older_than_minutes, dry_run)
