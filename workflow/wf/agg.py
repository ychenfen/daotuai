"""聚合层客户端：文本 / 图像 / 视频。测试时可注入 ASGI transport，不联网。"""
from __future__ import annotations

import os
import sys

import httpx

AGG_BASE = os.environ.get("AGG_BASE", "http://127.0.0.1:8010")
USER = os.environ.get("WORKFLOW_USER", "studio")


def make_client() -> httpx.AsyncClient:
    """WORKFLOW_TEST_AGG=asgi 时，直接把聚合层的 ASGI app 挂进来（进程内测试）。"""
    if os.environ.get("WORKFLOW_TEST_AGG") == "asgi":
        agg_root = os.environ.get("AGG_ROOT") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "aggregator")
        if agg_root not in sys.path:
            sys.path.insert(0, agg_root)
        from app.main import app as agg_app  # type: ignore
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=agg_app), base_url="http://agg",
                                 timeout=60, trust_env=False)
    return httpx.AsyncClient(base_url=AGG_BASE, timeout=180, trust_env=False)


class Agg:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client

    @property
    def headers(self) -> dict:
        return {"X-User-Id": USER}

    async def topup(self, amount_rmb: float) -> dict:
        r = await self.c.post("/v1/wallet/topup", json={"amount_rmb": amount_rmb}, headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def wallet(self) -> dict:
        r = await self.c.get("/v1/wallet", headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def chat(self, model: str, messages: list[dict], max_tokens: int = 2000,
                   temperature: float = 0.3) -> dict:
        r = await self.c.post("/v1/chat/completions", headers=self.headers,
                              json={"model": model, "messages": messages, "max_tokens": max_tokens,
                                    "temperature": temperature})
        if r.status_code >= 400:
            raise AggError(f"chat failed {r.status_code}: {r.text[:300]}")
        return r.json()

    async def submit_video(self, model: str, prompt: str, resolution: str, duration_s: float,
                           aspect_ratio: str = "9:16") -> dict:
        r = await self.c.post("/v1/videos/generations", headers=self.headers,
                              json={"model": model, "prompt": prompt, "resolution": resolution,
                                    "duration": duration_s, "aspect_ratio": aspect_ratio})
        if r.status_code >= 400:
            raise AggError(f"video submit failed {r.status_code}: {r.text[:300]}")
        return r.json()

    async def poll_video(self, task_id: str) -> dict:
        r = await self.c.get(f"/v1/videos/{task_id}", headers=self.headers)
        if r.status_code >= 400:
            raise AggError(f"video poll failed {r.status_code}: {r.text[:300]}")
        return r.json()

    async def report(self, days: int = 7) -> dict:
        r = await self.c.get(f"/admin/report?days={days}")
        r.raise_for_status()
        return r.json()


class AggError(Exception):
    pass
