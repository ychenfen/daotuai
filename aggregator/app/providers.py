"""上游适配层：OpenAI 兼容 / 火山方舟 / mock。

统一出口：chat()、chat_stream()、image()、submit_video()、poll_video()。
所有异常收敛成 ProviderError，方便路由层做降级。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import AsyncIterator

import httpx

from .config import Provider


class ProviderError(Exception):
    def __init__(self, provider: str, status: int | None, message: str, retryable: bool = True):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status = status
        self.retryable = retryable


def _mock_disabled(provider: Provider) -> bool:
    """MOCK_FAIL=dmxapi,a6_api 用来演示降级。"""
    bad = os.environ.get("MOCK_FAIL", "")
    return provider.id in {x.strip() for x in bad.split(",") if x.strip()}


def _headers(provider: Provider) -> dict:
    return {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}


def _client(provider: Provider) -> httpx.AsyncClient:
    """统一客户端：显式代理，且不信 env。

    Windows 上常见 NO_PROXY 里带 `[::1]` 这类写法，httpx 解析会直接抛
    InvalidURL(':1]')，所以一律 trust_env=False，代理走 provider.proxy 或 AGG_PROXY。
    """
    proxy = provider.proxy or os.environ.get("AGG_PROXY") or None
    return httpx.AsyncClient(timeout=provider.timeout_s, trust_env=False, proxy=proxy)


async def _post(provider: Provider, path: str, payload: dict, extra_headers: dict | None = None) -> dict:
    url = provider.base_url.rstrip("/") + path
    try:
        async with _client(provider) as client:
            r = await client.post(url, json=payload, headers={**_headers(provider), **(extra_headers or {})})
    except httpx.HTTPError as e:
        raise ProviderError(provider.id, None, f"network: {e}") from e
    if r.status_code >= 400:
        raise ProviderError(provider.id, r.status_code, r.text[:300],
                            retryable=r.status_code >= 500 or r.status_code in (408, 429))
    return r.json()


async def _get(provider: Provider, path: str) -> dict:
    url = provider.base_url.rstrip("/") + path
    try:
        async with _client(provider) as client:
            r = await client.get(url, headers=_headers(provider))
    except httpx.HTTPError as e:
        raise ProviderError(provider.id, None, f"network: {e}") from e
    if r.status_code >= 400:
        raise ProviderError(provider.id, r.status_code, r.text[:300])
    return r.json()


# ---------------- chat ----------------

async def chat(provider: Provider, api_id: str, payload: dict) -> dict:
    if _mock_disabled(provider):
        raise ProviderError(provider.id, 502, "mock injected failure")
    if not provider.live:
        await asyncio.sleep(0.01)
        last = (payload.get("messages") or [{}])[-1].get("content", "")
        n_img = 0
        if isinstance(last, list):   # 图文混合消息：只取文字部分，图片按 1500 token 计
            n_img = sum(1 for p in last if isinstance(p, dict) and p.get("type") in ("image_url", "input_image"))
            last = " ".join(str(p.get("text", "")) for p in last if isinstance(p, dict) and p.get("type") == "text")
        text = f"[mock:{provider.id}] " + last[:120]
        return {"content": text,
                "usage": {"prompt_tokens": max(1, len(last) // 2 + n_img * 1500),
                          "completion_tokens": max(1, len(text) // 2)},
                "provider_model": api_id}
    body = {"model": api_id, **{k: v for k, v in payload.items() if k != "model"}}
    data = await _post(provider, "/chat/completions", body)
    choice = (data.get("choices") or [{}])[0]
    usage = data.get("usage") or {}
    return {
        "content": (choice.get("message") or {}).get("content", ""),
        "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                  "completion_tokens": usage.get("completion_tokens", 0)},
        "provider_model": data.get("model", api_id),
    }


MOCK_CHUNKS = ["[mock] ", "这是", "流式", "输出", "。"]


async def chat_stream(provider: Provider, api_id: str, payload: dict) -> AsyncIterator[dict]:
    """产出 {"delta": str} 若干次，最后 {"done": True, "usage": {...}}。"""
    if _mock_disabled(provider):
        raise ProviderError(provider.id, 502, "mock injected failure")
    if not provider.live:
        for chunk in MOCK_CHUNKS:
            await asyncio.sleep(0.005)
            yield {"delta": chunk}
        prompt_tokens = max(1, len(str((payload.get("messages") or [{}])[-1].get("content", ""))) // 2)
        yield {"done": True,
               "usage": {"prompt_tokens": prompt_tokens,
                         "completion_tokens": max(1, len("".join(MOCK_CHUNKS)) // 2)},
               "provider_model": api_id}
        return

    url = provider.base_url.rstrip("/") + "/chat/completions"
    body = {"model": api_id, **{k: v for k, v in payload.items() if k != "model"}, "stream": True,
            "stream_options": {"include_usage": True}}
    started = time.perf_counter()
    try:
        async with _client(provider) as client:
            async with client.stream("POST", url, json=body, headers=_headers(provider)) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode("utf-8", "ignore")[:300]
                    raise ProviderError(provider.id, r.status_code, text,
                                        retryable=r.status_code >= 500 or r.status_code in (408, 429))
                usage = {"prompt_tokens": 0, "completion_tokens": 0}
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = {"prompt_tokens": obj["usage"].get("prompt_tokens", 0),
                                 "completion_tokens": obj["usage"].get("completion_tokens", 0)}
                    for ch in obj.get("choices") or []:
                        delta = (ch.get("delta") or {}).get("content")
                        if delta:
                            yield {"delta": delta}
    except httpx.HTTPError as e:
        raise ProviderError(provider.id, None, f"network: {e}") from e
    finally:
        _ = time.perf_counter() - started
    yield {"done": True, "usage": usage, "provider_model": api_id}


# ---------------- image ----------------

async def image(provider: Provider, api_id: str, payload: dict) -> dict:
    if _mock_disabled(provider):
        raise ProviderError(provider.id, 502, "mock injected failure")
    if not provider.live:
        await asyncio.sleep(0.01)
        n = int(payload.get("n", 1))
        return {"images": [f"https://mock.local/{provider.id}/{uuid.uuid4().hex[:8]}.png" for _ in range(n)],
                "provider_model": api_id, "usage": {"images": n}}
    body = {"model": api_id, "prompt": payload.get("prompt", ""), "n": int(payload.get("n", 1)),
            "size": payload.get("size") or "1024x1024", "response_format": "url"}
    refs = [u for u in (payload.get("image") or []) if u]
    if provider.kind == "ark":
        body.pop("n", None)
        body["watermark"] = False
        if refs:
            body["image"] = refs if len(refs) > 1 else refs[0]
    elif refs:
        body["image"] = refs
    data = await _post(provider, "/images/generations", body)
    images = []
    for d in data.get("data", []):
        if d.get("url"):
            images.append(d["url"])
        elif d.get("b64_json"):
            images.append("data:image/png;base64," + d["b64_json"])
    if not images:
        raise ProviderError(provider.id, None, f"no image in response: {str(data)[:200]}")
    return {"images": images, "provider_model": data.get("model", api_id),
            "usage": {"images": len(images)}}


# ---------------- tts ----------------

async def tts(provider: Provider, api_id: str, payload: dict) -> dict:
    if _mock_disabled(provider):
        raise ProviderError(provider.id, 502, "mock injected failure")
    text = payload.get("text", "")
    if not provider.live:
        await asyncio.sleep(0.01)
        secs = round(max(0.6, len(text) * 0.22), 2)   # 中文约 4.5 字/秒
        return {"url": f"https://mock.local/tts/{uuid.uuid4().hex[:8]}.wav?sec={secs}", "characters": len(text)}
    if provider.kind == "dashscope_tts":
        data = await _post(provider, "/services/aigc/multimodal-generation/generation",
                           {"model": api_id, "input": {"text": text, "voice": payload.get("voice") or "Cherry",
                                                       "language_type": payload.get("language_type") or "Chinese"}})
        url = ((data.get("output") or {}).get("audio") or {}).get("url")
        if not url:
            raise ProviderError(provider.id, None, f"no audio in response: {str(data)[:200]}")
        return {"url": url, "characters": (data.get("usage") or {}).get("characters") or len(text)}
    data = await _post(provider, "/audio/speech", {"model": api_id, "input": text, "voice": payload.get("voice")})
    return {"url": data.get("url"), "characters": len(text)}


# ---------------- video ----------------

VIDEO_IMAGE_ROLES = ("first_frame", "last_frame", "reference_image", "reference_audio")


def normalize_images(raw) -> list[dict]:
    """参考图统一成 [{"url", "role"}]；字符串默认当 reference_image。"""
    out = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict) or not item.get("url"):
            continue
        role = item.get("role") if item.get("role") in VIDEO_IMAGE_ROLES else "reference_image"
        out.append({"url": str(item["url"]), "role": role})
    return out[:9]


async def submit_video(provider: Provider, api_id: str, spec: str, payload: dict) -> dict:
    if _mock_disabled(provider):
        raise ProviderError(provider.id, 502, "mock injected failure")
    images = normalize_images(payload.get("images"))
    if not provider.live:
        await asyncio.sleep(0.01)
        # 把时长编进假任务号，轮询时按请求时长"出片"，便于下游测拼接
        dur = int(round(float(payload.get("duration", 5))))
        return {"provider_task_id": f"mock-{dur}-{uuid.uuid4().hex[:12]}", "state": "submitted"}
    if provider.kind == "ark":
        content: list[dict] = [{"type": "text", "text": payload.get("prompt", "")}]
        for img in images:
            if img["role"] == "reference_audio":
                content.append({"type": "audio_url", "audio_url": {"url": img["url"]}, "role": "reference_audio"})
            else:
                content.append({"type": "image_url", "image_url": {"url": img["url"]}, "role": img["role"]})
        if payload.get("video"):
            content.append({"type": "video_url", "video_url": {"url": payload["video"]}, "role": "reference_video"})
        body = {
            "model": api_id,
            "content": content,
            "duration": int(round(float(payload.get("duration", 5)))),
            "resolution": spec.lower(),
            "ratio": payload.get("aspect_ratio", "16:9"),
            "watermark": False,
        }
        if "generate_audio" in payload:
            body["generate_audio"] = bool(payload["generate_audio"])
        data = await _post(provider, "/contents/generations/tasks", body)
    elif provider.kind == "libtv":
        return await _libtv_submit(api_id, spec, payload, images)
    elif provider.kind == "dashscope_video":
        # 百炼万相：异步任务；素材放 input.media，提示词里用 图1/图2/音频1 指代（按同类素材在数组中的顺序）
        media = [{"type": i["role"], "url": i["url"]} for i in images]
        if payload.get("video"):
            media.append({"type": "reference_video", "url": payload["video"]})
        if any(m["type"] in ("first_frame", "last_frame") for m in media):   # 首尾帧不能和参考素材混用
            media = [m for m in media if m["type"] in ("first_frame", "last_frame")]
        body = {"model": api_id, "input": {"prompt": payload.get("prompt", "")[:2500]},
                "parameters": {"resolution": spec.upper(), "ratio": payload.get("aspect_ratio", "16:9"),
                               "duration": int(round(float(payload.get("duration", 5)))),
                               "audio": bool(payload.get("generate_audio", True)), "watermark": False,
                               "prompt_extend": False}}
        if media:
            body["input"]["media"] = media
        data = await _post(provider, "/services/aigc/video-generation/video-synthesis", body,
                           {"X-DashScope-Async": "enable"})
        data = data.get("output") or data
    else:
        body = {"model": api_id, "prompt": payload.get("prompt", ""),
                "duration": int(round(float(payload.get("duration", 5)))), "quality": spec.lower(),
                "aspect_ratio": payload.get("aspect_ratio", "16:9"),
                "generate_audio": bool(payload.get("generate_audio", True))}
        if images:
            body["images"] = [i["url"] for i in images]
            body["image"] = images[0]["url"]
        if payload.get("video"):
            body["video"] = payload["video"]
        data = await _post(provider, "/videos/generations", body)
    task_id = data.get("id") or data.get("task_id")
    if not task_id:
        raise ProviderError(provider.id, None, f"no task id in response: {str(data)[:200]}")
    return {"provider_task_id": task_id, "state": "submitted"}


# ---------------- LibTV（远程 MCP，OAuth 登录态） ----------------

_libtv_bg: set = set()


async def _libtv_submit(api_id: str, spec: str, payload: dict, images: list[dict]) -> dict:
    """LibTV 的 generate_video 可能一直等到出片才返回，所以放到后台跑，先回一个任务号。"""
    from . import db, libtv
    jid = "lt_" + uuid.uuid4().hex[:16]
    with db.tx() as c:
        c.execute("INSERT INTO libtv_jobs(id,status,created_at,updated_at) VALUES(?,?,?,?)",
                  (jid, "submitting", db.now(), db.now()))

    async def run():
        try:
            res = await libtv.generate({**payload, "images": images, "idempotency_key": "vid-" + jid}, api_id, spec)
            with db.tx() as c:
                c.execute("UPDATE libtv_jobs SET status='running', project_id=?, task_id=?, node_id=?, updated_at=? "
                          "WHERE id=?", (res["project_id"], res.get("task_id") or "", res.get("node_id") or "",
                                         db.now(), jid))
        except Exception as e:  # 失败写库，轮询时报失败并退款
            with db.tx() as c:
                c.execute("UPDATE libtv_jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                          (str(e)[:500], db.now(), jid))

    t = asyncio.get_running_loop().create_task(run())
    _libtv_bg.add(t)
    t.add_done_callback(_libtv_bg.discard)
    return {"provider_task_id": jid, "state": "submitted"}


async def _libtv_poll(jid: str) -> dict:
    from . import db, libtv
    with db.tx() as c:
        row = c.execute("SELECT * FROM libtv_jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        return {"state": "failed", "error": "LibTV 任务记录不存在（可能服务重启时还没提交成功）"}
    if row["status"] in ("succeeded", "failed"):
        return {"state": row["status"], "url": row["url"], "seconds": row["seconds"] or None,
                "usage": {"output_seconds": row["seconds"] or None}, "error": row["error"]}
    if row["status"] == "submitting":
        if not any(not t.done() for t in _libtv_bg):   # 服务重启过，后台提交已丢失
            with db.tx() as c:
                c.execute("UPDATE libtv_jobs SET status='failed', error=? WHERE id=?",
                          ("服务重启，提交中断（未拿到 LibTV 任务号）", jid))
            return {"state": "failed", "error": "服务重启，提交中断"}
        return {"state": "running"}
    r = await libtv.poll(row["project_id"], row["task_id"] or None, row["node_id"] or None)
    if r["state"] in ("succeeded", "failed"):
        with db.tx() as c:
            c.execute("UPDATE libtv_jobs SET status=?, url=?, seconds=?, error=?, updated_at=? WHERE id=?",
                      (r["state"], r.get("url") or "", float(r.get("seconds") or 0), r.get("error") or "",
                       db.now(), jid))
    return {**r, "usage": {"output_seconds": r.get("seconds")}}


async def poll_video(provider: Provider, provider_task_id: str) -> dict:
    if provider.kind == "libtv" and provider_task_id.startswith("lt_"):   # 真实提交过的 LibTV 任务，不走模拟分支
        return await _libtv_poll(provider_task_id)
    if not provider.live:
        await asyncio.sleep(0.01)
        try:
            seconds = float(os.environ.get("MOCK_VIDEO_SECONDS") or provider_task_id.split("-")[1])
        except (IndexError, ValueError):
            seconds = 5.0
        return {"state": "succeeded", "url": f"https://mock.local/{provider_task_id}.mp4",
                "seconds": seconds, "usage": {"output_seconds": seconds}}
    if provider.kind == "ark":
        data = await _get(provider, f"/contents/generations/tasks/{provider_task_id}")
        state = data.get("status")
        mapping = {"succeeded": "succeeded", "failed": "failed", "cancelled": "failed"}
        seconds = data.get("duration") or (data.get("usage") or {}).get("output_seconds")
        return {"state": mapping.get(state, "running"),
                "url": (data.get("content") or {}).get("video_url"),
                "seconds": seconds, "usage": {"output_seconds": seconds}}
    if provider.kind == "dashscope_video":
        data = await _get(provider, f"/tasks/{provider_task_id}")
        out, usage = data.get("output") or {}, data.get("usage") or {}
        state = {"SUCCEEDED": "succeeded", "FAILED": "failed", "CANCELED": "failed",
                 "UNKNOWN": "failed"}.get(out.get("task_status"), "running")
        seconds = usage.get("output_video_duration") or usage.get("duration")
        return {"state": state, "url": out.get("video_url"), "seconds": seconds,
                "usage": {"output_seconds": seconds}, "error": out.get("message") or out.get("code")}
    data = await _get(provider, f"/tasks/{provider_task_id}")
    seconds = data.get("duration") or (data.get("usage") or {}).get("output_seconds")
    return {"state": {"completed": "succeeded", "failed": "failed"}.get(data.get("status"), "running"),
            "url": (data.get("results") or [{}])[0].get("url"),
            "seconds": seconds, "usage": {"output_seconds": seconds}}
