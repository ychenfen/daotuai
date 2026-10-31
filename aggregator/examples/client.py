"""客户端示例：把聚合网关当成 OpenAI 用，另外多了 billing / routing 字段。

运行前先起服务：python -m uvicorn app.main:app --port 8010
"""
import json

import httpx

BASE = "http://127.0.0.1:8010"
HEAD = {"X-User-Id": "demo"}


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as c:
        c.post("/v1/wallet/topup", json={"amount_rmb": 10}, headers=HEAD)

        # 1) 普通对话
        r = c.post("/v1/chat/completions", headers={**HEAD, "Idempotency-Key": "demo-1"},
                   json={"model": "gpt-6.1-sol",
                         "messages": [{"role": "user", "content": "把这段剧本拆成 3 个分镜"}],
                         "max_tokens": 256})
        data = r.json()
        print("chat:", data["choices"][0]["message"]["content"][:80])
        print("billing:", json.dumps(data["billing"], ensure_ascii=False))
        print("routing:", json.dumps(data["routing"], ensure_ascii=False))

        # 2) 流式
        print("\nstream:", end=" ")
        with c.stream("POST", "/v1/chat/completions", headers=HEAD,
                      json={"model": "gpt-6.1-sol", "stream": True,
                            "messages": [{"role": "user", "content": "写一句片头旁白"}]}) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                obj = json.loads(payload)
                if "choices" in obj:
                    print(obj["choices"][0]["delta"].get("content", ""), end="")
                elif obj.get("object") == "chat.completion.final":
                    print("\nbilling:", json.dumps(obj["billing"], ensure_ascii=False))

        # 3) 图像
        img = c.post("/v1/images/generations", headers=HEAD,
                     json={"model": "gpt-image-2.5", "prompt": "孔宣 蛋形态，3D 国风", "n": 1}).json()
        print("\nimage:", img["data"][0]["url"], "|", img["billing"]["charged_rmb"], "元")

        # 4) 视频（异步 + 等待）
        v = c.post("/v1/videos/generations", headers=HEAD,
                   json={"model": "seedance-2.5", "prompt": "蛋壳裂开，金红光芒",
                         "resolution": "480P", "duration": 5}).json()
        print("\nvideo submitted:", v["id"], "预扣", v["billing"]["quoted_rmb"], "元")
        done = c.get(f"/v1/videos/{v['id']}/wait?timeout_s=10", headers=HEAD).json()
        print("video done:", done["status"], "|", json.dumps(done.get("billing"), ensure_ascii=False))

        print("\nwallet:", json.dumps(c.get("/v1/wallet", headers=HEAD).json(), ensure_ascii=False))
        report = c.get("/admin/report?days=1").json()
        print("report:", json.dumps(report["totals"], ensure_ascii=False))
        for row in report["by_model"]:
            print(f"  {row['model']}: {row['n']} 次, 收入 {row['revenue']} 元, 成本 {row['cost']} 元, "
                  f"毛利 {row['margin_pct']}%")


if __name__ == "__main__":
    main()
