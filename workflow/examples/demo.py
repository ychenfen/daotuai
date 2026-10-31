"""端到端演示：建项目 → 剧本 → 一键（分镜/提示词/视频）→ 轮询 → 打印结果。

前置：聚合层 :8010、工作流 :8011 都在跑。
"""
import json
import time

import httpx

WF = "http://127.0.0.1:8011"
AGG = "http://127.0.0.1:8010"

SCRIPT = """第1集
△五只青鸾鸟背上的冒牌捕妖队俯视金鸡岭全貌，狂风猎猎，青鸾鸟从云层中穿梭而过。
猪婆龙（指向彩蛋）：那里！
△二狗子和鹅鹏落在彩蛋前，躬身行礼。
猪婆龙：谁是管事的，站出来！
△众妖面面相觑，一齐指向萤姐。

第2集
△蛋壳在暗红火光里裂开，金红色的光透出来。
孔宣从壳里探出头，茫然地看着四周。
萤姐：别怕，跟着我。
"""


def main() -> None:
    with httpx.Client(base_url=WF, timeout=300, trust_env=False) as c:
        print("health:", c.get("/healthz").json())
        proj = c.post("/projects", json={"name": "内测短剧", "style": "3D"}).json()
        pid = proj["id"]
        print("project:", pid)

        c.post(f"/projects/{pid}/script", json={"text": SCRIPT})
        run = c.post(f"/projects/{pid}/run?max_shots=4&resolution=480P").json()
        print("storyboard:", json.dumps(run["storyboard"], ensure_ascii=False))
        print("prompts:", run["prompts"])
        print("videos submitted:", len(run["videos"]["submitted"]))

        for _ in range(20):
            state = c.get(f"/projects/{pid}").json()
            if state["status"] in ("videos_done", "videos_running"):
                if all(s["status"] in ("video_done", "video_failed") for s in state["shots"]):
                    break
            c.post(f"/projects/{pid}/videos/poll")
            time.sleep(1.0)

        state = c.get(f"/projects/{pid}").json()
        print(f"\n项目状态：{state['status']}  镜头 {len(state['shots'])} 个")
        for s in state["shots"][:6]:
            job = s.get("job") or {}
            print(f"  E{s.get('episode_no', '?')}-{s['shot_no']} {s['size']}/{s['angle']} {s['duration_s']}s "
                  f"[{s['status']}] {job.get('url','')[:46]}  ¥{job.get('charged_rmb', 0)}")

    with httpx.Client(base_url=AGG, timeout=30, trust_env=False) as a:
        rep = a.get("/admin/report?days=1").json()
        print("\n聚合层报表:", json.dumps(rep["totals"], ensure_ascii=False))
        for row in rep["by_model"]:
            print(f"  {row['model']}: {row['n']} 次 收入 {row['revenue']} 成本 {row['cost']} 毛利 {row['margin_pct']}%")
        w = a.get("/v1/wallet", headers={"X-User-Id": "studio"}).json()
        print("钱包:", json.dumps({k: w[k] for k in ("balance_rmb", "total_spent_rmb", "total_cost_rmb")},
                                ensure_ascii=False))


if __name__ == "__main__":
    main()
