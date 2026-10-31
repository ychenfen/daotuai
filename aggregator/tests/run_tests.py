"""零依赖测试：不联网、不装 pytest，直接 `python tests/run_tests.py`。

覆盖：定价数学 / 预扣结算退回 / 幂等 / 路由降级 / 流式 / 视频生命周期 / 对账。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:  # Windows 控制台默认 GBK，强制 UTF-8 便于打印中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import logging  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
DB = os.path.join(ROOT, "data", "test.db")
os.environ["AGG_DB"] = DB
os.environ["AGG_FORCE_MOCK"] = "1"  # 测试绝不走真实渠道（电脑上有真实 key）
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

from app import billing, concurrency, router  # noqa: E402
from app.main import app  # noqa: E402

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []
U = {"X-User-Id": "tester"}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def near(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


async def main() -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agg") as c:
        print("\n[A] 模型目录与定价")
        h = (await c.get("/healthz")).json()
        check("healthz ok", h.get("ok") is True and h["providers"] == 21 and h["models"] == 14, str(h))
        ms = (await c.get("/v1/models")).json()["data"]
        check("模型目录 14 个", len(ms) == 14, str(len(ms)))
        check("强制 mock 时没有真实线路", not any(m["live"] for m in ms), str([m["id"] for m in ms if m["live"]]))

        q = (await c.post("/v1/quote", json={"model": "gpt-6.1-sol", "in_tokens": 1_000_000,
                                             "out_tokens": 1_000_000})).json()
        check("文本报价 = 48.32 元/百万双计", near(q["rmb"], 48.32), str(q))
        q = (await c.post("/v1/quote", json={"model": "gpt-image-2.5", "n": 3})).json()
        check("图像报价 3 张 = 1.5 元", near(q["rmb"], 1.5), str(q))
        q = (await c.post("/v1/quote", json={"model": "seedance-2.5", "spec": "480P", "seconds": 5})).json()
        check("视频 480P×5s = 4.0 元", near(q["rmb"], 4.0), str(q))

        print("\n[B] 钱包与文本主流程")
        w = (await c.post("/v1/wallet/topup", json={"amount_rmb": 50}, headers=U)).json()
        check("充值后余额 50", near(w["balance_rmb"], 50.0), str(w))

        r = await c.post("/v1/chat/completions", headers={**U, "Idempotency-Key": "k1"},
                         json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "拆分镜"}],
                               "max_tokens": 200})
        body = r.json()
        check("文本 200", r.status_code == 200, str(body)[:200])
        check("按实际 usage 结算 < 预扣", 0 < body["billing"]["charged_rmb"] <= body["billing"]["quoted_rmb"],
              json.dumps(body["billing"]))
        bal1 = body["billing"]["balance_rmb"]

        again = (await c.post("/v1/chat/completions", headers={**U, "Idempotency-Key": "k1"},
                              json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "拆分镜"}],
                                    "max_tokens": 200})).json()
        check("幂等重放：同 id", again["id"] == body["id"])
        check("幂等重放：不重复扣费", near(again["billing"]["balance_rmb"], bal1), str(again["billing"]))

        r404 = await c.post("/v1/chat/completions", headers=U, json={"model": "nope", "messages": []})
        check("未知模型 404", r404.status_code == 404, r404.text[:120])
        r400 = await c.post("/v1/chat/completions", headers=U,
                            json={"model": "gpt-image-2.5", "messages": [{"role": "user", "content": "x"}]})
        check("模型类型错配 400", r400.status_code == 400, r400.text[:120])

        poor = await c.post("/v1/chat/completions", headers={**U, "X-User-Id": "poor"},
                            json={"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}],
                                  "max_tokens": 4000})
        check("余额不足 402 + quote", poor.status_code == 402 and "quote" in poor.text, poor.text[:160])

        print("\n[C] 图像")
        img = (await c.post("/v1/images/generations", headers=U,
                            json={"model": "gpt-image-2.5", "prompt": "孔宣", "n": 2})).json()
        check("图像 2 张扣 1.0 元", near(img["billing"]["charged_rmb"], 1.0), json.dumps(img["billing"]))
        check("图像返回 2 个 url", len(img["data"]) == 2, str(len(img["data"])))

        print("\n[D] 视频生命周期（异步 + 按实际秒结算）")
        v = (await c.post("/v1/videos/generations", headers={**U, "Idempotency-Key": "v1"},
                          json={"model": "seedance-2.5", "prompt": "蛋壳裂开", "resolution": "480P",
                                "duration": 4})).json()
        check("提交返回 submitted", v["status"] == "submitted" and v["id"].startswith("vtask_"), str(v)[:160])
        check("预扣 = 4s×0.8 = 3.2", near(v["billing"]["quoted_rmb"], 3.2), str(v["billing"]))
        os.environ["MOCK_VIDEO_SECONDS"] = "5"  # 模拟上游实际出了 5s（比请求的 4s 多）
        done = (await c.get(f"/v1/videos/{v['id']}/wait?timeout_s=5", headers=U)).json()
        os.environ.pop("MOCK_VIDEO_SECONDS", None)
        check("轮询完成并结算", done["status"] == "succeeded", str(done)[:200])
        check("按上游返回 5s 结算 = 4.0（补扣）", near(done["billing"]["charged_rmb"], 4.0),
              str(done["billing"]))

        bad_res = await c.post("/v1/videos/generations", headers=U,
                               json={"model": "seedance-2.5", "prompt": "x", "resolution": "8K", "duration": 4})
        check("非法分辨率 400", bad_res.status_code == 400, bad_res.text[:120])
        bad_dur = await c.post("/v1/videos/generations", headers=U,
                               json={"model": "seedance-2.5", "prompt": "x", "resolution": "480P", "duration": 99})
        check("非法时长 400", bad_dur.status_code == 400, bad_dur.text[:120])

        vf = (await c.post("/v1/videos/generations", headers=U,
                           json={"model": "seedance-2.5", "prompt": "x", "resolution": "720P",
                                 "duration": 5})).json()
        check("720P 预扣 = 8.75", near(vf["billing"]["quoted_rmb"], 8.75), str(vf["billing"]))

        print("\n[E] 路由降级与熔断")
        os.environ["MOCK_FAIL"] = "custom_api_1"
        fb = (await c.post("/v1/chat/completions", headers=U,
                           json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "降级"}]})).json()
        check("主线路故障 → 换到 dmxapi", fb["routing"]["provider"] == "dmxapi", str(fb["routing"]))
        check("降级记录 attempt=2", fb["routing"]["attempt"] == 2, str(fb["routing"]))

        os.environ["MOCK_FAIL"] = "custom_api_1,dmxapi,a6_api"
        bal_before = (await c.get("/v1/wallet", headers=U)).json()["balance_rmb"]
        dead = await c.post("/v1/chat/completions", headers=U,
                            json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "全挂"}]})
        bal_after = (await c.get("/v1/wallet", headers=U)).json()["balance_rmb"]
        check("全部失败 502", dead.status_code == 502, dead.text[:160])
        check("全部失败全额退回", near(bal_before, bal_after), f"{bal_before} -> {bal_after}")
        check("失败返回 attempts 明细", len(dead.json()["error"].get("attempts", [])) == 3,
              dead.text[:200])
        os.environ["MOCK_FAIL"] = ""

        print("\n[F] 流式 (SSE)")
        chunks, final = [], None
        async with c.stream("POST", "/v1/chat/completions", headers=U,
                            json={"model": "gpt-6.1-sol", "stream": True,
                                  "messages": [{"role": "user", "content": "流式测试"}]}) as resp:
            check("流式 200 + text/event-stream", resp.status_code == 200 and
                  "text/event-stream" in resp.headers.get("content-type", ""),
                  resp.headers.get("content-type", ""))
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if "choices" in obj:
                    chunks.append(obj)
                elif obj.get("object") == "chat.completion.final":
                    final = obj
        check("收到多个 delta", len(chunks) >= 3, str(len(chunks)))
        check("流式有最终 billing", final is not None and final["billing"]["charged_rmb"] > 0,
              json.dumps(final or {})[:200])

        print("\n[G] 账本与对账")
        ws = (await c.get("/v1/wallet", headers=U)).json()
        check("钱包汇总含消费额", ws["total_spent_rmb"] > 0, json.dumps(ws))
        rec = (await c.get("/admin/reconcile")).json()
        check("账本 drift = 0", near(rec["drift_rmb"], 0.0), json.dumps(rec)[:300])
        check("无孤儿预扣", rec["ok"] is True and not rec["orphan_holds"], json.dumps(rec)[:300])

        st = (await c.get("/admin/stats")).json()
        check("任务统计含 succeeded", st["tasks"].get("succeeded", 0) >= 1, json.dumps(st["tasks"]))
        check("并发快照已记录", len(st["concurrency"]) >= 1, json.dumps(st["concurrency"]))

        holds = (await c.get("/v1/ledger?limit=200", headers=U)).json()["items"]
        kinds = {h["kind"] for h in holds}
        check("账本包含 reserve/settle", {"reserve", "settle"} <= kinds, str(kinds))

        # 单位测试：重复 settle / refund 幂等
        billing.topup("unit", 5.0)
        hold = billing.reserve("unit", 1.0, "unit-test", model="gpt-6.1-sol")
        s1 = billing.settle(hold, 0.4)
        s2 = billing.settle(hold, 0.4)
        check("重复 settle 幂等", s2.get("idempotent") is True and near(s2["charged_rmb"], 0.4),
              json.dumps([s1, s2])[:200])
        hold2 = billing.reserve("unit", 1.0, "unit-refund", model="gpt-6.1-sol")
        r1 = billing.refund(hold2)
        r2 = billing.refund(hold2)
        check("重复 refund 幂等", near(r1["refunded_rmb"], 1.0) and near(r2["refunded_rmb"], 0.0),
              json.dumps([r1, r2])[:200])

        print("\n[G2] 报表 / 过期回收 / 取消")
        rep = (await c.get("/admin/report?days=1")).json()
        check("报表有收入", rep["totals"]["revenue_rmb"] > 0, json.dumps(rep["totals"]))
        check("报表毛利 >= 0", rep["totals"]["margin_rmb"] >= 0, json.dumps(rep["totals"]))
        check("报表按模型分组", len(rep["by_model"]) >= 2, str(len(rep["by_model"])))
        check("渠道成本已记录", any(r["cost"] > 0 for r in rep["by_model_provider"]),
              json.dumps(rep["by_model_provider"])[:200])
        check("报表无未知模型（所有预扣都带 model）", all(r["model"] != "?" for r in rep["by_model"]),
              json.dumps(rep["by_model"])[:200])

        # 过期回收：造一个 2 小时前的预扣
        billing.topup("reaper", 2.0)
        reap_hold = billing.reserve("reaper", 2.0, "reap-test", model="gpt-6.1-sol")
        with __import__("app.db", fromlist=["tx"]).tx() as cx:
            cx.execute("UPDATE holds SET created_at=datetime('now','-2 hours') WHERE hold_id=?", (reap_hold,))
        dry = (await c.post("/admin/reap?older_than_minutes=60&dry_run=true")).json()
        check("reap dry-run 命中 1 条", dry["eligible"] >= 1, json.dumps(dry)[:200])
        done = (await c.post("/admin/reap?older_than_minutes=60")).json()
        check("reap 已退回", done["eligible"] >= 1 and done["refunded_rmb"] >= 2.0, json.dumps(done)[:200])
        check("reap 后余额恢复", near(billing.balance("reaper"), 2.0), str(billing.balance("reaper")))
        rec2 = (await c.get("/admin/reconcile")).json()
        check("reap 后无孤儿", not rec2["orphan_holds"], json.dumps(rec2)[:200])

        # 取消视频
        cv = (await c.post("/v1/videos/generations", headers=U,
                           json={"model": "seedance-2.5", "prompt": "x", "resolution": "480P",
                                 "duration": 4})).json()
        cc = (await c.post(f"/v1/videos/{cv['id']}/cancel", headers=U)).json()
        check("取消后状态 cancelled", cc["status"] == "cancelled", json.dumps(cc)[:200])
        check("取消全额退回", near(cc["refund"]["refunded_rmb"], 3.2), json.dumps(cc["refund"]))
        rec3 = (await c.get("/admin/reconcile")).json()
        check("取消后账本仍平", near(rec3["drift_rmb"], 0.0), json.dumps(rec3)[:200])

        print("\n[H] 配置一致性")
        from app.config import candidates
        check("seedance-2.5 线路 10 条", len(candidates("seedance-2.5")) == 10,
              str(len(candidates("seedance-2.5"))))
        check("480P 线路 4 条", len([x for x in candidates("seedance-2.5", "480P") if x.spec == "480P"]) == 4)
        check("权重排序生效", candidates("gpt-6.1-sol")[0].provider == "custom_api_1",
              str([r.provider for r in candidates("gpt-6.1-sol")]))

    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAILED: {name} :: {detail[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
