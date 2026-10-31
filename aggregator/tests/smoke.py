"""端到端冒烟：不联网、不花钱，全部走 mock 上游。"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGG_FORCE_MOCK"] = "1"
os.environ.setdefault("AGG_DB", os.path.join(os.path.dirname(__file__), "..", "data", "smoke.db"))
if os.path.exists(os.environ["AGG_DB"]):
    os.remove(os.environ["AGG_DB"])

from app.main import app  # noqa: E402

U = {"X-User-Id": "tester"}


def show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=1)[:1200])


async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agg") as c:
        show("health", (await c.get("/healthz")).json())

        show("topup 100 RMB", (await c.post("/v1/wallet/topup", json={"amount_rmb": 100}, headers=U)).json())

        chat = await c.post("/v1/chat/completions", headers={**U, "Idempotency-Key": "k-chat-1"},
                            json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "把这段剧本拆成分镜"}],
                                  "max_tokens": 200})
        body = chat.json()
        show("chat (mock, 自动选路)", {"status": chat.status_code, "billing": body.get("billing"),
                                      "routing": body.get("routing"),
                                      "content": body["choices"][0]["message"]["content"][:60]})

        again = (await c.post("/v1/chat/completions", headers={**U, "Idempotency-Key": "k-chat-1"},
                              json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "把这段剧本拆成分镜"}],
                                    "max_tokens": 200})).json()
        show("同一 Idempotency-Key 重放", {"same_id": again["id"] == body["id"],
                                       "charged": again["billing"]["charged_rmb"],
                                       "balance": again["billing"]["balance_rmb"]})

        img = (await c.post("/v1/images/generations", headers=U,
                            json={"model": "gpt-image-2.5", "prompt": "孔宣 蛋形态 3D", "n": 2})).json()
        show("image x2", {"billing": img.get("billing"), "urls": len(img.get("data", []))})

        vid = (await c.post("/v1/videos/generations", headers={**U, "Idempotency-Key": "k-vid-1"},
                            json={"model": "seedance-2.5", "prompt": "蛋壳裂开，金红光芒", "resolution": "720P",
                                  "duration": 5, "aspect_ratio": "9:16"})).json()
        show("video submit", vid)

        polled = (await c.get(f"/v1/videos/{vid['id']}", headers=U)).json()
        show("video poll", polled)

        # 单家故障 → 自动降级到下一家
        os.environ["MOCK_FAIL"] = "custom_api_1"
        fb = (await c.post("/v1/chat/completions", headers=U,
                           json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "降级测试"}]})).json()
        show("主线路故障 → 降级", {"routing": fb.get("routing"), "charged": fb["billing"]["charged_rmb"]})

        # 全部线路故障 → 502 且全额退回
        bal_before = (await c.get("/v1/wallet", headers=U)).json()["balance_rmb"]
        os.environ["MOCK_FAIL"] = "custom_api_1,dmxapi,a6_api"
        bad = await c.post("/v1/chat/completions", headers=U,
                           json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "x"}]})
        bal_after = (await c.get("/v1/wallet", headers=U)).json()["balance_rmb"]
        show("全部线路失败", {"status": bad.status_code, "body": bad.json(),
                          "refunded": abs(bal_after - bal_before) < 1e-9})

        os.environ["MOCK_FAIL"] = ""
        poor = await c.post("/v1/chat/completions", headers={**U, "X-User-Id": "poor"},
                            json={"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}],
                                  "max_tokens": 4000})
        show("余额不足", {"status": poor.status_code, "body": poor.json()})

        show("wallet", (await c.get("/v1/wallet", headers=U)).json())
        show("ledger tail", (await c.get("/v1/ledger?limit=6", headers=U)).json())
        show("provider health", (await c.get("/admin/health")).json())


asyncio.run(main())
