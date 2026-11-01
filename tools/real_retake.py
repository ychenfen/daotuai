"""真实验证“按质检意见重抽”：对指定镜头重抽一次，等出片和自动质检，打印前后对比。会真实花钱。
用法（服务器上）：venv/bin/python tools/real_retake.py <项目ID> 1-5 1-9 [--base http://127.0.0.1:8011]
"""
import argparse, json, time, urllib.request
ap = argparse.ArgumentParser(); ap.add_argument("project"); ap.add_argument("codes", nargs="+")
ap.add_argument("--base", default="http://127.0.0.1:8011")
ap.add_argument("--requc", action="store_true", help="重抽前先按当前质检规则重新质检一次（约 0.5 分钱/镜）")
a = ap.parse_args()
B, P, CODES = a.base, a.project, a.codes
def req(m, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=m, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=600) as f: return json.loads(f.read())
    except urllib.error.HTTPError as e: return {"_status": e.code, "_body": e.read().decode()[:800]}
def proj(): return req("GET", f"/projects/{P}")
def show(s, tag):
    j = s["job"] or {}; q = j.get("qc") or {}
    print(f"[{tag}] {s['code']} 选用 v{j.get('version')} 质检 {q.get('score')} {q.get('verdict')} 人物 {q.get('character_consistency')} 分镜 {q.get('prompt_adherence')}")
    for i in (q.get("issues") or [])[:4]: print("     -", i.get("severity",""), i.get("type",""), i.get("detail","")[:90])
p = proj(); print("项目", p["name"], "花费", p["spend"]["total_rmb"], "剩余", p["spend"]["remaining_rmb"], "出片模型", p["settings"]["video_model"], p["settings"]["resolution"])
tg = {s["code"]: s for s in p["shots"] if s["code"] in CODES}
for c in CODES: show(tg[c], "重抽前")
ids = [tg[c]["id"] for c in CODES]
if a.requc:
    for c in CODES:
        q = req("POST", f"/jobs/{tg[c]['job']['id']}/qc?force=true")
        print(f"[按新规则重新质检] {c} {q.get('score')} {q.get('verdict')}")
        for i in (q.get("issues") or [])[:4]: print("     -", i.get("severity",""), i.get("type",""), i.get("detail","")[:90])
r = req("POST", f"/projects/{P}/retake", {"shot_ids": ids})
if "_status" in r: print("重抽失败", r); raise SystemExit(1)
print("报价", r["quote"]["total_rmb"], "提交", [(x["code"], "v%d" % x["version"]) for x in r["submitted"]], "跳过", r["skipped"])
for pl in r["plans"]: print(" 改写", pl["code"], pl["source"], "；".join(pl["changes"])[:300])
new = {x["shot"]: x["job"] for x in r["submitted"]}
t0 = time.time()
while time.time() - t0 < 1800:
    time.sleep(20); req("POST", f"/projects/{P}/videos/poll")
    st = {j["id"]: j["status"] for s in proj()["shots"] for j in s["jobs"] if j["id"] in new.values()}
    if all(v in ("succeeded", "failed", "cancelled") for v in st.values()): break
print("出片", st, "用时 %.0f 秒" % (time.time() - t0))
for _ in range(30):   # 等后台自动质检 + 择优
    p = proj(); js = {j["id"]: j for s in p["shots"] for j in s["jobs"]}
    if all(js[j].get("qc") or js[j]["status"] != "succeeded" for j in new.values()): break
    time.sleep(10)
time.sleep(5); p = proj()
for s in p["shots"]:
    if s["id"] in new:
        nj = next(j for j in s["jobs"] if j["id"] == new[s["id"]]); q = nj.get("qc") or {}
        print(f"[新版本] {s['code']} v{nj['version']} {nj['status']} 质检 {q.get('score')} {q.get('verdict')} 人物 {q.get('character_consistency')} 分镜 {q.get('prompt_adherence')} 花费 {nj.get('charged_rmb')}")
        for i in (q.get("issues") or [])[:4]: print("     -", i.get("severity",""), i.get("type",""), i.get("detail","")[:90])
        show(s, "现在选用")
print("花费", p["spend"])
print("通知", [n["title"] for n in req("GET", "/notifications?limit=6")["items"]])
