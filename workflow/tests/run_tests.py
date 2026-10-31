"""工作流端到端测试：聚合层与工作流都在同一进程（ASGI 直连，不联网不花钱）。

覆盖：建项目 → 剧本 → 分集 → 分镜 → 提示词 → 提交视频 → 轮询回执 → 账本扣费。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # workflow/
WS = os.path.dirname(ROOT)                                                  # 工作区
AGG = os.path.join(WS, "aggregator")
sys.path.insert(0, ROOT)
sys.path.insert(0, AGG)

# 关键：先定好两套 DB 与模式，再 import
for f in ("wf_test.db", "agg_test.db"):
    p = os.path.join(ROOT, "data", f)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(p + suffix):
            os.remove(p + suffix)
os.environ["WORKFLOW_DB"] = os.path.join(ROOT, "data", "wf_test.db")
os.environ["AGG_DB"] = os.path.join(ROOT, "data", "agg_test.db")
os.environ["WORKFLOW_TEST_AGG"] = "asgi"
os.environ["WORKFLOW_LLM"] = "mock"
os.environ["WORKFLOW_POLL_INTERVAL_S"] = "0"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402

from wf import main as wf_main  # noqa: E402

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


SCRIPT = """第1集
△五只青鸾鸟背上的冒牌捕妖队俯视金鸡岭全貌，狂风猎猎，青鸾鸟从云层中穿梭而过。
猪婆龙（指向彩蛋）：那里！
△二狗子和鹅鹏落在彩蛋前，躬身行礼。
猪婆龙：谁是管事的，站出来！

