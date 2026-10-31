"""@提及解析：把提示词里的「@孔宣」解析成具体资产 + 具体版本（reference_bindings）。

刀徒的教训：库里存的绑定和解析器算出来的不一致（14 条里 11 条），提交时就被拒。
这里的做法：
- 绑定在「保存提示词」和「提交视频」时各算一次，提交时一律以最新解析结果为准；
- 名字按最长匹配（"孔宣蛋形态" 优先于 "孔宣"），@ 后面紧跟正文也能认；
- 认不出的 @ 单独列为 unresolved，在界面上提示，不会静默变成空数组。
"""
from __future__ import annotations

import re

_STOP = re.compile(r"[\s，。,.;；:：、!！?？()（）\[\]【】\"“”'‘’@<>《》]")


def _keys(asset: dict) -> list[str]:
    keys = [asset["name"]] + list(asset.get("aliases") or [])
    return [k for k in keys if k]


def resolve(text: str, assets: list[dict]) -> tuple[list[dict], list[str]]:
    """返回 (bindings, unresolved)。assets 每项需含 id/kind/name/aliases/current_version/image_url/local_path。"""
    table = sorted(((k, a) for a in assets for k in _keys(a)), key=lambda kv: -len(kv[0]))
    bindings: list[dict] = []
    seen: set[str] = set()
    unresolved: list[str] = []
    i = 0
    while True:
        i = text.find("@", i)
        if i < 0:
            break
        rest = text[i + 1:]
        hit = next(((k, a) for k, a in table if rest.startswith(k)), None)
        if hit:
            key, a = hit
            if a["id"] not in seen:
                seen.add(a["id"])
                bindings.append({"asset_id": a["id"], "kind": a["kind"], "name": a["name"],
                                 "mention": "@" + key, "version": int(a.get("current_version") or 0),
                                 "image_url": a.get("image_url") or "", "local_path": a.get("local_path") or ""})
            i += 1 + len(key)
        else:
            m = _STOP.search(rest)
            token = rest[: m.start() if m else len(rest)][:8]
            if token and token not in unresolved:
                unresolved.append(token)
            i += 1
    return bindings, unresolved


def stale(bindings: list[dict], assets_by_id: dict[str, dict]) -> list[dict]:
    """哪些绑定的版本已经落后于资产当前版本（资产重新出图后）。"""
    out = []
    for b in bindings:
        a = assets_by_id.get(b["asset_id"])
        if a is None:
            out.append({**b, "reason": "asset_deleted"})
        elif int(a.get("current_version") or 0) != int(b.get("version") or 0):
            out.append({**b, "reason": "version_changed", "current_version": a.get("current_version")})
    return out
