"""工作流端到端测试：聚合层与工作流都在同一进程（ASGI 直连），全部走 mock，不联网不花钱。

覆盖：建项目 → 剧本 → 分集 → 资产抽取 → 参考图（多版本/上传/切换）→ 分镜（站位）→ 分镜图
→ 提示词（@绑定/未解析）→ 字段级冲突 → 报价与预算 → 带参考图提交 → 回执下载 → 单镜重做
→ 按集合成（字幕）→ 一键后台任务 → 边界。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # workflow/
WS = os.path.dirname(ROOT)                                                  # 工作区
AGG = os.path.join(WS, "aggregator")
sys.path.insert(0, ROOT)
sys.path.insert(0, AGG)

TMP = tempfile.mkdtemp(prefix="wf_test_")
os.environ["WORKFLOW_DB"] = os.path.join(TMP, "wf_test.db")
os.environ["AGG_DB"] = os.path.join(TMP, "agg_test.db")
os.environ["WORKFLOW_MEDIA"] = os.path.join(TMP, "media")
os.environ["WORKFLOW_TEST_AGG"] = "asgi"
os.environ["WORKFLOW_LLM"] = "mock"
os.environ["AGG_FORCE_MOCK"] = "1"          # 电脑上有真实 key，测试必须强制 mock
os.environ["WORKFLOW_POLL_INTERVAL_S"] = "0"
os.environ["WORKFLOW_WAIT_INTERVAL_S"] = "0.05"
os.environ["DAOTU_ENV_FILE"] = os.path.join(TMP, "none.env")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging  # noqa: E402

import httpx  # noqa: E402

for _n in ("httpx", "agg", "workflow"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from wf import main as wf_main  # noqa: E402
from wf import media  # noqa: E402

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []
HAS_FFMPEG = bool(media.ffmpeg_bin())


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


SCRIPT = """第1集
1-1 日 外 金鸡岭
△五只青鸾鸟背上的冒牌捕妖队俯视金鸡岭全貌，狂风猎猎，青鸾鸟从云层中穿梭而过。
猪婆龙（指向彩蛋）：那里！
△二狗子和鹅鹏落在彩蛋前，躬身行礼。
猪婆龙：谁是管事的，站出来！
二狗子：在下二狗子，见过龙爷。

