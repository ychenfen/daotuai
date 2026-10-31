"""路由：权重排序 + 健康度冷却 + 并发闸门 + 逐家降级 + 时延统计。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from . import concurrency
from .config import Route, candidates, providers
from .providers import ProviderError


class RouterExhausted(Exception):
    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class Health:
    fails: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    wins: int = 0
    total_ms: float = 0.0
    calls: int = 0

    @property
    def avg_ms(self) -> float:
        return round(self.total_ms / self.calls, 1) if self.calls else 0.0


_health: dict[str, Health] = {}


def health(provider_id: str) -> Health:
    return _health.setdefault(provider_id, Health())


def snapshot() -> dict:
    now = time.time()
    return {
        pid: {"fails": h.fails, "wins": h.wins,
              "cooldown_s": max(0.0, round(h.cooldown_until - now, 1)),
              "avg_ms": h.avg_ms, "calls": h.calls, "last_error": h.last_error}
        for pid, h in sorted(_health.items())
    }


def _mark_ok(provider_id: str, latency_ms: float) -> None:
    h = health(provider_id)
    h.wins += 1
    h.fails = 0
    h.cooldown_until = 0.0
    h.last_error = ""
    h.total_ms += latency_ms
    h.calls += 1


def _mark_fail(provider_id: str, err: str, latency_ms: float = 0.0) -> None:
    h = health(provider_id)
    h.fails += 1
    h.last_error = err
    h.total_ms += latency_ms
    h.calls += 1
    # 指数退避，最长 60s
    h.cooldown_until = time.time() + min(60.0, 2.0 ** min(h.fails, 5))


def ordered(model_id: str, spec: str | None = None) -> list[Route]:
    routes = candidates(model_id, spec)
    now = time.time()
    live = [r for r in routes if health(r.provider).cooldown_until <= now]
    cooling = [r for r in routes if health(r.provider).cooldown_until > now]
    # 全部在冷却时仍给最后一次机会（避免假死锁）
    return live + cooling


async def call(model_id: str, spec: str | None, fn: Callable[[Route], object],
               gate_timeout_s: float = 8.0) -> tuple[Route, dict, dict]:
    """按顺序尝试，返回 (route, result, meta)。全部失败抛 RouterExhausted。"""
    routes = ordered(model_id, spec)
    if not routes:
        raise RouterExhausted(f"no route for model={model_id} spec={spec}", [])

    attempts: list[dict] = []
    for idx, route in enumerate(routes, start=1):
        t0 = time.perf_counter()
        try:
            async with concurrency.slot(route.provider, gate_timeout_s):
                result = await fn(route)
            latency = (time.perf_counter() - t0) * 1000
            _mark_ok(route.provider, latency)
            attempts.append({"provider": route.provider, "ok": True, "ms": round(latency, 1)})
            return route, result, {"attempt": idx, "latency_ms": round(latency, 1), "attempts": attempts}
        except concurrency.GateTimeout as e:
            latency = (time.perf_counter() - t0) * 1000
            _mark_fail(route.provider, str(e), latency)
            attempts.append({"provider": route.provider, "ok": False, "ms": round(latency, 1), "error": str(e)})
        except ProviderError as e:
            latency = (time.perf_counter() - t0) * 1000
            _mark_fail(route.provider, str(e), latency)
            attempts.append({"provider": route.provider, "ok": False, "ms": round(latency, 1), "error": str(e)})
        except Exception as e:  # 未知异常同样降级
            latency = (time.perf_counter() - t0) * 1000
            _mark_fail(route.provider, repr(e), latency)
            attempts.append({"provider": route.provider, "ok": False, "ms": round(latency, 1), "error": repr(e)})

    raise RouterExhausted(f"all routes failed for {model_id}: {attempts[-1]['error']}", attempts)


async def stream(model_id: str, spec: str | None, fn: Callable[[Route], AsyncIterator[dict]],
                 gate_timeout_s: float = 8.0) -> AsyncIterator[tuple[Route, dict]]:
    """流式：把第一家的每个事件原样抛出；首包前失败可换线，首包后失败直接抛错。"""
    routes = ordered(model_id, spec)
    if not routes:
        raise RouterExhausted(f"no route for model={model_id} spec={spec}", [])

    attempts: list[dict] = []
    for route in routes:
        t0 = time.perf_counter()
        got_first = False
        try:
            async with concurrency.slot(route.provider, gate_timeout_s):
                async for event in fn(route):
                    if not got_first:
                        got_first = True
                    yield route, event
            latency = (time.perf_counter() - t0) * 1000
            _mark_ok(route.provider, latency)
            return
        except (ProviderError, concurrency.GateTimeout) as e:
            latency = (time.perf_counter() - t0) * 1000
            _mark_fail(route.provider, str(e), latency)
            attempts.append({"provider": route.provider, "ok": False, "error": str(e)})
            if got_first:
                raise RouterExhausted(f"stream broke after first delta: {e}", attempts) from e
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            _mark_fail(route.provider, repr(e), latency)
            attempts.append({"provider": route.provider, "ok": False, "error": repr(e)})
            if got_first:
                raise RouterExhausted(f"stream broke after first delta: {e}", attempts) from e

    raise RouterExhausted(f"all routes failed for {model_id}", attempts)


def provider_meta(provider_id: str) -> dict:
    p = providers()[provider_id]
    return {"id": p.id, "label": p.label, "kind": p.kind, "live": p.live}
