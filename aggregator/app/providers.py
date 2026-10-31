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


async def _post(provider: Provider, path: str, payload: dict) -> dict:
    url = provider.base_url.rstrip("/") + path
    try:
        async with _client(provider) as client:
            r = await client.post(url, json=payload, headers=_headers(provider))
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
        text = f"[mock:{provider.id}] " + last[:120]
        return {"content": text,
                "usage": {"prompt_tokens": max(1, len(last) // 2),
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
    body = {"model": api_id, **{k: v for k, v in payload.items() if k != "model"}}
    data = await _post(provider, "/images/generations", body)
    images = [d.get("url") or d.get("b64_json") for d in data.get("data", [])]
    return {"images": images, "provider_model": data.get("model", api_id),
            "usage": {"images": len(images)}}


# ---------------- video ----------------

async def submit_video(provider: Provider, api_id: str, spec: str, payload: dict) -> dict:
    if _mock_disabled(provider):
        raise ProviderError(provider.id, 502, "mock injected failure")
    if not provider.live:
        await asyncio.sleep(0.01)
        return {"provider_task_id": f"mock-{uuid.uuid4().hex[:12]}", "state": "submitted"}
    if provider.kind == "ark":
        body = {
            "model": api_id,
            "content": [{"type": "text", "text": payload.get("prompt", "")}],
            "duration": int(payload.get("duration", 5)),
            "resolution": spec.lower(),
            "ratio": payload.get("aspect_ratio", "16:9"),
            "generate_audio": bool(payload.get("generate_audio", True)),
            "watermark": False,
        }
        data = await _post(provider, "/contents/generations/tasks", body)
    else:
        body = {"model": api_id, "prompt": payload.get("prompt", ""),
                "duration": int(payload.get("duration", 5)), "quality": spec.lower(),
                "aspect_ratio": payload.get("aspect_ratio", "16:9"),
                "generate_audio": bool(payload.get("generate_audio", True))}
        data = await _post(provider, "/videos/generations", body)
    task_id = data.get("id") or data.get("task_id")
    if not task_id:
        raise ProviderError(provider.id, None, f"no task id in response: {str(data)[:200]}")
    return {"provider_task_id": task_id, "state": "submitted"}


async def poll_video(provider: Provider, provider_task_id: str) -> dict:
    if not provider.live:
        await asyncio.sleep(0.01)
        return {"state": "succeeded", "url": f"https://mock.local/{provider_task_id}.mp4",
                "seconds": 5, "usage": {"output_seconds": 5}}
    if provider.kind == "ark":
        data = await _get(provider, f"/contents/generations/tasks/{provider_task_id}")
        state = data.get("status")
        mapping = {"succeeded": "succeeded", "failed": "failed", "cancelled": "failed"}
        seconds = data.get("duration") or (data.get("usage") or {}).get("output_seconds")
        return {"state": mapping.get(state, "running"),
                "url": (data.get("content") or {}).get("video_url"),
                "seconds": seconds, "usage": {"output_seconds": seconds}}
    data = await _get(provider, f"/tasks/{provider_task_id}")
    seconds = data.get("duration") or (data.get("usage") or {}).get("output_seconds")
    return {"state": {"completed": "succeeded", "failed": "failed"}.get(data.get("status"), "running"),
            "url": (data.get("results") or [{}])[0].get("url"),
            "seconds": seconds, "usage": {"output_seconds": seconds}}
