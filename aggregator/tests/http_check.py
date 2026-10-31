"""对真实 uvicorn 实例做一次 HTTP 全链路检查（不是 ASGI 直连）。"""
import json
import sys

import httpx

BASE = "http://127.0.0.1:8010"
U = {"X-User-Id": "http-tester"}
ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {detail}")


with httpx.Client(base_url=BASE, timeout=30, trust_env=False) as c:
    h = c.get("/healthz").json()
    check("healthz", h["ok"] and h["version"] == "0.3.0", str(h))

    check("充值", abs(c.post("/v1/wallet/topup", json={"amount_rmb": 20}, headers=U).json()["balance_rmb"] - 20) < 1e-9)

    r = c.post("/v1/chat/completions", headers=U,
               json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "测试"}], "max_tokens": 64})
    body = r.json()
    check("chat 200", r.status_code == 200, r.text[:120])
    check("chat 有 billing", body["billing"]["charged_rmb"] > 0, json.dumps(body.get("billing")))

    # SSE
    chunks, final = 0, None
    with c.stream("POST", "/v1/chat/completions", headers=U,
                  json={"model": "gpt-6.1-sol", "stream": True,
                        "messages": [{"role": "user", "content": "流式"}]}) as resp:
        check("SSE content-type", "text/event-stream" in resp.headers.get("content-type", ""))
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if "choices" in obj:
                chunks += 1
            elif obj.get("object") == "chat.completion.final":
                final = obj
    check("SSE delta", chunks >= 3, str(chunks))
    check("SSE final billing", final and final["billing"]["charged_rmb"] > 0, json.dumps(final or {})[:160])

    img = c.post("/v1/images/generations", headers=U,
                 json={"model": "gpt-image-2.5", "prompt": "x", "n": 1}).json()
    check("image 0.5 元", abs(img["billing"]["charged_rmb"] - 0.5) < 1e-9, json.dumps(img["billing"]))

    v = c.post("/v1/videos/generations", headers=U,
               json={"model": "seedance-2.5", "prompt": "x", "resolution": "480P", "duration": 6}).json()
    check("video 预扣 4.8", abs(v["billing"]["quoted_rmb"] - 4.8) < 1e-9, json.dumps(v["billing"]))
    w = c.get(f"/v1/videos/{v['id']}/wait?timeout_s=5", headers=U).json()
    check("video 结算完成", w["status"] == "succeeded" and w["billing"]["charged_rmb"] > 0, json.dumps(w)[:200])

    stats = c.get("/admin/stats").json()
    check("admin/stats 有任务", stats["tasks"].get("succeeded", 0) >= 1, json.dumps(stats["tasks"]))
    rec = c.get("/admin/reconcile").json()
    check("对账 drift=0", abs(rec["drift_rmb"]) < 1e-6, json.dumps(rec)[:200])

    rep = c.get("/admin/report?days=1").json()
    check("报表收入>0", rep["totals"]["revenue_rmb"] > 0, json.dumps(rep["totals"]))
    check("报表毛利>=0", rep["totals"]["margin_rmb"] >= 0, json.dumps(rep["totals"]))

    cv = c.post("/v1/videos/generations", headers=U,
                json={"model": "seedance-2.5", "prompt": "x", "resolution": "480P", "duration": 4}).json()
    cc = c.post(f"/v1/videos/{cv['id']}/cancel", headers=U).json()
    check("取消视频并退款", cc["status"] == "cancelled" and cc["refund"]["refunded_rmb"] > 0, json.dumps(cc)[:200])

    ws = c.get("/v1/wallet", headers=U).json()
    check("钱包汇总", ws["total_spent_rmb"] > 0 and ws["balance_rmb"] < 20, json.dumps(ws))

    rid = c.get("/healthz").headers.get("x-request-id", "")
    check("响应带 X-Request-Id", rid.startswith("req_"), rid)

print(f"\nHTTP CHECK: PASS {ok}  FAIL {fail}")
sys.exit(1 if fail else 0)
