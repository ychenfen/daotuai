"""@提及解析：把提示词里的「@孔宣（蛋形态）」解析成 资产 + 形态 + 版本（reference_bindings）。

刀徒的教训：库里存的绑定和解析器算出来的不一致（14 条里 11 条），提交时就被拒。这里的做法：
- 绑定在「保存提示词」和「提交视频」时各算一次，提交时一律以最新解析结果为准；
- 名字按最长匹配：「@孔宣（蛋形态）」→ 孔宣·蛋，「@孔宣」→ 孔宣·默认；@ 后面紧跟正文也能认；
- 形态写法都认：（蛋）（蛋形态）(蛋) ·蛋；
- 认不出的 @ 或形态单独列为 unresolved，在界面上提示，不会静默变成空数组。
"""
from __future__ import annotations

import re

_STOP = re.compile(r"[\s，。,.;；:：、!！?？\[\]【】\"“”'‘’@<>《》{}]")
_FORM_TAIL = re.compile(r"^[（(]([^）)]{1,12})[）)]")


def _form_keys(name: str, form: str) -> list[str]:
    out = [f"{name}（{form}）", f"{name}({form})", f"{name}·{form}"]
    if not form.endswith("形态"):
        out += [f"{name}（{form}形态）", f"{name}({form}形态)"]
    return out


def _table(assets: list[dict]) -> list[tuple[str, dict, dict]]:
    rows = []
    for a in assets:
        forms = a.get("forms") or []
        default = next((f for f in forms if f.get("is_default")), forms[0] if forms else None)
        for n in [a["name"]] + list(a.get("aliases") or []):
            if not n:
                continue
            if default:
                rows.append((n, a, default))
            for f in forms:
                for k in _form_keys(n, f["name"]):
                    rows.append((k, a, f))
    rows.sort(key=lambda r: -len(r[0]))
    return rows


def binding(a: dict, f: dict | None, mention: str) -> dict:
    f = f or {}
    return {"asset_id": a["id"], "form_id": f.get("id", ""), "kind": a["kind"], "name": a["name"],
            "form": f.get("name", ""), "mention": mention, "version": int(f.get("current_version") or 0),
            "media_type": "audio" if a["kind"] == "audio" else "image",
            "image_url": f.get("image_url") or "", "local_path": f.get("local_path") or ""}


def resolve(text: str, assets: list[dict]) -> tuple[list[dict], list[str]]:
    """返回 (bindings, unresolved)。assets 为 Workflow._assets() 的结果（含 forms）。"""
    table = _table(assets)
    bindings: list[dict] = []
    seen: set[str] = set()
    unresolved: list[str] = []
    i = 0
    while True:
        i = text.find("@", i)
        if i < 0:
            break
        rest = text[i + 1:]
        hit = next(((k, a, f) for k, a, f in table if rest.startswith(k)), None)
        if hit:
            key, a, f = hit
            tail = _FORM_TAIL.match(rest[len(key):]) if key in ([a["name"]] + list(a.get("aliases") or [])) else None
            if tail and not any(fm["name"] in (tail.group(1), tail.group(1).removesuffix("形态"))
                                for fm in a.get("forms") or []):
                # 写了形态但没有这个形态：仍按默认形态绑定，同时提示
                miss = f"{key}（{tail.group(1)}）"
                if miss not in unresolved:
                    unresolved.append(miss)
            uid = (f or {}).get("id") or a["id"]
            if uid not in seen:
                seen.add(uid)
                bindings.append(binding(a, f, "@" + key))
            i += 1 + len(key)
        else:
            m = _STOP.search(rest)
            token = rest[: m.start() if m else len(rest)][:12]
            if token and token not in unresolved and not token.isdigit():
                unresolved.append(token)
            i += 1
    return bindings, unresolved


def stale(bindings: list[dict], forms_by_id: dict[str, dict]) -> list[dict]:
    """哪些绑定的版本已经落后于该形态当前版本（重新出图 / 切换版本后）。"""
    out = []
    for b in bindings:
        f = forms_by_id.get(b.get("form_id") or "")
        if f is None:
            out.append({**b, "reason": "deleted"})
        elif int(f.get("current_version") or 0) != int(b.get("version") or 0):
            out.append({**b, "reason": "version_changed", "current_version": f.get("current_version")})
    return out
