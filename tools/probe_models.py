"""核对真实渠道：key 是否可用、catalog.yaml 里写的 api_id 是否存在。

    py -3.12 tools/probe_models.py           # 只读：列模型、查鉴权（不花钱）
    py -3.12 tools/probe_models.py --chat    # 另外对每个文本模型发一句"回复OK"（几厘钱）

不会调用图像/视频生成接口（那要真实扣费）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aggregator"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402

from app.config import models, providers  # noqa: E402

LIST_PATHS = {"deepseek": "/models", "dashscope": "/models", "volcengine_official": "/models"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="store_true", help="对每个真实文本模型发一句话（极少量扣费）")
    a = ap.parse_args()
    plist = providers()
    report: dict = {"providers": {}, "models": {}}
    with httpx.Client(timeout=30, trust_env=False, proxy=os.environ.get("AGG_PROXY") or None) as c:
        for pid, path in LIST_PATHS.items():
            p = plist.get(pid)
            if not p:
                continue
            row = {"key": bool(p.api_key), "base_url": p.base_url}
            if p.api_key:
                try:
                    r = c.get(p.base_url.rstrip("/") + path, headers={"Authorization": f"Bearer {p.api_key}"})
                    row["status"] = r.status_code
                    if r.status_code == 200:
                        ids = sorted(m.get("id", "") for m in (r.json().get("data") or []))
                        row["model_count"] = len(ids)
                        row["models"] = ids
                    else:
                        row["body"] = r.text[:200]
                except httpx.HTTPError as e:
                    row["error"] = str(e)[:200]
            if pid == "volcengine_official" and p.api_key:
                try:  # 列视频任务：只读、免费，用来确认视频接口鉴权
                    r = c.get(p.base_url.rstrip("/") + "/contents/generations/tasks?page_size=1",
                              headers={"Authorization": f"Bearer {p.api_key}"})
                    row["video_tasks_api"] = r.status_code
                except httpx.HTTPError as e:
                    row["video_tasks_api"] = str(e)[:120]
            report["providers"][pid] = row

        for m in models().values():
            for rt in m.routes:
                p = plist.get(rt.provider)
                if not p or rt.provider not in LIST_PATHS:
                    continue
                listed = report["providers"].get(rt.provider, {}).get("models")
                entry = {"provider": rt.provider, "api_id": rt.api_id, "type": m.type, "live": p.live,
                         "listed": (rt.api_id in listed) if listed else None}
                if a.chat and m.type == "chat" and p.live:
                    try:
                        r = c.post(p.base_url.rstrip("/") + "/chat/completions",
                                   headers={"Authorization": f"Bearer {p.api_key}"},
                                   json={"model": rt.api_id, "max_tokens": 8,
                                         "messages": [{"role": "user", "content": "只回复OK"}]})
                        entry["chat_status"] = r.status_code
                        entry["chat_reply"] = (r.json()["choices"][0]["message"]["content"][:40]
                                               if r.status_code == 200 else r.text[:200])
                    except Exception as e:  # noqa: BLE001
                        entry["chat_error"] = str(e)[:200]
                report["models"][f"{m.id} @ {rt.provider}"] = entry

    for pid, row in report["providers"].items():
        models_list = row.pop("models", None)
        print(f"== {pid}: {json.dumps(row, ensure_ascii=False)}")
        if models_list:
            interesting = [x for x in models_list if any(k in x for k in ("seed", "deepseek", "qwen", "wan", "doubao"))]
            print("   相关模型：", ", ".join(interesting[:80]))
    print("\n== catalog 核对")
    for k, v in report["models"].items():
        print(f"   {k}: {json.dumps(v, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
