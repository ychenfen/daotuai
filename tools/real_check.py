"""真实渠道打通：驱动正在运行的工作台（默认 http://127.0.0.1:8011），用真实模型走一遍，花费有上限。

    py -3.12 tools/real_check.py --idea "妖王来到人界，发现天赋能炒股" --episodes 3 --budget 2.5

步骤：一键剧本(文本) → 解析 → AI 抽资产(文本) → 主角出图(图像×1) → AI 分镜(文本) → AI 提示词(文本)
→ 第 1 镜分镜图(图像×1) → 第 1 镜出片(视频×1，首帧参考，5 秒) → 等回执 → 下载 → 合成第 1 集。
项目预算 = --budget；视频提交前工作台按报价检查预算，超了不会提交。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.environ.get("WORKBENCH", "http://127.0.0.1:8011")


def call(method: str, path: str, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} → {e.code} {e.read().decode('utf-8', 'ignore')[:600]}")


def step(name: str):
    print(f"\n== {name}", flush=True)
    return time.time()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idea", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--episodes", type=int, default=3)
    ap.add_argument("--budget", type=float, default=2.5)
    ap.add_argument("--text-model", default="deepseek-chat")
    ap.add_argument("--image-model", default="seedream-5.0-flash")
    ap.add_argument("--video-model", default="ark-seedance-2.0-fast")
    ap.add_argument("--skip-video", action="store_true", help="只验证文本+出图（视频模型还没开通时用）")
    ap.add_argument("--wait", type=int, default=900, help="等视频回执的最长秒数")
    a = ap.parse_args()

    h = call("GET", "/healthz")
    print("工作台：", json.dumps(h, ensure_ascii=False))
    missing = [m for m in (a.text_model, a.image_model) + (() if a.skip_video else (a.video_model,)) if m not in h.get("live_models", [])]
    if missing:
        print("[x] 这些模型当前不是真实渠道：", missing)
        return 1

    t = step("建项目")
    p = call("POST", "/projects", {"name": a.name or "真实打通·" + a.idea[:10], "style": "3D 国漫",
                                   "settings": {"text_model": a.text_model, "image_model": a.image_model,
                                                "video_model": a.video_model, "resolution": "480P",
                                                "budget_rmb": a.budget, "engine": "auto", "shot_seconds": 5,
                                                "image_resolution": "2K", "gen_frames": False}})
    pid = p["id"]
    print("项目", pid, p["name"])

    t = step("一键剧本（真实文本模型）")
    r = call("POST", f"/projects/{pid}/script/generate", {"idea": a.idea, "episodes": a.episodes, "seconds_per_ep": 60})
    print(f"来源 {r['generated_source']}  {r['script_check']}  {time.time() - t:.1f}s")
    if r["generated_source"] != "real":
        print("[x] 文本模型没调通（退回了本地模板），看 data/logs/workflow.log"); return 1
    print(r["script_text"][:600])

    t = step("全剧解析")
    r = call("POST", f"/projects/{pid}/parse")
    print(f"{len(r['episodes'])} 集 {len(r['scenes'])} 场", [s["code"] + " " + s["location"] for s in r["scenes"]][:12])

    t = step("AI 抽取资产")
    r = call("POST", f"/projects/{pid}/assets/extract?engine=ai")
    if r["source"] != "real":
        print("[x] AI 抽取没调通"); return 1
    print(f"来源 {r['source']}  新增 {len(r['added'])}：", [(x["kind"], x["name"], x["forms"]) for x in r["added"]][:20])
    chars = [x for x in r["state"]["assets"] if x["kind"] == "character"]
    if not chars:
        print("[x] 没抽到角色"); return 1
    hero = next((x for x in chars if "孔宣" in x["name"] or "妖王" in x["name"]), chars[0])

    t = step(f"主角出图（{a.image_model}）：{hero['name']}")
    r = call("POST", f"/assets/{hero['id']}/image")
    f0 = r["forms"][0]
    v = f0["versions"][-1] if f0["versions"] else {}
    size = os.path.getsize(v["local_path"]) if v.get("local_path") and os.path.exists(v["local_path"]) else 0
    print(f"v{f0['current_version']} 来源 {v.get('source')}  本地 {size} 字节  {time.time() - t:.1f}s  {v.get('display_url')}")

    t = step("AI 分镜（第 1 集）")
    r = call("POST", f"/projects/{pid}/storyboard?episode_no=1")
    print(f"来源 {r['results'][0]['source']}  {r['results'][0]['shots']} 卡  {time.time() - t:.1f}s")
    s1 = next(s for s in r["state"]["shots"] if s["episode_no"] == 1)
    print("1-1 画面：", s1["picture"][:200])

    t = step("AI 提示词（第 1 集）")
    r = call("POST", f"/projects/{pid}/prompts?episode_no=1")
    s1 = next(s for s in r["state"]["shots"] if s["id"] == s1["id"])
    print(f"来源 {r['source']}  {r['generated']} 条  {time.time() - t:.1f}s")
    print(s1["prompt"]["positive"][:500])
    print("绑定：", [b["mention"] for b in s1["prompt"]["bindings"]], "未解析：", s1["prompt"]["unresolved"])

    t = step("第 1 镜分镜图（带主角参考图）")
    r = call("POST", f"/projects/{pid}/frames", {"shot_ids": [s1["id"]]})
    print(json.dumps(r, ensure_ascii=False)[:300], f"{time.time() - t:.1f}s")

    if a.skip_video:
        st = call("GET", f"/projects/{pid}")
        print("\n== 花费", json.dumps(st["spend"], ensure_ascii=False))
        print("项目地址", f"{BASE}/ui/#/board/{pid}")
        print("REAL_CHECK", "PASS（文本+出图；视频跳过）")
        return 0

    t = step(f"第 1 镜出片（{a.video_model}，首帧参考，5 秒 480P）")
    q = call("POST", f"/projects/{pid}/videos/quote", {"shot_ids": [s1["id"]], "duration": 5})
    print(f"报价 ¥{q['total_rmb']}  剩余预算 ¥{q['remaining_rmb']}  真实={q['live']}")
    r = call("POST", f"/projects/{pid}/videos", {"shot_ids": [s1["id"]], "duration": 5, "mode": "first_frame"})
    print(json.dumps({"submitted": r["submitted"], "skipped": r["skipped"]}, ensure_ascii=False)[:600])
    if not r["submitted"]:
        print("[x] 没提交成功"); return 1

    t = step("等回执")
    deadline = time.time() + a.wait
    job = None
    while time.time() < deadline:
        call("POST", f"/projects/{pid}/videos/poll")
        st = call("GET", f"/projects/{pid}")
        job = next(s for s in st["shots"] if s["id"] == s1["id"])["job"]
        print(f"  {int(time.time() - t)}s  {job['status']}  {job['error'][:120]}", flush=True)
        if job["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(15)
    ok = bool(job and job["status"] == "succeeded")
    if ok:
        size = os.path.getsize(job["local_path"]) if job["local_path"] and os.path.exists(job["local_path"]) else 0
        print(f"[ok] 出片成功 {job['seconds']}s  扣费 ¥{job['charged_rmb']}  本地 {size} 字节  {job['play_url']}")
        t = step("合成第 1 集（只用已出片的镜头）")
        try:
            r = call("POST", f"/projects/{pid}/assemble?episode_no=1&allow_partial=true")
            print(json.dumps(r["episodes"], ensure_ascii=False))
        except RuntimeError as e:
            print("[x] 合成失败", e)
    st = call("GET", f"/projects/{pid}")
    print("\n== 花费", json.dumps(st["spend"], ensure_ascii=False))
    print("项目地址", f"{BASE}/ui/#/videos/{pid}")
    print("REAL_CHECK", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 打印完整原因，方便排查上游报错
        print("[x] 中断：", e)
        sys.exit(2)