第2集
△蛋壳裂开，金红色的光透出来。
孔宣从壳里探出头，茫然地看着四周。
萤姐：别怕，跟着我。
"""


async def main() -> int:
    transport = httpx.ASGITransport(app=wf_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://wf", timeout=120) as c:
        print("\n[A] 健康与建项目")
        h = (await c.get("/healthz")).json()
        check("healthz", h["ok"] is True and h["llm_mode"] == "mock", json.dumps(h))

        proj = (await c.post("/projects", json={"name": "内测项目", "style": "真人"})).json()
        pid = proj["id"]
        check("建项目 draft", proj["status"] == "draft", json.dumps(proj)[:160])

        print("\n[B] 剧本与分集")
        r = (await c.post(f"/projects/{pid}/script", json={"text": SCRIPT})).json()
        check("写入剧本", r["status"] == "script_ready" and "青鸾鸟" in r["script_text"], r["status"])
        r = (await c.post(f"/projects/{pid}/parse")).json()
        check("分成 2 集", len(r["episodes"]) == 2, str(len(r["episodes"])))
        check("解析后状态 parsed", r["status"] == "parsed", r["status"])

        print("\n[C] 分镜（第 1 集）")
        sb = (await c.post(f"/projects/{pid}/storyboard?episode_no=1&max_shots=5")).json()
        check("分镜生成成功", sb["results"][0]["source"] == "mock" and sb["results"][0]["shots"] >= 3,
              json.dumps(sb["results"]))
        shots = [s for s in sb["state"]["shots"] if s["shot_no"]]
        check("镜头落在库里", len(shots) >= 3, str(len(shots)))
        check("镜头有画面描述", all(s["picture"] for s in shots), json.dumps(shots[0])[:160])
        check("时长在 4~15 秒", all(4 <= s["duration_s"] <= 15 for s in shots),
              str([s["duration_s"] for s in shots]))

        # 幂等：再跑一次不重复
        sb2 = (await c.post(f"/projects/{pid}/storyboard?episode_no=1&max_shots=5")).json()
        check("重复分镜幂等（跳过）", sb2["results"][0].get("skipped") is True, json.dumps(sb2["results"]))

        print("\n[D] 提示词")
        pr = (await c.post(f"/projects/{pid}/prompts")).json()
        check("提示词生成", pr["generated"] >= 3 and pr["source"] == "mock", json.dumps(pr)[:160])
        with_prompt = [s for s in pr["state"]["shots"] if s.get("prompt")]
        check("每个镜头都有提示词", len(with_prompt) == len(shots), f"{len(with_prompt)}/{len(shots)}")
        check("positive 含时间轴", "秒" in with_prompt[0]["prompt"]["positive"],
              with_prompt[0]["prompt"]["positive"][:80])

        print("\n[E] 提交视频（走聚合层 → mock 上游）")
        v = (await c.post(f"/projects/{pid}/videos?resolution=480P")).json()
        check("提交成功", len(v["submitted"]) == len(shots), json.dumps(v)[:220])
        check("预扣金额 > 0", all((x["quoted_rmb"] or 0) > 0 for x in v["submitted"]),
              json.dumps(v["submitted"])[:200])
        check("项目状态 videos_running", v["submitted"] and True)

        st = (await c.get(f"/projects/{pid}")).json()
        check("镜头状态 video_submitted",
              all(s["status"] in ("video_submitted", "video_done") for s in st["shots"]),
              str({s["status"] for s in st["shots"]}))

        print("\n[F] 轮询回执")
        p1 = (await c.post(f"/projects/{pid}/videos/poll")).json()
        check("第一轮全部完成", len(p1["done"]) == len(shots), json.dumps(p1)[:220])
        check("拿到视频 url", all(d["url"] for d in p1["done"]), json.dumps(p1["done"])[:200])
        check("记录了实际扣费", all(d["charged_rmb"] > 0 for d in p1["done"]), json.dumps(p1["done"])[:200])

        st = (await c.get(f"/projects/{pid}")).json()
        check("项目状态 videos_done", st["status"] == "videos_done", st["status"])
        check("镜头状态 video_done", all(s["status"] == "video_done" for s in st["shots"]),
              str({s["status"] for s in st["shots"]}))

        print("\n[G] 账本（聚合层）")
        from app.main import app as agg_app  # noqa: E402
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agg_app),
                                     base_url="http://agg", timeout=30) as ac:
            w = (await ac.get("/v1/wallet", headers={"X-User-Id": "studio"})).json()
        check("有钱包记录", w["total_spent_rmb"] > 0, json.dumps(w))
        check("无未结算预扣", w["open_holds"]["count"] == 0, json.dumps(w["open_holds"]))

        print("\n[H] 一键跑通（新项目，2 集一起）")
        p2 = (await c.post("/projects", json={"name": "一键项目"})).json()["id"]
        await c.post(f"/projects/{p2}/script", json={"text": SCRIPT})
        run = (await c.post(f"/projects/{p2}/run?max_shots=4&resolution=480P")).json()
        check("一键：两集都出分镜", len(run["storyboard"]) == 2, json.dumps(run["storyboard"]))
        check("一键：提示词已生成", run["prompts"] >= 4, str(run["prompts"]))
        check("一键：视频已提交", len(run["videos"]["submitted"]) >= 4, json.dumps(run["videos"])[:200])
        final = (await c.get(f"/projects/{p2}")).json()
        check("一键：镜头全部提交", all(s["status"] in ("video_submitted", "video_done") for s in final["shots"]),
              str({s["status"] for s in final["shots"]}))

        print("\n[I] 边界")
        miss = await c.get("/projects/proj_nope")
        check("不存在项目 404", miss.status_code == 404, str(miss.status_code))
        empty = await c.post(f"/projects/{p2}/script", json={"text": "   "})
        check("空剧本 400", empty.status_code == 400, str(empty.status_code))
        nob = (await c.post("/projects", json={"name": "空项目"})).json()["id"]
        bad = await c.post(f"/projects/{nob}/storyboard")
        check("无剧本分镜 400", bad.status_code == 400, str(bad.status_code))

    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for name, detail in FAIL:
        print(f"  FAILED: {name} :: {detail[:220]}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