第2集
1-2 夜 内 山洞
△蛋壳裂开，金红色的光透出来。
孔宣从壳里探出头，茫然地看着四周。
萤姐：别怕，跟着我。
孔宣：你是谁？
"""


async def j(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300], "_status": r.status_code}


async def main() -> int:
    transport = httpx.ASGITransport(app=wf_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://wf", timeout=120) as c:
        print("\n[A] 健康与建项目")
        h = await j(await c.get("/healthz"))
        check("healthz：聚合层可达、mock 模式", h["ok"] and h["aggregator"] and h["llm_mode"] == "mock", json.dumps(h))
        check("强制 mock 下没有真实模型", h["live_models"] == [], str(h["live_models"]))
        ui = await c.get("/ui/")
        check("网页界面可打开", ui.status_code == 200 and "<html" in ui.text.lower(), str(ui.status_code))

        proj = await j(await c.post("/projects", json={"name": "妖王", "style": "国漫",
                                                         "settings": {"budget_rmb": 200, "gen_frames": True}}))
        pid = proj["id"]
        check("建项目 draft + 设置生效", proj["status"] == "draft" and proj["settings"]["budget_rmb"] == 200
              and proj["settings"]["gen_frames"] is True, json.dumps(proj)[:200])
        bad = await c.patch(f"/projects/{pid}/settings", json={"video_mode": "乱写"})
        check("非法设置 400", bad.status_code == 400, str(bad.status_code))

        print("\n[B] 剧本与分集")
        r = await j(await c.post(f"/projects/{pid}/script", json={"text": SCRIPT}))
        check("写入剧本", r["status"] == "script_ready", r["status"])
        r = await j(await c.post(f"/projects/{pid}/parse"))
        check("分成 2 集", len(r["episodes"]) == 2, str(len(r["episodes"])))

        print("\n[C] 资产抽取")
        r = await j(await c.post(f"/projects/{pid}/assets/extract"))
        names = {a["name"]: a for a in r["state"]["assets"]}
        check("抽到角色", {"猪婆龙", "二狗子", "萤姐", "孔宣"} <= set(names), str(list(names)))
        check("抽到场景", {"金鸡岭", "山洞"} <= set(names), str(list(names)))
        check("不把'第1集'当角色", not any(n.startswith("第") for n in names), str(list(names)))
        r2 = await j(await c.post(f"/projects/{pid}/assets/extract"))
        check("重复抽取不重复建", len(r2["added"]) == 0 and len(r2["state"]["assets"]) == len(names),
              str(len(r2["added"])))
        added = await j(await c.post(f"/projects/{pid}/assets", json={"kind": "prop", "name": "彩蛋",
                                                                      "description": "金红色巨蛋"}))
        check("手动加道具", added["kind"] == "prop" and added["name"] == "彩蛋", json.dumps(added)[:120])
        dup = await c.post(f"/projects/{pid}/assets", json={"kind": "prop", "name": "彩蛋"})
        check("同名资产 400", dup.status_code == 400, str(dup.status_code))

        print("\n[D] 资产参考图（版本）")
        r = await j(await c.post(f"/projects/{pid}/assets/images"))
        check("全部出图", len(r["generated"]) == len(names) + 1 and not r["failed"], json.dumps(r)[:200])
        st = await j(await c.get(f"/projects/{pid}"))
        kx = next(a for a in st["assets"] if a["name"] == "孔宣")
        check("参考图落到本地", kx["current_version"] == 1 and os.path.exists(kx["local_path"]), json.dumps(kx)[:200])
        check("参考图可通过 /media 访问", (await c.get(kx["display_url"])).status_code == 200, kx["display_url"])
        kx2 = await j(await c.post(f"/assets/{kx['id']}/image"))
        check("重新出图 → v2", kx2["current_version"] == 2 and len(kx2["versions"]) == 2, str(kx2["current_version"]))
        up = await j(await c.post(f"/assets/{kx['id']}/upload?filename=kx.png", content=b"\x89PNG fake"))
        check("上传参考图 → v3", up["current_version"] == 3 and up["versions"][-1]["source"] == "uploaded",
              str(up.get("current_version")))
        sel = await j(await c.post(f"/assets/{kx['id']}/select?version=2"))
        check("切回 v2", sel["current_version"] == 2, str(sel.get("current_version")))
        st = await j(await c.get(f"/projects/{pid}"))
        check("项目花费含出图", st["spend"]["images_rmb"] > 0, json.dumps(st["spend"]))

        print("\n[E] 分镜（含站位、出场角色）")
        sb = await j(await c.post(f"/projects/{pid}/storyboard?max_shots=6"))
        check("两集都拆了镜头", len(sb["results"]) == 2 and all(x["shots"] >= 3 for x in sb["results"]),
              json.dumps(sb["results"]))
        shots = sb["state"]["shots"]
        check("镜头带出场角色", any("猪婆龙" in s["characters"] for s in shots), str([s["characters"] for s in shots]))
        check("镜头绑定场景资产", {s["scene_asset"] for s in shots} >= {"金鸡岭", "山洞"},
              str({s["scene_asset"] for s in shots}))
        check("有站位描述", any(s["blocking"] for s in shots), "")
        check("时长在 4~15 秒", all(4 <= s["duration_s"] <= 15 for s in shots), str([s["duration_s"] for s in shots]))
        sb2 = await j(await c.post(f"/projects/{pid}/storyboard"))
        check("重复分镜幂等（跳过）", all(x.get("skipped") for x in sb2["results"]), json.dumps(sb2["results"]))
        pf = await c.post(f"/projects/{pid}/parse")
        check("已有分镜时重新分集需 force", pf.status_code == 400, str(pf.status_code))

        print("\n[F] 分镜图")
        fr = await j(await c.post(f"/projects/{pid}/frames", json={}))
        check("每镜一张分镜图", len(fr["generated"]) == len(shots) and not fr["failed"], json.dumps(fr)[:200])
        st = await j(await c.get(f"/projects/{pid}"))
        check("分镜图可访问", all(s["frame"] for s in st["shots"]), "")

        print("\n[G] 提示词与 @绑定")
        pr = await j(await c.post(f"/projects/{pid}/prompts"))
        check("提示词生成", pr["generated"] == len(shots) and pr["source"] == "mock", json.dumps(pr)[:160])
        shots = pr["state"]["shots"]
        s0 = next(s for s in shots if "猪婆龙" in s["characters"])
        check("提示词里用 @ 引用角色", "@猪婆龙" in s0["prompt"]["positive"], s0["prompt"]["positive"][:120])
        b_names = {b["name"] for b in s0["prompt"]["bindings"]}
        check("@ 解析成资产绑定", {"猪婆龙", "金鸡岭"} <= b_names, str(b_names))
        check("绑定带版本号", all(b["version"] >= 1 for b in s0["prompt"]["bindings"]), "")
        check("没有未解析的 @", all(not s["prompt"]["unresolved"] for s in shots), "")
        e = await j(await c.patch(f"/prompts/{s0['prompt']['id']}", json={
            "fields": {"positive": s0["prompt"]["positive"] + " @不存在的人"},
            "expected_rev": s0["prompt"]["rev"]}))
        st = await j(await c.get(f"/projects/{pid}"))
        s0 = next(s for s in st["shots"] if s["id"] == s0["id"])
        check("未知 @ 列入 unresolved", "不存在的人" in s0["prompt"]["unresolved"], str(s0["prompt"]["unresolved"]))
        await c.patch(f"/prompts/{s0['prompt']['id']}", json={
            "fields": {"positive": s0["prompt"]["positive"].replace(" @不存在的人", "")}})

        print("\n[H] 字段级冲突（刀徒 325 次冲突的反面）")
        s1 = st["shots"][1]
        r1 = await j(await c.patch(f"/shots/{s1['id']}", json={"fields": {"picture": "A 改的画面"},
                                                                "expected_rev": s1["rev"]}))
        check("第一次保存成功", r1["changed"] == ["picture"] and r1["rev"] == s1["rev"] + 1, json.dumps(r1))
        r2 = await j(await c.patch(f"/shots/{s1['id']}", json={"fields": {"dialogue": "B 改的台词"},
                                                                "expected_rev": s1["rev"]}))
        check("旧版本号改别的字段：自动合并", r2.get("merged") is True and r2["changed"] == ["dialogue"], json.dumps(r2))
        r3 = await c.patch(f"/shots/{s1['id']}", json={"fields": {"picture": "B 改的画面"}, "expected_rev": s1["rev"]})
        body3 = await j(r3)
        check("旧版本号改同一字段：409", r3.status_code == 409, str(r3.status_code))
        check("409 带冲突字段与双方取值", body3["error"]["detail"]["conflict_fields"] == ["picture"]
              and body3["error"]["detail"]["current"]["picture"] == "A 改的画面", json.dumps(body3)[:300])
        r4 = await j(await c.patch(f"/shots/{s1['id']}", json={"fields": {"picture": "A 改的画面"},
                                                                "expected_rev": s1["rev"]}))
        check("改成相同值不算冲突", r4["changed"] == [], json.dumps(r4))
        rb = await c.patch(f"/shots/{s1['id']}", json={"fields": {"status": "x"}})
        check("系统字段不能改", rb.status_code == 400, str(rb.status_code))
        rev_before = (await j(await c.get(f"/projects/{pid}")))["shots"][1]["rev"]

        print("\n[I] 报价与预算")
        q = await j(await c.post(f"/projects/{pid}/videos/quote", json={}))
        check("报价覆盖全部镜头", len(q["items"]) == len(shots) and q["total_rmb"] > 0, json.dumps(q)[:200])
        check("预算内", q["within_budget"] is True, json.dumps(q)[:200])
        await c.patch(f"/projects/{pid}/settings", json={"budget_rmb": 1})
        over = await c.post(f"/projects/{pid}/videos", json={})
        ob = await j(over)
        check("超预算 402 且不提交", over.status_code == 402 and "预算" in ob["error"]["message"], json.dumps(ob)[:200])
        st = await j(await c.get(f"/projects/{pid}"))
        check("超预算时一个任务都没建", all(not s["jobs"] for s in st["shots"]), "")
        await c.patch(f"/projects/{pid}/settings", json={"budget_rmb": 200})

        print("\n[J] 提交视频（全能参考）")
        v = await j(await c.post(f"/projects/{pid}/videos", json={"resolution": "480P"}))
        check("全部提交", len(v["submitted"]) == len(shots), json.dumps(v)[:240])
        check("有参考图的镜头走 reference 模式", any(x["mode"] == "reference" for x in v["submitted"]),
              str([x["mode"] for x in v["submitted"]]))
        st = await j(await c.get(f"/projects/{pid}"))
        jref = next(s["jobs"][0] for s in st["shots"] if s["jobs"] and s["jobs"][0]["mode"] == "reference")
        check("记录了参考的资产与版本", jref["refs"] and all("version" in x for x in jref["refs"]), json.dumps(jref["refs"]))
        kx_ref = [x for s in st["shots"] for jj in s["jobs"] for x in jj["refs"] if x.get("name") == "孔宣"]
        check("孔宣用的是当前选用的 v2", kx_ref and all(x["version"] == 2 for x in kx_ref), json.dumps(kx_ref))
        v2 = await j(await c.post(f"/projects/{pid}/videos", json={}))
        check("重复提交不重复下单", not v2["submitted"] and len(v2["skipped"]) == len(shots), json.dumps(v2)[:200])

        print("\n[K] 回执与下载")
        p1 = await j(await c.post(f"/projects/{pid}/videos/poll"))
        check("第一轮全部完成", len(p1["done"]) == len(shots) and not p1["running"], json.dumps(p1)[:200])
        st = await j(await c.get(f"/projects/{pid}"))
        check("每镜都选用了成功版本", all(s["selected_job_id"] and s["job"]["status"] == "succeeded"
                                         for s in st["shots"]), "")
        if HAS_FFMPEG:
            check("视频已下载到本地（mock 占位片）", all(os.path.exists(s["job"]["local_path"]) for s in st["shots"]), "")
        check("系统写回不推进用户对象版本号", st["shots"][1]["rev"] == rev_before,
              f"{st['shots'][1]['rev']} vs {rev_before}")
        check("项目状态 videos_done", st["status"] == "videos_done", st["status"])
        check("花费记账", st["spend"]["videos_rmb"] > 0, json.dumps(st["spend"]))

        print("\n[L] 单镜重做 + 选版本")
        target = st["shots"][0]
        rd = await j(await c.post(f"/projects/{pid}/videos", json={"shot_ids": [target["id"]], "mode": "text"}))
        check("单镜重做 → v2", len(rd["submitted"]) == 1 and rd["submitted"][0]["version"] == 2
              and rd["submitted"][0]["mode"] == "text", json.dumps(rd)[:200])
        await c.post(f"/projects/{pid}/videos/poll")
        st = await j(await c.get(f"/projects/{pid}"))
        t2 = next(s for s in st["shots"] if s["id"] == target["id"])
        check("新版本自动选用", t2["job"]["version"] == 2 and len(t2["jobs"]) == 2, str(t2["job"]["version"]))
        v1 = next(x for x in t2["jobs"] if x["version"] == 1)
        ok = await j(await c.post(f"/shots/{target['id']}/select?job_id={v1['id']}"))
        check("切回 v1", ok["selected_job_id"] == v1["id"], json.dumps(ok))

        print("\n[M] 按集合成（ffmpeg）")
        if HAS_FFMPEG:
            a = await j(await c.post(f"/projects/{pid}/assemble"))
            check("两集都合成", len(a["episodes"]) == 2, json.dumps(a)[:200])
            ep1 = a["episodes"][0]
            st = await j(await c.get(f"/projects/{pid}"))
            e1 = st["episodes"][0]
            check("成片文件存在", os.path.exists(e1["final_path"]), e1["final_path"])
            exp = sum(s["job"]["seconds"] for s in st["shots"] if s["episode_no"] == 1)
            info = media.probe(e1["final_path"])
            check("成片时长 ≈ 各镜之和", abs(info["duration"] - exp) < 1.0, f"{info['duration']} vs {exp}")
            srt = open(e1["srt_path"], encoding="utf-8").read()
            check("字幕含台词", "谁是管事的" in srt and "-->" in srt, srt[:120])
            check("成片可通过 /media 访问", (await c.get(ep1["url"])).status_code == 200, ep1["url"])
            check("项目状态 final_ready", st["status"] == "final_ready", st["status"])
        else:
            print("  （本机没有 ffmpeg，跳过合成）")

        print("\n[N] 一键（后台任务）")
        p2 = (await j(await c.post("/projects", json={"name": "一键项目",
                                                       "settings": {"gen_frames": True, "budget_rmb": 100}})))["id"]
        await c.post(f"/projects/{p2}/script", json={"text": SCRIPT})
        t = await j(await c.post(f"/projects/{p2}/run"))
        check("返回后台任务", t["status"] in ("queued", "running") and t["id"].startswith("task_"), json.dumps(t)[:160])
        busy = await c.post(f"/projects/{p2}/run")
        check("同项目不能并发两个任务", busy.status_code == 409, str(busy.status_code))
        for _ in range(300):
            t = await j(await c.get(f"/tasks/{t['id']}"))
            if t["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.05)
        check("一键任务成功", t["status"] == "succeeded", json.dumps(t)[:400])
        names = [s["name"] for s in t["steps"]]
        want = ["资产抽取", "资产参考图", "分镜", "分镜图", "提示词", "提交视频", "等待出片"]
        check("步骤完整", names[:7] == want, str(names))
        check("每步都成功", all(s["status"] == "succeeded" for s in t["steps"]), json.dumps(t["steps"])[:300])
        st2 = await j(await c.get(f"/projects/{p2}"))
        if HAS_FFMPEG:
            check("一键出成片", all(e["final_url"] for e in st2["episodes"]) and st2["status"] == "final_ready",
                  st2["status"])
        tl = await j(await c.get(f"/projects/{p2}/tasks"))
        check("任务列表", tl["items"] and tl["items"][0]["id"] == t["id"], "")
        t3 = await j(await c.post(f"/projects/{p2}/run?wait=true"))
        check("再跑一次：各环节幂等跳过、不重复下单", t3["status"] == "succeeded"
              and t3["result"]["videos"]["submitted"] == 0, json.dumps(t3.get("result", {}))[:300])

        print("\n[O] 边界")
        miss = await c.get("/projects/proj_nope")
        check("不存在项目 404", miss.status_code == 404, str(miss.status_code))
        empty = await c.post(f"/projects/{p2}/script", json={"text": "   "})
        check("空剧本 400", empty.status_code == 400, str(empty.status_code))
        nob = (await j(await c.post("/projects", json={"name": "空项目"})))["id"]
        bad = await c.post(f"/projects/{nob}/storyboard")
        check("无剧本分镜 400", bad.status_code == 400, str(bad.status_code))
        bad = await c.post(f"/projects/{nob}/run")
        check("无剧本一键 400", bad.status_code == 400, str(bad.status_code))
        noname = await c.post("/projects", json={"name": " "})
        check("空项目名 400", noname.status_code == 400, str(noname.status_code))
        d = await j(await c.delete(f"/projects/{nob}"))
        check("删除项目", d.get("deleted") == nob, json.dumps(d))

        print("\n[P] 聚合层账本")
        from app.main import app as agg_app  # noqa: E402
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agg_app),
                                     base_url="http://agg", timeout=30) as ac:
            w = (await ac.get("/v1/wallet", headers={"X-User-Id": "studio"})).json()
        check("有消费记录", w["total_spent_rmb"] > 0, json.dumps(w)[:200])
        check("无未结算预扣", w["open_holds"]["count"] == 0, json.dumps(w["open_holds"]))

    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}" + ("" if HAS_FFMPEG else "  （无 ffmpeg，合成相关已跳过）"))
    for name, detail in FAIL:
        print(f"  FAILED: {name} :: {detail[:300]}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except Exception:
        traceback.print_exc()
        code = 2
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
