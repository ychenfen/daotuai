"""聚合层客户端：文本 / 图像 / 视频 / 报价 / 钱包。测试时可注入 ASGI transport，不联网。"""
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
    return httpx.AsyncClient(base_url=AGG_BASE, timeout=300, trust_env=False)


class AggError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _raise(r: httpx.Response, what: str) -> None:
    if r.status_code < 400:
        return
    code = None
    try:
        code = (r.json().get("error") or {}).get("code")
    except Exception:
        pass
    raise AggError(f"{what} failed {r.status_code}: {r.text[:300]}", r.status_code, code)


class Agg:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client

    @staticmethod
    def _h(idem: str | None = None) -> dict:
        h = {"X-User-Id": USER}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    async def models(self) -> list[dict]:
        r = await self.c.get("/v1/models")
        _raise(r, "models")
        return r.json()["data"]

    async def quote(self, model: str, **kw) -> dict:
        r = await self.c.post("/v1/quote", json={"model": model, **kw})
        _raise(r, "quote")
        return r.json()

    async def topup(self, amount_rmb: float) -> dict:
        r = await self.c.post("/v1/wallet/topup", json={"amount_rmb": amount_rmb}, headers=self._h())
        _raise(r, "topup")
        return r.json()

    async def wallet(self) -> dict:
        r = await self.c.get("/v1/wallet", headers=self._h())
        _raise(r, "wallet")
        return r.json()

    async def chat(self, model: str, messages: list[dict], max_tokens: int = 2000,
                   temperature: float = 0.3) -> dict:
        r = await self.c.post("/v1/chat/completions", headers=self._h(),
                              json={"model": model, "messages": messages, "max_tokens": max_tokens,
                                    "temperature": temperature})
        _raise(r, "chat")
        return r.json()

    async def image(self, model: str, prompt: str, size: str = "1024x1024",
                    refs: list[str] | None = None, idem: str | None = None) -> dict:
        body = {"model": model, "prompt": prompt, "n": 1, "size": size}
        if refs:
            body["image"] = refs
        r = await self.c.post("/v1/images/generations", headers=self._h(idem), json=body)
        _raise(r, "image")
        return r.json()

    async def submit_video(self, model: str, prompt: str, resolution: str, duration_s: float,
                           aspect_ratio: str = "9:16", images: list[dict] | None = None,
                           idem: str | None = None) -> dict:
        body = {"model": model, "prompt": prompt, "resolution": resolution,
                "duration": duration_s, "aspect_ratio": aspect_ratio}
        if images:
            body["images"] = images
        r = await self.c.post("/v1/videos/generations", headers=self._h(idem), json=body)
        _raise(r, "video submit")
        return r.json()

    async def poll_video(self, task_id: str) -> dict:
        r = await self.c.get(f"/v1/videos/{task_id}", headers=self._h())
        _raise(r, "video poll")
        return r.json()

    async def cancel_video(self, task_id: str) -> dict:
        r = await self.c.post(f"/v1/videos/{task_id}/cancel", headers=self._h())
        _raise(r, "video cancel")
        return r.json()

    async def report(self, days: int = 7) -> dict:
        r = await self.c.get(f"/admin/report?days={days}")
        _raise(r, "report")
        return r.json()

    async def fetch(self, url: str) -> bytes:
        """下载上游产物（视频/图片）。上游 URL 是外网地址，单独开客户端，不走聚合层。"""
        proxy = os.environ.get("WORKFLOW_PROXY") or None
        async with httpx.AsyncClient(timeout=300, trust_env=False, proxy=proxy,
                                     follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.content
