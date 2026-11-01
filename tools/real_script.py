"""真实验证剧本增强：新建项目 → 分层写剧本（真实文本模型）→ 打印圣经、节拍、评级、第 1 集正文与指标。只花文本费（几分钱）。
用法（服务器上）：venv/bin/python tools/real_script.py "灵感" [--episodes 3] [--seconds 60] [--genre 穿越]
"""
import argparse, json, urllib.request
ap = argparse.ArgumentParser(); ap.add_argument("idea"); ap.add_argument("--episodes", type=int, default=3)
ap.add_argument("--seconds", type=int, default=60); ap.add_argument("--genre", default="")
ap.add_argument("--base", default="http://127.0.0.1:8011"); a = ap.parse_args()
def req(m, path, body=None):
    r = urllib.request.Request(a.base + path, data=json.dumps(body).encode() if body is not None else None, method=m,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=900) as f: return json.loads(f.read())
    except urllib.error.HTTPError as e: return {"_status": e.code, "_body": e.read().decode()[:800]}
p = req("POST", "/projects", {"name": "剧本增强验证·" + a.idea[:8], "style": "真人", "settings": {"budget_rmb": 1}})
pid = p["id"]; print("项目", pid)
r = req("POST", f"/projects/{pid}/script/generate", {"idea": a.idea, "episodes": a.episodes, "seconds_per_ep": a.seconds,
                                                   "genre": a.genre})
if "_status" in r: print("失败", r); raise SystemExit(1)
b = r["bible"]; print("来源", r["generated_source"], "｜剧名", b["title"], "｜一句话", b["logline"])
for c in b["characters"]: print("  人设", c.get("name"), "｜", c.get("tags"), "｜欲望", c.get("desire"), "｜软肋", c.get("weakness"), "｜外貌", c.get("look"))
for x in b["props"]: print("  道具", x["name"], "｜别写成", x.get("avoid"))
for x in b["beats"]: print(f"  第{x['ep']}集 {x['mode']}{' ⭐卡点' if x['paywall'] else ''}｜开场 {x['hook_open']}｜反转 {x['twist']}｜爽点 {x['payoff']}｜集末 {x['hook_end']}（{x['hook_type']}）")
for k in sorted(r["reviews"], key=int):
    v = r["reviews"][k]; m = v["metrics"]
    print(f"== 第{k}集 评级 {v['grade']}（过程 {'→'.join(v.get('history', []))}）审核来源 {v.get('source')} 分项 {v.get('scores')}")
    print(f"   结论：{v.get('summary', '')[:120]}")
    print(f"   时长 标注{m['declared_seconds']}s/估算{m['estimated_seconds']}s/目标{m['target_seconds']}s 贴字{m['tiezi']} 长句{len(m['long_lines'])} 人数超{len(m['crowded_scenes'])} 屏幕字未贴{len(m['screen_text_unmarked'])} 道具写错{m['misnamed_props']}")
    for i in v["issues"][:5]: print("   -", i["severity"], i.get("scene", ""), i["detail"][:80])
print("== 第 1 集正文 ==")
print(r["script_text"].split("第2集")[0][:2500])
print("花费", req("GET", f"/projects/{pid}")["spend"])
