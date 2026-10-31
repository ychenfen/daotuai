"""按渠道的并发闸门：避免单家被打爆，同时保证请求不会无限排队。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from .config import providers


class GateTimeout(Exception):
    pass


_semaphores: dict[str, asyncio.Semaphore] = {}
_inflight: dict[str, int] = {}
_peak: dict[str, int] = {}


def _sem(provider_id: str) -> asyncio.Semaphore:
    if provider_id not in _semaphores:
        limit = max(1, providers()[provider_id].concurrency)
        _semaphores[provider_id] = asyncio.Semaphore(limit)
    return _semaphores[provider_id]


@asynccontextmanager
async def slot(provider_id: str, timeout_s: float = 8.0):
    """占用一个并发位；排队超时抛 GateTimeout，由路由层换下一家。"""
    sem = _sem(provider_id)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=timeout_s)
    except asyncio.TimeoutError as e:
        raise GateTimeout(f"{provider_id} busy (>{timeout_s}s wait)") from e
    _inflight[provider_id] = _inflight.get(provider_id, 0) + 1
    _peak[provider_id] = max(_peak.get(provider_id, 0), _inflight[provider_id])
    try:
        yield
    finally:
        _inflight[provider_id] -= 1
        sem.release()


def snapshot() -> dict:
    return {
        pid: {"inflight": _inflight.get(pid, 0), "peak": _peak.get(pid, 0),
              "limit": max(1, providers()[pid].concurrency)}
        for pid in _semaphores
    }
