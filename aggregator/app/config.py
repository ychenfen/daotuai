"""配置：providers.yaml + catalog.yaml 的加载与查询。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Provider:
    id: str
    label: str
    kind: str
    base_url: str
    api_key_env: str
    concurrency: int = 4
    timeout_s: int = 120
    enabled: bool = True
    proxy: str | None = None

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @property
    def live(self) -> bool:
        """没有 key 就走 mock，保证本地可运行、可测。AGG_FORCE_MOCK=1 时一律 mock（测试用，绝不花钱）。"""
        if os.environ.get("AGG_FORCE_MOCK", "").strip().lower() in ("1", "true", "yes", "on"):
            return False
        if self.kind == "libtv":   # LibTV 用 OAuth 登录态，不用 key
            from . import libtv
            return self.enabled and libtv.authorized()
        return bool(self.api_key) and self.kind != "mock" and self.enabled


@dataclass
class Route:
    provider: str
    api_id: str
    spec: str | None = None
    group: str | None = None
    weight: int = 1
    price: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)


@dataclass
class Model:
    id: str
    type: str            # chat | image | video | video_edit
    unit: str            # tokens | image | output_second | input_second
    price: dict
    routes: list[Route]
    cost: dict = field(default_factory=dict)
    specs: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    duration: dict = field(default_factory=dict)
    context: int = 0

    def pricing(self, route: Route | None = None, basis: str = "price") -> dict:
        """basis=price 取零售价，cost 取上游成本；线路可覆盖，且做一层深合并。"""
        source = self.price if basis == "price" else (self.cost or self.price)
        merged = _deep_merge({}, source)
        if route is not None:
            override = route.price if basis == "price" else route.cost
            merged = _deep_merge(merged, override or {})
        return merged


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _load_yaml(name: str) -> dict:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def providers() -> dict[str, Provider]:
    out = {}
    for raw in _load_yaml("providers.yaml")["providers"]:
        p = Provider(**raw)
        out[p.id] = p
    return out


@lru_cache(maxsize=1)
def models() -> dict[str, Model]:
    out = {}
    for raw in _load_yaml("catalog.yaml")["models"]:
        routes = [Route(**r) for r in raw.get("routes", [])]
        out[raw["id"]] = Model(
            id=raw["id"], type=raw["type"], unit=raw["unit"], price=raw["price"], routes=routes,
            cost=raw.get("cost", {}), specs=raw.get("specs", []), modes=raw.get("modes", []),
            duration=raw.get("duration", {}), context=raw.get("context", 0),
        )
    return out


def candidates(model_id: str, spec: str | None = None) -> list[Route]:
    """按权重降序返回可用线路；weight 越大越优先。"""
    m = models().get(model_id)
    if not m:
        return []
    routes = [r for r in m.routes if spec is None or r.spec in (None, spec)]
    plist = providers()
    routes = [r for r in routes if r.provider in plist and plist[r.provider].enabled]
    # 有真实线路时只走真实线路：真实失败不能悄悄降级到模拟线路（否则会把假视频当真视频交付）
    live = [r for r in routes if plist[r.provider].live]
    if live:
        routes = live
    return sorted(routes, key=lambda r: -r.weight)
