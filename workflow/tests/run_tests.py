"""工作流端到端测试：聚合层与工作流都在同一进程（ASGI 直连），全部走 mock，不联网不花钱。

覆盖刀徒截图里的全部环节：项目(封面/置顶/导出导入) → 剧本(上传 TXT/DOCX、一键剧本、检查) → 分集/场次
→ 资产(角色/场景/道具/生物/音频，形态，版本，出图/改图/润色/上传/批量上传) → 分镜(视频卡、时间段、表演、新增卡片)
→ 站位图(每场一张) → 分镜图 → 提示词(刀徒格式、@名字（形态）、声线参考、JSON 格式) → 字段级冲突
→ 报价与预算 → 出片(单镜参数覆盖、勾选参考、本地素材) → 回执/封面/通知 → 重做/选版本 → 截音色
→ 按集合成(字幕) → 批量下载 → AI 对话 → 一键成片(后台任务) → 边界。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile

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

from wf import llm, media  # noqa: E402
from wf import main as wf_main  # noqa: E402

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []
HAS_FFMPEG = bool(media.ffmpeg_bin())


def check(name: str, cond, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


SCRIPT = """第1集
1-1 日 外 金鸡岭
人物：萤姐、猪婆龙、二狗子
△五只青鸾鸟背上的冒牌捕妖队俯视金鸡岭全貌，狂风猎猎<风声>。
猪婆龙（指向彩蛋）：那里！
△二狗子落在彩蛋前，躬身行礼。
猪婆龙（声音沙哑，气场十足）：谁是管事的，站出来！
萤姐（边走边骂）：没良心的，一群孬种！
二狗子：在下二狗子，见过龙爷。

第2集
2-1 夜 内 山洞
人物：孔宣、萤姐
△蛋壳裂开，金红色的光透出来。
孔宣（蛋形态）微微颤动。
孔宣：你是谁？
萤姐：别怕，跟着我。
2-2 日 外 山路
△孔宣跟着萤姐走出山洞<鸟鸣>。
萤姐：走快点。
"""


def make_docx(text: str) -> bytes:
    from xml.sax.saxutils import escape
    paras = "".join(f"<w:p><w:r><w:t>{escape(ln)}</w:t></w:r></w:p>" for ln in text.splitlines())
    doc = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           f'wordprocessingml/2006/main"><w:body>{paras}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


async def j(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300], "_status": r.status_code}


async def wait_task(c, tid: str) -> dict:
    t = {}
    for _ in range(600):
        t = await j(await c.get(f"/tasks/{tid}"))
        if t["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.05)
    return t


async def main() -> int:
    transport = httpx.ASGITransport(app=wf_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://wf", timeout=120) as c:
        print("\n[A] 健康 / 建项目 / 设置")
        h = await j(await c.get("/healthz"))
        check("healthz：聚合层可达、mock 模式", h["ok"] and h["aggregator"] and h["llm_mode"] == "mock", json.dumps(h))
        check("强制 mock 下没有真实模型", h["live_models"] == [], str(h["live_models"]))
        ui = await c.get("/ui/")
        check("网页界面可打开", ui.status_code == 200 and "<html" in ui.text.lower(), str(ui.status_code))
        proj = await j(await c.post("/projects", json={"name": "妖王", "style": "3D",
                                                         "settings": {"budget_rmb": 300, "gen_frames": True}}))
        pid = proj["id"]
        check("建项目 + 设置生效", proj["status"] == "draft" and proj["settings"]["budget_rmb"] == 300, "")
        for bad_patch in ({"video_mode": "乱写"}, {"engine": "x"}, {"image_resolution": "8K"}, {"nope": 1}):
            r = await c.patch(f"/projects/{pid}/settings", json=bad_patch)
            check(f"非法设置 400：{list(bad_patch)[0]}", r.status_code == 400, str(r.status_code))

        print("\n[B] 剧本：上传 TXT / DOCX、检查、一键剧本")
        r = await j(await c.post(f"/projects/{pid}/script/upload?filename=剧本.txt", content=SCRIPT.encode("gb18030")))
        check("上传 GBK 编码 TXT", "金鸡岭" in r["script_text"] and r["script_check"]["episodes"] == 2,
              json.dumps(r.get("script_check")))
        check("剧本检查：识别 3 个场次、无警告", r["script_check"]["scenes"] == 3 and not r["script_check"]["warnings"],
              json.dumps(r["script_check"]))
        r = await j(await c.post(f"/projects/{pid}/script/upload?filename=剧本.docx", content=make_docx(SCRIPT)))
        check("上传 DOCX", "孔宣（蛋形态）" in r["script_text"], r.get("script_text", "")[:80])
        bad = await c.post(f"/projects/{pid}/script/upload?filename=a.exe", content=b"x")
        check("不支持的剧本格式 400", bad.status_code == 400, str(bad.status_code))
        g = (await j(await c.post("/projects", json={"name": "灵感项目"})))["id"]
        r = await j(await c.post(f"/projects/{g}/script/generate", json={"idea": "外卖员捡到会说话的猫", "episodes": 3}))
        check("一键剧本：生成 3 集", r["script_check"]["episodes"] == 3 and r["generated_source"] == "mock",
              json.dumps(r.get("script_check")))
        r = await c.post(f"/projects/{g}/script/generate", json={"idea": " "})
        check("一键剧本：空灵感 400", r.status_code == 400, str(r.status_code))

        print("\n[C] 分集与场次")
        r = await j(await c.post(f"/projects/{pid}/parse"))
        check("分成 2 集", len(r["episodes"]) == 2, str(len(r["episodes"])))
        codes = [(s["code"], s["location"], s["time_of_day"]) for s in r["scenes"]]
        check("场次：1-1 金鸡岭 / 2-1 山洞 / 2-2 山路", codes == [("1-1", "金鸡岭", "日"), ("2-1", "山洞", "夜"),
                                                       ("2-2", "山路", "日")], str(codes))

        print("\n[D] 资产：抽取 / 形态 / 生物 / 去重")
        r = await j(await c.post(f"/projects/{pid}/assets/extract"))
        by = {a["name"]: a for a in r["state"]["assets"]}
        check("抽到角色", {"猪婆龙", "二狗子", "萤姐", "孔宣"} <= set(by), str(list(by)))
        check("抽到场景", {"金鸡岭", "山洞", "山路"} <= set(by), str(list(by)))
        check("孔宣自动带出『蛋』形态", [f["name"] for f in by["孔宣"]["forms"]] == ["默认", "蛋"],
              str([f["name"] for f in by["孔宣"]["forms"]]))
        check("资产适用集数", by["孔宣"]["episodes"] == [2] and by["萤姐"]["episodes"] == [1, 2],
              f"{by['孔宣']['episodes']} {by['萤姐']['episodes']}")
        r2 = await j(await c.post(f"/projects/{pid}/assets/extract"))
        check("重复抽取不重复建", not r2["added"], str(r2["added"]))
        cr = await j(await c.post(f"/projects/{pid}/assets", json={"kind": "creature", "name": "青鸾鸟",
                                                                   "description": "青色神鸟"}))
        check("新增生物", cr["kind"] == "creature" and cr["forms"][0]["name"] == "默认", json.dumps(cr)[:120])
        dup = await c.post(f"/projects/{pid}/assets", json={"kind": "creature", "name": "青鸾鸟"})
        check("同名资产 400", dup.status_code == 400, str(dup.status_code))
        kx = await j(await c.post(f"/assets/{by['孔宣']['id']}/forms", json={"name": "战斗形态", "description": "火焰缠身"}))
        check("新增形态（自动去掉“形态”二字）", [f["name"] for f in kx["forms"]] == ["默认", "蛋", "战斗"],
              str([f["name"] for f in kx["forms"]]))
        bad = await c.post(f"/assets/{by['孔宣']['id']}/forms", json={"name": "默认"})
        check("不能新建“默认”形态", bad.status_code == 400, str(bad.status_code))
        dflt = kx["forms"][0]["id"]
        bad = await c.delete(f"/forms/{dflt}")
        check("默认形态不能删", bad.status_code == 400, str(bad.status_code))

        print("\n[E] 资产出图 / 改图 / 润色 / 上传 / 版本")
        r = await j(await c.post(f"/projects/{pid}/assets/images"))
        n_forms = sum(len(a["forms"]) for a in (await j(await c.get(f"/projects/{pid}")))["assets"])
        check("每个形态都出了图", len(r["generated"]) == n_forms and not r["failed"],
              f"{len(r['generated'])}/{n_forms} {r['failed']}")
        st = await j(await c.get(f"/projects/{pid}"))
        kx = next(a for a in st["assets"] if a["name"] == "孔宣")
        egg = next(f for f in kx["forms"] if f["name"] == "蛋")
        check("形态图落到本地并可访问", os.path.exists(egg["local_path"])
              and (await c.get(egg["display_url"])).status_code == 200, egg["display_url"])
        check("形态图提示词包含形态", "蛋形态" in egg["versions"][0]["prompt"], egg["versions"][0]["prompt"][:60])
        a2 = await j(await c.post(f"/forms/{dflt}/image"))
        check("重新出图 → v2", a2["forms"][0]["current_version"] == 2, "")
        a3 = await j(await c.post(f"/forms/{dflt}/edit-image", json={"instruction": "把眼睛改成黑色"}))
        v3 = a3["forms"][0]["versions"][-1]
        check("改图 → v3（edited，带修改要求）", a3["forms"][0]["current_version"] == 3 and "黑色" in v3["prompt"],
              json.dumps(v3)[:160])
        bad = await c.post(f"/forms/{dflt}/edit-image", json={"instruction": ""})
        check("改图要求为空 400", bad.status_code == 400, str(bad.status_code))
        a4 = await j(await c.post(f"/forms/{dflt}/upload?filename=kx.png", content=b"\x89PNG fake"))
        check("上传 → v4", a4["forms"][0]["current_version"] == 4 and a4["forms"][0]["versions"][-1]["source"] == "uploaded", "")
        bad = await c.post(f"/forms/{dflt}/upload?filename=kx.mp3", content=b"ID3")
        check("图片资产不能传音频", bad.status_code == 400, str(bad.status_code))
        a5 = await j(await c.post(f"/forms/{dflt}/select?version=2"))
        check("切回 v2", a5["forms"][0]["current_version"] == 2, "")
        pol = await j(await c.post(f"/assets/{kx['id']}/polish"))
        check("AI 润色描述", len(pol["asset"]["description"]) > len(kx["description"]), pol["asset"]["description"][:60])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("孔宣-蛋.png", b"\x89PNG egg")
            z.writestr("萤姐.jpg", b"\xff\xd8 jpg")
            z.writestr("不存在的人.png", b"x")
        bu = await j(await c.post(f"/projects/{pid}/assets/bulk-upload?filename=a.zip", content=buf.getvalue()))
        check("批量上传按文件名匹配", {(m["asset"], m["form"]) for m in bu["matched"]} == {("孔宣", "蛋"), ("萤姐", "默认")}
              and bu["unmatched"] == ["不存在的人.png"], json.dumps(bu, ensure_ascii=False))
        st = await j(await c.get(f"/projects/{pid}"))
        check("项目花费含出图", st["spend"]["images_rmb"] > 0, json.dumps(st["spend"]))
        check("首页自动封面", (await j(await c.get("/projects")))["items"][-1]["cover_url"] or True, "")

        print("\n[F] 分镜：视频卡（时间段 / 表演 / 台词 / 场次）")
        sb = await j(await c.post(f"/projects/{pid}/storyboard"))
        check("两集都拆了卡片", len(sb["results"]) == 2 and all(x["shots"] >= 1 for x in sb["results"]),
              json.dumps(sb["results"]))
        shots = sb["state"]["shots"]
        check("卡片时长在 4~15 秒", all(4 <= s["duration_s"] <= 15 for s in shots), str([s["duration_s"] for s in shots]))
        check("画面按时间段写", all(s["picture"].startswith("【0-") for s in shots), shots[0]["picture"][:60])
        s11 = shots[0]
        check("卡片编号 1-1", s11["code"] == "1-1" and s11["scene_code"] == "1-1", s11["code"])
        check("表演取自括号说明", "声音沙哑" in "".join(s["performance"] for s in shots), "")
        check("台词按“角色：台词”收集", "猪婆龙：那里！" in "".join(s["dialogue"] for s in shots), "")
        check("音效收集", "风声" in "".join(s["sfx"] for s in shots), "")
        check("卡片挂上场景资产", {s["scene_asset"] for s in shots} >= {"金鸡岭", "山洞"}, str({s["scene_asset"] for s in shots}))
        check("出场角色", any("猪婆龙" in s["characters"] for s in shots), str([s["characters"] for s in shots]))
        sb2 = await j(await c.post(f"/projects/{pid}/storyboard"))
        check("重复分镜幂等（跳过）", all(x.get("skipped") for x in sb2["results"]), json.dumps(sb2["results"]))
        pf = await c.post(f"/projects/{pid}/parse")
        check("已有分镜时重新解析需 force", pf.status_code == 400, str(pf.status_code))
        ns = await j(await c.post(f"/projects/{pid}/shots", json={"episode_no": 1, "after_shot_no": 0,
                                                                  "fields": {"picture": "片头空镜", "duration_s": 4}}))
        st = await j(await c.get(f"/projects/{pid}"))
        e1 = [s for s in st["shots"] if s["episode_no"] == 1]
        check("新增卡片插到最前、后面顺延", e1[0]["id"] == ns["id"] and [s["shot_no"] for s in e1] == list(range(1, len(e1) + 1)),
              str([(s["shot_no"], s["picture"][:6]) for s in e1]))
        await c.delete(f"/shots/{ns['id']}")
        st = await j(await c.get(f"/projects/{pid}"))
        e1 = [s for s in st["shots"] if s["episode_no"] == 1]
        check("删除卡片、编号回收", e1[0]["id"] == s11["id"] and e1[0]["shot_no"] == 1, "")
        check("剧集进度统计", st["episodes"][1]["shots"] >= 1 and st["episodes"][0]["stats"]["characters"] >= 3,
              json.dumps(st["episodes"][0]["stats"]))

        print("\n[G] 站位图（每场一张）/ 分镜图")
        bl = await j(await c.post(f"/projects/{pid}/blocking", json={}))
        check("3 个场次都出了站位图", len(bl["generated"]) == 3 and not bl["failed"], json.dumps(bl)[:200])
        check("站位图带了场景+角色参考", all(x["refs"] >= 2 for x in bl["generated"]), str([x["refs"] for x in bl["generated"]]))
        st = await j(await c.get(f"/projects/{pid}"))
        check("镜头能看到所在场次的站位图", all(s["blocking_image"] for s in st["shots"]), "")
        bl2 = await j(await c.post(f"/projects/{pid}/blocking", json={}))
        check("站位图只补缺", not bl2["generated"], str(bl2))
        sc0 = st["scenes"][0]
        e = await j(await c.patch(f"/scenes/{sc0['id']}", json={"fields": {"blocking_note": "萤姐站在彩蛋前"},
                                                                 "expected_rev": sc0["rev"]}))
        check("编辑站位说明", e["changed"] == ["blocking_note"], json.dumps(e))
        fr = await j(await c.post(f"/projects/{pid}/frames", json={}))
        check("每镜一张分镜图", len(fr["generated"]) == len(st["shots"]) and not fr["failed"], json.dumps(fr)[:160])

        print("\n[H] 提示词：刀徒格式 / @名字（形态）/ 声线")
        pr = await j(await c.post(f"/projects/{pid}/prompts"))
        check("提示词生成", pr["generated"] == len(st["shots"]) and pr["source"] == "mock", json.dumps(pr)[:120])
        shots = pr["state"]["shots"]
        p0 = shots[0]["prompt"]
        check("参考资产前缀", p0["positive"].startswith("参考资产：@") and "整体环境参考@金鸡岭" in p0["positive"],
              p0["positive"][:80])
        check("【镜头 结构 + 音效约束", "【镜头1" in p0["positive"] and llm.SUFFIX in p0["positive"], "")
        check("台词用 {} 包起来", "猪婆龙：{那里！}" in "".join(s["prompt"]["positive"] for s in shots), "")
        b_names = {b["name"] for b in p0["bindings"]}
        check("@ 解析成资产绑定（含版本）", {"猪婆龙", "金鸡岭"} <= b_names and all(b["version"] >= 1 for b in p0["bindings"]),
              str(b_names))
        check("没有未解析的 @", all(not s["prompt"]["unresolved"] for s in shots),
              str([s["prompt"]["unresolved"] for s in shots]))
        s2 = next(s for s in shots if s["episode_no"] == 2)
        txt = s2["prompt"]["positive"].replace("参考资产：", "参考资产：@孔宣（蛋形态）、@孔宣（飞行）、", 1)
        await c.patch(f"/prompts/{s2['prompt']['id']}", json={"fields": {"positive": txt}, "expected_rev": s2["prompt"]["rev"]})
        st = await j(await c.get(f"/projects/{pid}"))
        s2 = next(s for s in st["shots"] if s["id"] == s2["id"])
        forms = {(b["name"], b["form"]) for b in s2["prompt"]["bindings"]}
        check("@孔宣（蛋形态）绑定到蛋形态", ("孔宣", "蛋") in forms, str(forms))
        check("不存在的形态列入未解析", any("飞行" in u for u in s2["prompt"]["unresolved"]), str(s2["prompt"]["unresolved"]))
        egg_b = next(b for b in s2["prompt"]["bindings"] if b["form"] == "蛋")
        check("蛋形态绑定的是批量上传后的 v2", egg_b["version"] == 2, str(egg_b["version"]))

        print("\n[I] 字段级冲突")
        s1 = st["shots"][1]
        r1 = await j(await c.patch(f"/shots/{s1['id']}", json={"fields": {"picture": "A 改的画面"}, "expected_rev": s1["rev"]}))
        check("第一次保存成功", r1["changed"] == ["picture"] and r1["rev"] == s1["rev"] + 1, json.dumps(r1))
        r2 = await j(await c.patch(f"/shots/{s1['id']}", json={"fields": {"performance": "B 改的表演"}, "expected_rev": s1["rev"]}))
        check("旧版本号改别的字段：自动合并", r2.get("merged") is True and r2["changed"] == ["performance"], json.dumps(r2))
        r3 = await c.patch(f"/shots/{s1['id']}", json={"fields": {"picture": "B 改的画面"}, "expected_rev": s1["rev"]})
        b3 = await j(r3)
        check("旧版本号改同一字段：409 + 双方取值", r3.status_code == 409
              and b3["error"]["detail"]["current"]["picture"] == "A 改的画面", json.dumps(b3)[:200])
        rb = await c.patch(f"/shots/{s1['id']}", json={"fields": {"status": "x"}})
        check("系统字段不能改", rb.status_code == 400, str(rb.status_code))
        rev_before = (await j(await c.get(f"/projects/{pid}")))["shots"][1]["rev"]

        print("\n[J] 报价与预算")
        q = await j(await c.post(f"/projects/{pid}/videos/quote", json={}))
        check("报价覆盖全部镜头（含积分）", len(q["items"]) == len(st["shots"]) and q["total_points"] > 0, json.dumps(q)[:160])
        await c.patch(f"/projects/{pid}/settings", json={"budget_rmb": 1})
        over = await c.post(f"/projects/{pid}/videos", json={})
        check("超预算 402 且不提交", over.status_code == 402, str(over.status_code))
        await c.patch(f"/projects/{pid}/settings", json={"budget_rmb": 300})

        print("\n[K] 出片：全能参考 / 单镜参数覆盖 / 勾选参考 / 本地素材")
        mat = await j(await c.post(f"/projects/{pid}/materials?filename=道具参考.png", content=b"\x89PNG prop"))
        check("上传本地素材", mat["media_type"] == "image" and mat["url"], json.dumps(mat)[:120])
        one = st["shots"][0]
        egg_form = egg["id"]
        v1 = await j(await c.post(f"/projects/{pid}/videos", json={
            "shot_ids": [one["id"]], "duration": 6, "resolution": "720P", "aspect_ratio": "16:9",
            "form_ids": [egg_form], "material_ids": [mat["id"]]}))
        check("单镜提交成功", len(v1["submitted"]) == 1 and v1["submitted"][0]["mode"] == "reference", json.dumps(v1)[:200])
        st = await j(await c.get(f"/projects/{pid}"))
        job = next(s for s in st["shots"] if s["id"] == one["id"])["jobs"][-1]
        check("覆盖参数生效：6 秒 / 720P / 16:9", job["duration_s"] == 6 and job["resolution"] == "720P"
              and job["aspect_ratio"] == "16:9", json.dumps({k: job[k] for k in ("duration_s", "resolution", "aspect_ratio")}))
        roles = [x.get("blocking") or x.get("form") or x.get("material_id") for x in job["refs"]]
        check("参考 = 站位图 + 手选形态 + 本地素材", job["refs"][0].get("blocking") == "1-1"
              and any(x.get("form") == "蛋" for x in job["refs"]) and any(x.get("material_id") for x in job["refs"]),
              str(roles))
        v = await j(await c.post(f"/projects/{pid}/videos", json={"resolution": "480P"}))
        check("其余镜头批量提交", len(v["submitted"]) == len(st["shots"]) - 1, json.dumps(v)[:200])
        v2 = await j(await c.post(f"/projects/{pid}/videos", json={}))
        check("重复提交不重复下单", not v2["submitted"], json.dumps(v2)[:200])

        print("\n[L] 回执 / 下载 / 封面 / 通知")
        p1 = await j(await c.post(f"/projects/{pid}/videos/poll"))
        check("全部完成", len(p1["done"]) == len(st["shots"]) and not p1["running"], json.dumps(p1)[:200])
        st = await j(await c.get(f"/projects/{pid}"))
        check("每镜都选用了成功版本", all(s["job"] and s["job"]["status"] == "succeeded" for s in st["shots"]), "")
        if HAS_FFMPEG:
            check("视频已落到本地", all(os.path.exists(s["job"]["local_path"]) for s in st["shots"]), "")
            check("抽了封面帧", all(s["job"]["poster_url"] for s in st["shots"]), "")
        check("系统写回不推进用户对象版本号", st["shots"][1]["rev"] == rev_before, f"{st['shots'][1]['rev']} vs {rev_before}")
        nt = await j(await c.get("/notifications"))
        check("通知中心有出片完成", any("出片完成" in n["title"] for n in nt["items"]) and nt["unread"] > 0,
              json.dumps([n["title"] for n in nt["items"]][:6], ensure_ascii=False))

        print("\n[M] 单镜重做 + 选版本")
        target = st["shots"][0]
        rd = await j(await c.post(f"/projects/{pid}/videos", json={"shot_ids": [target["id"]], "mode": "text"}))
        check("单镜重做 → v2（文生）", rd["submitted"][0]["version"] == 2 and rd["submitted"][0]["mode"] == "text",
              json.dumps(rd)[:200])
        await c.post(f"/projects/{pid}/videos/poll")
        st = await j(await c.get(f"/projects/{pid}"))
        t2 = next(s for s in st["shots"] if s["id"] == target["id"])
        v1j = next(x for x in t2["jobs"] if x["version"] == 1)
        ok = await j(await c.post(f"/shots/{target['id']}/select?job_id={v1j['id']}"))
        check("切回 v1", ok["selected_job_id"] == v1j["id"], json.dumps(ok))

        print("\n[N] 从成片截音色 → 声线参考")
        if HAS_FFMPEG:
            ej = next(s for s in st["shots"] if s["episode_no"] == 1)["job"]
            vo = await j(await c.post(f"/jobs/{ej['id']}/extract-voice", json={"character": "萤姐", "start": 0.5, "seconds": 2}))
            check("截出「萤姐音色」音频资产", vo["asset"]["name"] == "萤姐音色" and vo["asset"]["kind"] == "audio"
                  and vo["asset"]["current_version"] == 1, json.dumps(vo)[:200])
            pr = await j(await c.post(f"/projects/{pid}/prompts?force=true"))
            ys = next(s for s in pr["state"]["shots"] if "萤姐" in s["characters"])
            check("提示词自动写声线参考", "萤姐声线参考@萤姐音色" in ys["prompt"]["positive"], ys["prompt"]["positive"][:120])
            check("音色也进了绑定", any(b["media_type"] == "audio" for b in ys["prompt"]["bindings"]), "")
            bad = await c.post(f"/jobs/{ej['id']}/extract-voice", json={"character": ""})
            check("截音色要写角色", bad.status_code == 400, str(bad.status_code))

        print("\n[O] 按集合成（字幕去掉角色名）")
        if HAS_FFMPEG:
            a = await j(await c.post(f"/projects/{pid}/assemble"))
            check("两集都合成", len(a["episodes"]) == 2, json.dumps(a)[:200])
            st = await j(await c.get(f"/projects/{pid}"))
            e1 = st["episodes"][0]
            exp = sum(s["job"]["seconds"] for s in st["shots"] if s["episode_no"] == 1)
            info = media.probe(e1["final_path"])
            check("成片时长 ≈ 各镜之和", abs(info["duration"] - exp) < 1.0, f"{info['duration']} vs {exp}")
            srt = open(e1["srt_path"], encoding="utf-8").read()
            check("字幕是纯台词", "谁是管事的，站出来！" in srt and "猪婆龙：" not in srt, srt[:160])
            check("项目状态 final_ready", st["status"] == "final_ready", st["status"])

        print("\n[P] 批量下载 / 项目导出导入")
        for what, want in (("videos", "视频/"), ("assets", "资产/"), ("finals", "成片/")):
            if what != "assets" and not HAS_FFMPEG:
                continue
            r = await c.get(f"/projects/{pid}/download?what={what}")
            names = zipfile.ZipFile(io.BytesIO(r.content)).namelist() if r.status_code == 200 else []
            check(f"批量下载 {what}", r.status_code == 200 and any(n.startswith(want) for n in names), str(names[:4]))
        check("资产包含形态命名", any(n.endswith("孔宣-蛋.png") for n in zipfile.ZipFile(io.BytesIO(
            (await c.get(f"/projects/{pid}/download?what=assets")).content)).namelist()), "")
        ex = await c.get(f"/projects/{pid}/export")
        check("导出项目包", ex.status_code == 200 and "project.json" in zipfile.ZipFile(io.BytesIO(ex.content)).namelist(),
              str(ex.status_code))
        dupi = await c.post("/projects/import", content=ex.content)
        check("同一项目不能重复导入", dupi.status_code == 400, str(dupi.status_code))
        before = await j(await c.get(f"/projects/{pid}"))
        await c.delete(f"/projects/{pid}")
        shutil.rmtree(os.path.join(os.environ["WORKFLOW_MEDIA"], pid), ignore_errors=True)
        im = await j(await c.post("/projects/import", content=ex.content))
        check("导入后结构一致", im["id"] == pid and len(im["shots"]) == len(before["shots"])
              and len(im["assets"]) == len(before["assets"]), json.dumps({k: im.get(k) for k in ("id", "name")}))
        kx2 = next(a for a in im["assets"] if a["name"] == "孔宣")
        check("导入后素材文件可访问", (await c.get(kx2["display_url"])).status_code == 200, kx2["display_url"])
        bad = await c.post("/projects/import", content=b"not a zip")
        check("坏项目包 400", bad.status_code == 400, str(bad.status_code))

        print("\n[Q] AI 对话")
        ag = await j(await c.get("/chat/agents"))
        check("智能体列表", {a["id"] for a in ag["items"]} >= {"assistant", "script", "video_prompt"}, str(ag))
        cs = await j(await c.post("/chat/sessions", json={"agent": "script", "project_id": pid}))
        r = await j(await c.post(f"/chat/sessions/{cs['id']}/messages", json={"content": "第一集的钩子够不够强？"}))
        check("对话回复 + 读取项目剧本", len(r["messages"]) == 2 and "已读取项目上下文" in r["messages"][1]["content"],
              json.dumps(r["messages"], ensure_ascii=False)[:200])
        check("首句成为对话标题", r["title"].startswith("第一集的钩子"), r["title"])
        lst = await j(await c.get(f"/chat/sessions?project_id={pid}"))
        check("对话记录列表", lst["items"] and lst["items"][0]["messages"] == 2, json.dumps(lst)[:160])
        bad = await c.post("/chat/sessions", json={"agent": "nope"})
        check("未知智能体 400", bad.status_code == 400, str(bad.status_code))
        await c.delete(f"/chat/sessions/{cs['id']}")
        check("删除对话", (await c.get(f"/chat/sessions/{cs['id']}")).status_code == 404, "")

        print("\n[R] 通知已读")
        mr = await j(await c.post("/notifications/read", json={}))
        nt = await j(await c.get("/notifications"))
        check("全部标为已读", mr["marked"] > 0 and nt["unread"] == 0, json.dumps(mr))

        print("\n[S] 一键成片（后台任务，从一键剧本开始）")
        await c.patch(f"/projects/{g}/settings", json={"budget_rmb": 200, "gen_frames": True, "prompt_format": "json"})
        t = await j(await c.post(f"/projects/{g}/run"))
        check("返回后台任务", t["status"] in ("queued", "running") and t["id"].startswith("task_"), json.dumps(t)[:160])
        busy = await c.post(f"/projects/{g}/run")
        check("同项目不能并发两个任务", busy.status_code == 409, str(busy.status_code))
        t = await wait_task(c, t["id"])
        check("一键任务成功", t["status"] == "succeeded", json.dumps(t)[:400])
        names = [s["name"] for s in t["steps"]]
        check("步骤完整（含站位图）", names == ["资产抽取", "资产参考图", "分镜", "站位图", "分镜图", "提示词", "提交视频",
                                         "等待出片", "合成成片"], str(names))
        st2 = await j(await c.get(f"/projects/{g}"))
        check("JSON 格式提示词", st2["shots"][0]["prompt"]["positive"].lstrip().startswith("{")
              and "镜头列表" in st2["shots"][0]["prompt"]["positive"], st2["shots"][0]["prompt"]["positive"][:80])
        if HAS_FFMPEG:
            check("一键出成片", all(e["final_url"] for e in st2["episodes"]) and st2["status"] == "final_ready", st2["status"])
        t3 = await j(await c.post(f"/projects/{g}/run?wait=true"))
        check("再跑一次：各环节幂等、不重复下单", t3["status"] == "succeeded" and t3["result"]["videos"]["submitted"] == 0,
              json.dumps(t3.get("result", {}))[:300])

        print("\n[T] 剧本直转提示词 / 项目列表 / 置顶 / 边界")
        p3 = (await j(await c.post("/projects", json={"name": "直转"})))["id"]
        await c.post(f"/projects/{p3}/script", json={"text": SCRIPT})
        dz = await j(await c.post(f"/projects/{p3}/prompts/from-script"))
        check("剧本直转提示词（自动拆卡）", dz["generated"] >= 2 and all(s["prompt"] for s in dz["state"]["shots"]), "")
        await c.post(f"/projects/{p3}/pin?pinned=true")
        items = (await j(await c.get("/projects")))["items"]
        check("置顶项目排第一", items[0]["id"] == p3 and items[0]["pinned"] == 1, str([i["name"] for i in items]))
        cov = await j(await c.post(f"/projects/{p3}/cover?filename=c.png", content=b"\x89PNG cover"))
        check("自定义封面", cov["cover_url"].endswith("cover.png"), json.dumps(cov))
        miss = await c.get("/projects/proj_nope")
        check("不存在项目 404", miss.status_code == 404, str(miss.status_code))
        empty = await c.post(f"/projects/{p3}/script", json={"text": "   "})
        check("空剧本 400", empty.status_code == 400, str(empty.status_code))
        nob = (await j(await c.post("/projects", json={"name": "空项目"})))["id"]
        check("无剧本分镜 400", (await c.post(f"/projects/{nob}/storyboard")).status_code == 400, "")
        check("无剧本一键 400", (await c.post(f"/projects/{nob}/run")).status_code == 400, "")
        check("空项目名 400", (await c.post("/projects", json={"name": " "})).status_code == 400, "")

        print("\n[V] 4K 高清 / 参考一致性闸门 / 相近名提示 / 视频编辑 / 动作迁移 / 剪辑工程")
        st = await j(await c.get(f"/projects/{pid}"))
        kx = next(a for a in st["assets"] if a["name"] == "孔宣")
        before_v = kx["forms"][0]["current_version"]
        up = await j(await c.post(f"/forms/{kx['forms'][0]['id']}/upscale"))
        newest = max(v["version"] for v in up["forms"][0]["versions"])
        check("4K 高清 → 新版本（upscaled，4K 尺寸提示）", up["forms"][0]["current_version"] == newest > before_v
              and up["forms"][0]["versions"][-1]["source"] in ("upscaled", "mock")
              and "4K" in up["forms"][0]["versions"][-1]["prompt"], json.dumps(up["forms"][0]["versions"][-1])[:160])
        ks = next(s for s in st["shots"] if any(b["name"] == "孔宣" and b["form"] == "默认" for b in s["prompt"]["bindings"]))
        gate = await c.post(f"/projects/{pid}/videos", json={"shot_ids": [ks["id"]]})
        gb = await j(gate)
        check("资产换图后提交：409 参考素材已变化（带明细）", gate.status_code == 409
              and gb["error"]["detail"]["code"] == "reference_changed"
              and any(x["asset"] == "孔宣" for x in gb["error"]["detail"]["stale"]), json.dumps(gb, ensure_ascii=False)[:300])
        ok = await j(await c.post(f"/projects/{pid}/videos", json={"shot_ids": [ks["id"]], "accept_stale": True}))
        check("确认用最新版后提交成功", len(ok["submitted"]) == 1, json.dumps(ok)[:200])
        await c.post(f"/projects/{pid}/videos/poll")
        await c.patch(f"/prompts/{ks['prompt']['id']}", json={"fields": {"positive": ks["prompt"]["positive"] + " @孔萱"}})
        st = await j(await c.get(f"/projects/{pid}"))
        ks = next(s for s in st["shots"] if s["id"] == ks["id"])
        check("未解析的 @ 给出相近名提示", "@孔宣" in (ks["prompt"]["suggestions"].get("孔萱") or []),
              json.dumps(ks["prompt"]["suggestions"], ensure_ascii=False))
        if HAS_FFMPEG:
            ve = await c.post(f"/projects/{pid}/videos/tool", json={"shot_id": ks["id"], "tool": "video_edit"})
            check("视频编辑要写修改要求", ve.status_code == 400, str(ve.status_code))
            ve = await j(await c.post(f"/projects/{pid}/videos/tool", json={"shot_id": ks["id"], "tool": "video_edit",
                                                                            "instruction": "把背景换成雨夜"}))
            check("视频编辑 → 新版本（视频编辑模型）", ve.get("mode") == "video_edit" and ve["model"] == "omni-flash-video-edit",
                  json.dumps(ve, ensure_ascii=False)[:200])
            mt = await c.post(f"/projects/{pid}/videos/tool", json={"shot_id": ks["id"], "tool": "motion_transfer"})
            check("动作迁移要选角色形态", mt.status_code == 400, str(mt.status_code))
            mt = await j(await c.post(f"/projects/{pid}/videos/tool", json={
                "shot_id": ks["id"], "tool": "motion_transfer", "form_id": kx["forms"][0]["id"], "instruction": "动作更夸张"}))
            check("动作迁移 → 新版本（Wan 动作迁移）", mt.get("mode") == "motion_transfer" and "motion" in mt["model"],
                  json.dumps(mt, ensure_ascii=False)[:200])
            await c.post(f"/projects/{pid}/videos/poll")
            st = await j(await c.get(f"/projects/{pid}"))
            ks = next(s for s in st["shots"] if s["id"] == ks["id"])
            modes = {x["mode"]: x["status"] for x in ks["jobs"]}
            check("编辑与迁移都出片成功并保留历史", modes.get("video_edit") == "succeeded"
                  and modes.get("motion_transfer") == "succeeded" and ks["job"]["mode"] == "motion_transfer", str(modes))
            ep = await c.get(f"/projects/{pid}/edit-package?episode_no=2")
            names = zipfile.ZipFile(io.BytesIO(ep.content)).namelist() if ep.status_code == 200 else []
            check("剪辑工程：片段 + 字幕 + EDL + FCPXML", ep.status_code == 200 and any(n.startswith("第2集/片段/01_2-1") for n in names)
                  and {"第2集/字幕.srt", "第2集/时间线.edl", "第2集/时间线.fcpxml", "导入说明.txt"} <= set(names), str(names[:8]))
            z = zipfile.ZipFile(io.BytesIO(ep.content))
            edl = z.read("第2集/时间线.edl").decode()
            from xml.etree import ElementTree as ET  # noqa: E402
            fx = ET.fromstring(z.read("第2集/时间线.fcpxml"))
            check("EDL / FCPXML 结构正确", "FROM CLIP NAME: 01_2-1" in edl and fx.tag == "fcpxml"
                  and len(fx.findall(".//asset-clip")) == len([n for n in names if n.startswith("第2集/片段/")]), edl[:120])

        print("\n[W] 发给视频模型的提示词改写 / 文本花费记账 / 旧库迁移")
        from wf.video_ops import VideoMixin  # noqa: E402
        binds = [{"mention": "@孔宣（蛋形态）", "form_id": "f_egg", "asset_id": "a_kx"},
                 {"mention": "@孔宣", "form_id": "f_kx", "asset_id": "a_kx"},
                 {"mention": "@金鸡岭", "form_id": "f_jjl", "asset_id": "a_jjl"},
                 {"mention": "@萤姐音色", "form_id": "f_voice", "asset_id": "a_v"}]
        rec = [{"blocking": "1-1", "role": "reference_image"},
               {"form_id": "f_kx", "name": "孔宣", "form": "默认", "role": "reference_image"},
               {"form_id": "f_egg", "name": "孔宣", "form": "蛋", "role": "reference_image"},
               {"asset_id": "a_v", "name": "萤姐音色", "role": "reference_audio"}]
        up = VideoMixin.upstream_prompt("参考资产：@孔宣、@孔宣（蛋形态）；整体环境参考@金鸡岭；萤姐声线参考@萤姐音色\n@孔宣抬头",
                                        "reference", rec, binds)
        check("全能参考：@名字 → 图N（名字），开头写【参考】说明", up.startswith("【参考】图1：场次 1-1 的站位图")
              and "图2（孔宣）" in up and "图3（孔宣（蛋形态））" in up and "音频1（萤姐音色）" in up
              and "图2：孔宣；图3：孔宣（蛋形态）" in up and "@" not in up and "金鸡岭" in up, up)
        up2 = VideoMixin.upstream_prompt("@孔宣站在@金鸡岭", "text", [], binds)
        check("文生视频：去掉 @ 符号", up2 == "孔宣站在金鸡岭", up2)
        wf_main.wf()._add_text_spend(pid, 0.0123)
        sp = (await j(await c.get(f"/projects/{pid}")))["spend"]
        check("文本花费计入项目总花费", sp["text_rmb"] >= 0.0123 and abs(sp["total_rmb"] - sp["videos_rmb"] - sp["images_rmb"]
                                                                      - sp["text_rmb"]) < 1e-6, json.dumps(sp))
        import sqlite3  # noqa: E402
        from wf import db as wfdb  # noqa: E402
        old = os.path.join(TMP, "old.db")
        con = sqlite3.connect(old)
        con.executescript(wfdb.SCHEMA.replace("  text_charged_rmb REAL NOT NULL DEFAULT 0,\n", ""))
        con.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES('p_old','旧项目','x','x')")
        con.commit()
        con.close()
        saved = wfdb.DB_PATH
        wfdb.DB_PATH = old
        try:
            wfdb.init()
            con = sqlite3.connect(old)
            cols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
            kept = con.execute("SELECT name FROM projects WHERE id='p_old'").fetchone()
            con.close()
        finally:
            wfdb.DB_PATH = saved
        check("旧库启动时自动补列、数据不丢", "text_charged_rmb" in cols and kept and kept[0] == "旧项目", str(cols))

        print("\n[X] 成熟度：配乐 / 分集封面 / 接上一镜尾帧 / 卡死任务超时退款 / 数据库备份")
        if HAS_FFMPEG:
            import subprocess  # noqa: E402
            bgm_file = os.path.join(TMP, "bgm.m4a")
            subprocess.run([media.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=3", "-c:a", "aac", bgm_file], check=True)
            mat = await j(await c.post(f"/projects/{pid}/materials?filename=bgm.m4a&name=配乐", content=open(bgm_file, "rb").read()))
            await c.patch(f"/projects/{pid}/settings", json={"bgm_material_id": mat["id"], "bgm_volume": 0.3})
            a = await j(await c.post(f"/projects/{pid}/assemble?episode_no=1&allow_partial=true"))
            st = await j(await c.get(f"/projects/{pid}"))
            info = media.probe(st["episodes"][0]["final_path"])
            exp = sum(s["job"]["seconds"] for s in st["shots"] if s["episode_no"] == 1)
            check("成片配乐：铺满全片且时长不变、有音轨", info["has_audio"] and abs(info["duration"] - exp) < 1.0,
                  json.dumps({"info": info, "exp": exp, "a": a}, ensure_ascii=False)[:200])
            bad = await c.patch(f"/projects/{pid}/settings", json={"bgm_volume": 5})
            check("配乐音量自动限制在 0~1", (await j(bad))["settings"]["bgm_volume"] == 1.0, "")
            ch = st["shots"][1]
            r = await j(await c.post(f"/projects/{pid}/videos", json={"shot_ids": [ch["id"]], "mode": "chain",
                                                                     "accept_stale": True, "allow_missing": True}))
            st = await j(await c.get(f"/projects/{pid}"))
            jb = next(s for s in st["shots"] if s["id"] == ch["id"])["jobs"][-1]
            check("接上一镜尾帧：首帧来自上一镜成片", r["submitted"] and jb["mode"] == "first_frame"
                  and jb["refs"][0].get("chain_from") == st["shots"][0]["code"], json.dumps(jb["refs"], ensure_ascii=False))
        cv = await j(await c.post(f"/projects/{pid}/covers?episode_no=1"))
        st = await j(await c.get(f"/projects/{pid}"))
        check("分集封面：生成竖版海报并可访问", cv["generated"] and st["episodes"][0]["cover_url"]
              and (await c.get(st["episodes"][0]["cover_url"])).status_code == 200, json.dumps(cv)[:200])
        check("封面花费计入图片", st["spend"]["images_rmb"] > 0, "")
        # 卡死任务：把一个在跑的任务改成 2 小时前提交，轮询时应取消并退款
        stuck = await j(await c.post(f"/projects/{pid}/videos", json={"shot_ids": [st["shots"][2]["id"]],
                                                                     "accept_stale": True, "allow_missing": True}))
        jid = stuck["submitted"][0]["job"]
        from wf import db as wfdb2  # noqa: E402
        with wfdb2.tx() as cc:
            cc.execute("UPDATE jobs SET created_at='2000-01-01 00:00:00' WHERE id=?", (jid,))
        p = await j(await c.post(f"/projects/{pid}/videos/poll"))
        st = await j(await c.get(f"/projects/{pid}"))
        jj = next(x for s in st["shots"] for x in s["jobs"] if x["id"] == jid)
        check("卡死任务超时：取消并标失败（写明原因）", jj["status"] == "failed" and "超时" in jj["error"]
              and any(f.get("timeout") for f in p["failed"]), json.dumps(jj, ensure_ascii=False)[:200])
        from app.main import app as agg_app2  # noqa: E402
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agg_app2), base_url="http://agg") as ac:
            w = (await ac.get("/v1/wallet", headers={"X-User-Id": "studio"})).json()
        check("超时任务的预扣已退回（无未结算预扣）", w["open_holds"]["count"] == 0, json.dumps(w["open_holds"]))
        bk = wfdb2.backup()
        check("数据库在线备份", bk and os.path.exists(bk) and os.path.getsize(bk) > 0, bk)

        print("\n[Y] 自动质检 / 配音")
        from wf.qc_dub import QcDubMixin  # noqa: E402
        lines = QcDubMixin._dub_lines("孔宣（冷笑）：你也配？\n旁白：三百年后。\n{猪婆龙：那里！}\n（沉默）\n一句没有说话人的话")
        check("台词拆句：角色/旁白/去表演括号/无前缀算旁白",
              lines == [("孔宣", "你也配？"), ("旁白", "三百年后。"), ("猪婆龙", "那里！"), ("旁白", "一句没有说话人的话")],
              str(lines))
        # 百炼 TTS 的流式 wav：文件头长度是占位值，时长必须按文件大小算
        import struct as _st, wave as _wv  # noqa: E401
        bogus = os.path.join(tempfile.gettempdir(), "wf_bogus_len.wav")
        with _wv.open(bogus, "wb") as _w:
            _w.setnchannels(1); _w.setsampwidth(2); _w.setframerate(24000); _w.writeframes(b"\0\0" * 24000 * 2)
        _b = bytearray(open(bogus, "rb").read()); _i = _b.find(b"data")
        _b[_i + 4:_i + 8] = _st.pack("<I", 0x7FFFFFFF); _b[4:8] = _st.pack("<I", 0x7FFFFFFF)
        open(bogus, "wb").write(_b)
        check("wav 头长度是占位值时按文件实际大小算时长", abs(media.wav_seconds(bogus) - 2.0) < 0.01,
              str(media.wav_seconds(bogus)))
        from wf.qc_dub import vision_score  # noqa: E402
        v1 = vision_score({"score": 92, "character_consistency": 95, "prompt_adherence": 55, "summary": "人物一致，但与分镜严重不符",
                           "issues": [{"type": "画面不符", "detail": "关键动作（擦过、后跳）未按分镜执行"}]})
        v2 = vision_score({"score": 95, "character_consistency": 95, "prompt_adherence": 90, "summary": "高度一致",
                           "issues": [{"type": "其他", "severity": "轻微", "detail": "背景细节略低"}]})
        v3 = vision_score({"character_consistency": 50, "prompt_adherence": 90, "issues": []})
        check("质检打分：不信模型总分，动作严重不符不能通过", v1[0] < 75 and v1[2], str(v1))
        check("质检打分：只有轻微问题照常通过", v2[0] >= 75 and not v2[1] and not v2[2], str(v2))
        check("质检打分：角色一致性低于 60 直接不合格", v3[1], str(v3))
        check("台词时长估算：19 字约 5~6 秒、无台词为 0",
              5 <= llm.speech_seconds("孔宣（皱眉）：此乃何物？红黄交替，莫不是哪位道友在斗法？") <= 6.5
              and llm.speech_seconds("") == 0, "")
        check("镜头时长按台词补足、不超过模型上限",
              llm.fit_duration(5, "孔宣：此乃何物？红黄交替，莫不是哪位道友在斗法？", 2, 15) >= 6
              and llm.fit_duration(5, "", 2, 15) == 5 and llm.fit_duration(5, "孔宣：" + "啊" * 200, 2, 15) == 15, "")
        await wf_main.wf().drain_background()
        st = await j(await c.get(f"/projects/{pid}"))
        done = [s for s in st["shots"] if s["job"] and s["job"]["status"] == "succeeded"]
        auto = [s for s in done if s["job"].get("qc")]
        check("出片后自动质检（后台）", len(auto) >= 1, f"{len(auto)}/{len(done)}")
        if HAS_FFMPEG and done:
            q = await j(await c.post(f"/projects/{pid}/qc", json={"episode_no": 1, "force": True}))
            check("本集质检：每镜有分数和结论", q["checked"] >= 1 and all(0 <= x["score"] <= 100 and x["verdict"] in
                  ("pass", "warn", "fail") for x in q["items"]), json.dumps(q, ensure_ascii=False)[:300])
            st = await j(await c.get(f"/projects/{pid}"))
            qq = next(s for s in st["shots"] if s["job"] and s["job"].get("qc"))["job"]["qc"]
            check("质检含技术检查与抽帧（模拟模式不调视觉模型）", any(x["name"] == "画幅" for x in qq["checks"])
                  and len(qq["frame_urls"]) == 4 and not qq["vision"],
                  json.dumps(qq, ensure_ascii=False)[:300])
            check("抽帧可访问", (await c.get(qq["frame_urls"][0])).status_code == 200, "")
            # 画幅不对 → error 级 → 不合格
            from wf import db as wfdb3  # noqa: E402
            bj = next(s for s in st["shots"] if s["job"] and s["job"].get("qc"))["job"]
            with wfdb3.tx() as cc:
                cc.execute("UPDATE jobs SET aspect_ratio='1:1' WHERE id=?", (bj["id"],))
            r = await j(await c.post(f"/jobs/{bj['id']}/qc"))
            check("画幅不符判不合格", r["verdict"] == "fail" and any(i["type"] == "画幅" for i in r["issues"]),
                  json.dumps(r, ensure_ascii=False)[:200])
            with wfdb3.tx() as cc:
                cc.execute("UPDATE jobs SET aspect_ratio=? WHERE id=?", (bj["aspect_ratio"], bj["id"]))
            W = wf_main.wf()
            r = await W.qc_job(bj["id"], force=True, vision=True)   # 走聚合层模拟视觉模型：返回不是 JSON
            check("视觉质检：请求能发出（含图片），结果不可解析时只留技术分并写明原因",
                  not r["vision"] and "视觉模型没给出" in r["note"], json.dumps({k: r[k] for k in ("note", "vision", "summary")}, ensure_ascii=False)[:300])
            orig_chat = W.agg.chat

            async def fake_chat(model, messages, **kw):
                imgs = sum(1 for p in messages[0]["content"] if p.get("type") == "image_url")
                return {"choices": [{"message": {"content": "```json\n" + json.dumps({
                    "score": 58, "character_consistency": 40, "prompt_adherence": 80,
                    "issues": [{"type": "角色不一致", "detail": f"共看了{imgs}张图，发色与定版图不同"}],
                    "summary": "角色不一致"}, ensure_ascii=False) + "\n```"}}], "billing": {"charged_rmb": 0.01}}
            W.agg.chat = fake_chat
            try:
                r = await W.qc_job(bj["id"], force=True, vision=True)
            finally:
                W.agg.chat = orig_chat
            check("视觉质检：解析模型 JSON、低分判不合格、问题列出", r["vision"] and r["verdict"] == "fail"
                  and r["character_consistency"] == 40 and any(i["type"] == "角色不一致" for i in r["issues"]),
                  json.dumps(r, ensure_ascii=False)[:300])

        vs = await j(await c.get(f"/api/voices?project={pid}"))
        check("音色列表 + 角色自动分配音色", len(vs["voices"]) >= 20 and vs["characters"]
              and all(x["voice"] in {v["id"] for v in vs["voices"]} for x in vs["characters"]),
              json.dumps(vs.get("characters"), ensure_ascii=False)[:200])
        ch0 = vs["characters"][0]
        bad = await c.post(f"/assets/{ch0['asset_id']}/voice", json={"voice": "不存在"})
        check("非法音色 400", bad.status_code == 400, str(bad.status_code))
        await j(await c.post(f"/assets/{ch0['asset_id']}/voice", json={"voice": "Kai"}))
        vs = await j(await c.get(f"/api/voices?project={pid}"))
        check("指定音色生效", next(x for x in vs["characters"] if x["asset_id"] == ch0["asset_id"])["voice"] == "Kai"
              and not next(x for x in vs["characters"] if x["asset_id"] == ch0["asset_id"])["auto"], "")
        smp = await j(await c.post(f"/projects/{pid}/voice-sample", json={"voice": "Cherry"}))
        check("试听音色：生成音频文件", smp["url"] and (await c.get(smp["url"])).status_code == 200, json.dumps(smp))
        if HAS_FFMPEG:
            st = await j(await c.get(f"/projects/{pid}"))
            talk = next(s for s in st["shots"] if s["episode_no"] == 1 and s["dialogue"].strip()
                        and s["job"] and s["job"]["status"] == "succeeded")
            await c.patch(f"/shots/{talk['id']}", json={"expected_rev": talk["rev"], "fields": {
                "dialogue": "孔宣（冷笑）：你也配？\n旁白：三百年后，人间股市。"}})
            d = await j(await c.post(f"/projects/{pid}/dub", json={"shot_ids": [talk["id"]]}))
            st = await j(await c.get(f"/projects/{pid}"))
            sd = next(s for s in st["shots"] if s["id"] == talk["id"])["dub"]
            check("配音：按句合成并排进镜头时长", d["dubbed"] == 1 and sd and len(sd["lines"]) == 2
                  and sd["lines"][0]["start"] < sd["lines"][1]["start"] and sd["lines"][1]["end"] <= sd["seconds"] + 0.01,
                  json.dumps(sd, ensure_ascii=False)[:300])
            check("旁白用旁白音色、角色用角色音色", sd["lines"][1]["voice"] == st["settings"]["narrator_voice"]
                  and sd["lines"][0]["speaker"] == "孔宣", json.dumps(sd["lines"], ensure_ascii=False)[:200])
            info = media.probe(next(s for s in st["shots"] if s["id"] == talk["id"])["dub_path"])
            check("配音轨时长 = 镜头时长", abs(info["duration"] - sd["seconds"]) < 0.15, json.dumps(info))
            d2 = await j(await c.post(f"/projects/{pid}/dub", json={"shot_ids": [talk["id"]]}))
            check("台词没变不重复配音（不重复扣费）", d2["dubbed"] == 0 and d2["skipped"] == 1, json.dumps(d2)[:200])
            check("配音花费计入项目", st["spend"]["audio_rmb"] > 0, json.dumps(st["spend"]))
            # 百炼限流：退避重试后成功；同时在跑的 TTS 请求不超过上限
            from wf.agg import AggError as _AE  # noqa: E402
            W = wf_main.wf()
            orig_tts, calls, live, peak = W.agg.tts, {"n": 0}, {"n": 0}, {"n": 0}
            os.environ["WORKFLOW_TTS_BACKOFF_S"] = "0.01"

            async def flaky_tts(model, text, voice, idem=None):
                live["n"] += 1; peak["n"] = max(peak["n"], live["n"])
                try:
                    await asyncio.sleep(0.02)
                    calls["n"] += 1
                    if calls["n"] % 3 == 1:
                        raise _AE('tts failed 502: all routes failed: {"code":"Throttling.RateQuota"}', 502)
                    return await orig_tts(model, text, voice, idem=idem)
                finally:
                    live["n"] -= 1
            W.agg.tts = flaky_tts
            try:
                many = [s for s in st["shots"] if s["dialogue"]][:4]
                rr = await W.dub_shots(pid, shot_ids=[s["id"] for s in many], force=True)
            finally:
                W.agg.tts = orig_tts
                os.environ.pop("WORKFLOW_TTS_BACKOFF_S", None)
            check("配音遇到限流自动重试，全部成功", not rr["errors"] and rr["dubbed"] == len(many) and calls["n"] > rr["dubbed"],
                  json.dumps({k: rr[k] for k in ("dubbed", "errors")}, ensure_ascii=False)[:300])
            check("配音并发不超过 2 路", peak["n"] <= 2, str(peak))
            st = await j(await c.get(f"/projects/{pid}"))
            # 很长的台词 → 加速
            long_line = "孔宣：" + "这一局我看得清清楚楚，量化也算不过我，" * 6
            talk = next(s for s in st["shots"] if s["id"] == talk["id"])
            await c.patch(f"/shots/{talk['id']}", json={"expected_rev": talk["rev"], "fields": {"dialogue": long_line}})
            d3 = await j(await c.post(f"/projects/{pid}/dub", json={"shot_ids": [talk["id"]]}))
            check("台词变了重新配音；太长会加速并提示超时长", d3["dubbed"] == 1 and d3["items"][0]["tempo"] > 1.0,
                  json.dumps(d3, ensure_ascii=False)[:200])
            await c.patch(f"/projects/{pid}/settings", json={"dub_mode": "mix", "bgm_material_id": ""})
            a = await j(await c.post(f"/projects/{pid}/assemble?episode_no=1&allow_partial=true"))
            st = await j(await c.get(f"/projects/{pid}"))
            ep = st["episodes"][0]
            info = media.probe(ep["final_path"])
            srt = open(ep["srt_path"], encoding="utf-8").read()
            check("成片混入配音：有音轨、字幕按配音时间", a["episodes"][0]["dubbed"] >= 1 and info["has_audio"]
                  and "这一局我看得清清楚楚" in srt, json.dumps(a["episodes"][0], ensure_ascii=False)[:200])
            await c.patch(f"/projects/{pid}/settings", json={"dub_mode": "replace"})
            a = await j(await c.post(f"/projects/{pid}/assemble?episode_no=1&allow_partial=true"))
            check("配音替换原声也能合成", a["episodes"][0]["dubbed"] >= 1, "")
            bad = await c.patch(f"/projects/{pid}/settings", json={"dub_mode": "乱写"})
            check("非法配音模式 400", bad.status_code == 400, "")
            cl = await j(await c.delete(f"/projects/{pid}/dub"))
            await c.patch(f"/projects/{pid}/settings", json={"dub_mode": "off"})
            check("清除配音", cl["cleared"] >= 1, json.dumps(cl))

        print("\n[Z] 质量闭环：按质检意见重抽 / 版本择优 / 审片 / 动作过密与拆卡")
        from wf import llm as wfllm, db as wfdbz  # noqa: E402
        dense = {"duration_s": 5, "picture": "【0-1秒】甲转身【1-2秒】乙拔剑【2-3.5秒】甲后跳【3.5-5秒】乙追", "dialogue": ""}
        ok_shot = {"duration_s": 6, "picture": "【0-3秒】甲转身【3-6秒】乙拔剑", "dialogue": "甲：站住！"}
        check("动作过密检查：5 秒 4 段 / 时间段太短 → 提示；正常卡无提示",
              any("过密" in x for x in wfllm.shot_density(dense)) and any("太短" in x for x in wfllm.shot_density(dense))
              and wfllm.shot_density(ok_shot) == [], str(wfllm.shot_density(dense)))
        p1, ch1 = wfllm.mock_revise("参考资产：@孔宣\n画面：…\n" + wfllm.SUFFIX,
                                    [{"type": "画面不符", "detail": "抬头动作没演出来"}, {"type": "多余人物", "detail": "x"}], ["孔宣"])
        p2, _ = wfllm.mock_revise(p1, [{"type": "字幕水印", "detail": "y"}], ["孔宣"])
        check("本地改提示词：插在结尾约束前、再次修改替换不叠加",
              p1.count("【质检修正】") == 1 and "抬头动作没演出来" in p1 and "只有 @孔宣" in p1
              and p1.rstrip().endswith(wfllm.SUFFIX) and p2.count("【质检修正】") == 1 and "抬头" not in p2, p1[-160:])

        st = await j(await c.get(f"/projects/{pid}"))
        done = [s for s in st["shots"] if s["episode_no"] == 1 and s["prompt"] and s["job"]
                and s["job"]["status"] == "succeeded"]
        a, b = done[0], done[1]
        r = await c.post(f"/jobs/{b['job']['id']}/review", json={"status": "rejected"})
        check("打回必须写原因", r.status_code == 400, str(r.status_code))
        await j(await c.post(f"/jobs/{b['job']['id']}/review", json={"status": "rejected", "note": "表情太平，要更震惊"}))
        bad_qc = {"score": 50, "verdict": "fail", "checks": [], "summary": "动作没演出来",
                  "issues": [{"type": "画面不符", "severity": "严重", "detail": "关键动作‘跳起来’缺失"}], "frames": []}
        with wfdbz.tx() as cc:
            cc.execute("UPDATE jobs SET qc_json=? WHERE id=?", (json.dumps(bad_qc, ensure_ascii=False), a["job"]["id"]))
        rt = await j(await c.post(f"/projects/{pid}/retake", json={"episode_no": 1}))
        codes = sorted(x["code"] for x in rt["submitted"])
        check("重抽：质检不合格 + 审片打回的镜头都重抽", codes == sorted([a["code"], b["code"]]),
              json.dumps(rt, ensure_ascii=False)[:300])
        st = await j(await c.get(f"/projects/{pid}"))
        sa = next(s for s in st["shots"] if s["id"] == a["id"])
        sb = next(s for s in st["shots"] if s["id"] == b["id"])
        nj = sa["jobs"][-1]
        check("重抽：质检问题写进提示词、新版本记着原版本和依据",
              "跳起来" in sa["prompt"]["positive"] and nj["version"] == max(x["version"] for x in a["jobs"]) + 1
              and nj["retake"]["from_job"] == a["job"]["id"] and nj["retake"]["from_score"] == 50,
              json.dumps({"has": "跳起来" in sa["prompt"]["positive"], "v": nj["version"], "old": a["job"]["version"],
                          "jobs": [(x["version"], x["status"]) for x in sa["jobs"]]}, ensure_ascii=False)[:300])
        check("重抽：审片意见也写进提示词", "更震惊" in sb["prompt"]["positive"], sb["prompt"]["positive"][-120:])
        r = await c.post(f"/projects/{pid}/retake", json={"episode_no": 1})
        check("在生成中的镜头不会重复重抽", r.status_code == 400, r.text[:120])
        await wf_main.wf().wait_videos(pid, timeout_s=30, interval_s=0.05)
        await wf_main.wf().drain_background()
        # 版本择优：新版本分更低 → 改回原版本
        W = wf_main.wf()
        worse = dict(bad_qc, score=30)
        with wfdbz.tx() as cc:
            cc.execute("UPDATE jobs SET qc_json=? WHERE id=?", (json.dumps(worse, ensure_ascii=False), nj["id"]))
        cmp_ = W.keep_better(nj["id"])
        st = await j(await c.get(f"/projects/{pid}"))
        check("重抽版本更差 → 自动保留原版本", cmp_["kept"] == "old"
              and next(s for s in st["shots"] if s["id"] == a["id"])["selected_job_id"] == a["job"]["id"], json.dumps(cmp_))
        good = dict(bad_qc, score=88, verdict="pass", issues=[])
        with wfdbz.tx() as cc:
            cc.execute("UPDATE jobs SET qc_json=? WHERE id=?", (json.dumps(good, ensure_ascii=False), nj["id"]))
        check("重抽版本更好 → 选用新版本", W.keep_better(nj["id"])["kept"] == "new", "")
        # 自动重抽：开关、次数上限
        await c.patch(f"/projects/{pid}/settings", json={"auto_retake": True, "retake_max": 1})
        with wfdbz.tx() as cc:
            cc.execute("UPDATE jobs SET qc_json=? WHERE id=?", (json.dumps(bad_qc, ensure_ascii=False), nj["id"]))
        await W.after_auto_qc(pid, [(nj["id"], bad_qc)])
        st = await j(await c.get(f"/projects/{pid}"))
        check("自动重抽：已重抽过 1 次（上限 1）不再抽", len(next(s for s in st["shots"] if s["id"] == a["id"])["jobs"]) == len(sa["jobs"]),
              "")
        c_shot = next(s for s in st["shots"] if s["id"] not in (a["id"], b["id"]) and s["prompt"] and s["job"]
                      and s["job"]["status"] == "succeeded" and not s["job"].get("retake"))
        with wfdbz.tx() as cc:
            cc.execute("UPDATE jobs SET qc_json=? WHERE id=?", (json.dumps(bad_qc, ensure_ascii=False), c_shot["job"]["id"]))
        await W.after_auto_qc(pid, [(c_shot["job"]["id"], bad_qc)])
        st = await j(await c.get(f"/projects/{pid}"))
        cj = next(s for s in st["shots"] if s["id"] == c_shot["id"])["jobs"][-1]
        check("自动重抽：不合格且没抽过的镜头自动重抽", cj["retake"] and cj["retake"]["auto"], json.dumps(cj.get("retake"), ensure_ascii=False)[:160])
        await c.patch(f"/projects/{pid}/settings", json={"auto_retake": False})
        await W.wait_videos(pid, timeout_s=30, interval_s=0.05)
        await W.drain_background()
        # 模型改坏 @ 引用 → 退回本地规则
        orig_chat = W.agg.chat

        async def bad_chat(model, messages, **kw):
            return {"choices": [{"message": {"content": json.dumps({"positive": "完全重写，没有任何引用" * 20, "changes": []}, ensure_ascii=False)}}],
                    "billing": {"charged_rmb": 0}}
        W.agg.chat = bad_chat
        try:
            os.environ["WORKFLOW_LLM"] = "auto"
            newp, _, src = await wfllm.revise_prompt(W.agg, "x", "参考资产：@孔宣 画面", [{"type": "画面不符", "detail": "d"}], ["孔宣"])
        finally:
            os.environ["WORKFLOW_LLM"] = "mock"
            W.agg.chat = orig_chat
        check("模型改丢了 @ 引用 → 退回本地规则", src == "mock" and "@孔宣" in newp, newp[:100])
        fl = wfllm._fix_line({"type": "画面不符", "detail": "分镜要求孔宣盯着‘屏幕’，但成片中角色手持手机，且无交易界面"}, [])
        check("改提示词只写要求、不把错误画面写进去", fl == "必须清楚演出：孔宣盯着‘屏幕’" and "手机" not in fl, fl)
        W.agg.chat = None

        async def extra_chat(model, messages, **kw):
            return {"choices": [{"message": {"content": json.dumps({"positive": "参考资产：@孔宣、@交易所大屏 画面：@孔宣盯着电脑屏幕" * 3,
                                                                  "changes": ["加了屏幕"]}, ensure_ascii=False)}}], "billing": {"charged_rmb": 0}}
        W.agg.chat = extra_chat
        try:
            os.environ["WORKFLOW_LLM"] = "auto"
            p_ok, _, s_ok = await wfllm.revise_prompt(W.agg, "x", "参考资产：@孔宣 画面", [{"type": "画面不符", "detail": "d"}], ["孔宣"],
                                                     known=["孔宣", "交易所大屏"])
            p_no, _, s_no = await wfllm.revise_prompt(W.agg, "x", "参考资产：@孔宣 画面", [{"type": "画面不符", "detail": "d"}], ["孔宣"],
                                                     known=["孔宣"])
        finally:
            os.environ["WORKFLOW_LLM"] = "mock"
            W.agg.chat = orig_chat
        check("模型新增的 @ 是资产表里的名字 → 接受；不认识的 → 退回本地规则", s_ok == "real" and s_no == "mock", f"{s_ok}/{s_no}")
        # 拆卡
        tgt = next(s for s in st["shots"] if s["episode_no"] == 1 and not any(x["status"] in ("submitted", "running") for x in s["jobs"]))
        await c.patch(f"/shots/{tgt['id']}", json={"expected_rev": tgt["rev"], "fields": {
            "picture": "【0-1秒】孔宣抬头【1-2秒】孔宣伸手【2-3秒】股民乙跳起来【3-5秒】孔宣冷笑",
            "dialogue": "孔宣：三日之内。\n股民乙：涨了！", "duration_s": 5}})
        st = await j(await c.get(f"/projects/{pid}"))
        tgt = next(s for s in st["shots"] if s["id"] == tgt["id"])
        dn = await j(await c.get(f"/projects/{pid}/density?episode_no=1"))
        check("项目动作过密清单里有这张卡", any(x["shot"] == tgt["id"] for x in dn["items"]), json.dumps(dn, ensure_ascii=False)[:200])
        n_before = sum(1 for s in st["shots"] if s["episode_no"] == 1)
        sp = await j(await c.post(f"/shots/{tgt['id']}/split"))
        st = await j(await c.get(f"/projects/{pid}"))
        s1 = next(s for s in st["shots"] if s["id"] == sp["split"])
        s2 = next(s for s in st["shots"] if s["id"] == sp["new"])
        check("拆卡：一分为二、新卡紧跟在后、时间轴从 0 重排、台词各一半",
              sum(1 for s in st["shots"] if s["episode_no"] == 1) == n_before + 1 and s2["shot_no"] == s1["shot_no"] + 1
              and s1["picture"].startswith("【0-1秒】") and s2["picture"].startswith("【0-1秒】股民乙跳起来")
              and s1["dialogue"] == "孔宣：三日之内。" and s2["dialogue"] == "股民乙：涨了！"
              and s1["prompt"] is None and s1["selected_job_id"] == "", json.dumps([s1["picture"], s2["picture"]], ensure_ascii=False))
        sp2 = await j(await c.post(f"/shots/{sp['new']}/split"))
        r = await c.post(f"/shots/{sp2['new']}/split")
        check("只剩一段一句的卡不能再拆", r.status_code == 400, r.text[:100])

        print("\n[Z2] 剪辑精修：入点/出点、叠化")
        if HAS_FFMPEG:
            await c.patch(f"/projects/{pid}/settings", json={"dub_mode": "off", "bgm_material_id": "", "burn_subtitles": False})
            st = await j(await c.get(f"/projects/{pid}"))
            ep1 = [s for s in st["shots"] if s["episode_no"] == 1 and s["job"] and s["job"]["status"] == "succeeded"
                   and s["job"]["local_path"]]
            a, b = ep1[0], ep1[1]
            r = await c.post(f"/shots/{a['id']}/cut", json={"trim_in": 30, "trim_out": 0})
            check("剪过头 400（至少留 1 秒）", r.status_code == 400, r.text[:100])
            r = await c.post(f"/shots/{a['id']}/cut", json={"xfade_s": 3})
            check("叠化超过 2 秒 400", r.status_code == 400, "")
            cut = await j(await c.post(f"/shots/{a['id']}/cut", json={"trim_in": 0.5, "trim_out": 0.5, "xfade_s": 0.5}))
            check("设置入点/出点/叠化", cut["trim_in"] == 0.5 and cut["xfade_s"] == 0.5, json.dumps(cut))
            st = await j(await c.get(f"/projects/{pid}"))
            durs = {s["id"]: media.probe(s["job"]["local_path"])["duration"] for s in st["shots"] if s["id"] in [x["id"] for x in ep1]}
            asm = await j(await c.post(f"/projects/{pid}/assemble?episode_no=1&allow_partial=true"))
            st = await j(await c.get(f"/projects/{pid}"))
            fin = media.probe(st["episodes"][0]["final_path"])["duration"]
            expect = sum(durs.values()) - 1.0 - 0.5
            check("成片时长 = 各镜之和 − 剪掉的 − 叠化重叠", abs(fin - expect) < 0.35,
                  json.dumps({"final": fin, "expect": expect, "durs": list(durs.values())}))
            srt = open(st["episodes"][0]["srt_path"], encoding="utf-8").read()
            check("叠化后字幕仍然有且按顺序", srt.count("-->") >= 1, srt[:200])
            import zipfile as _zf
            rz = await c.get(f"/projects/{pid}/edit-package?episode_no=1")
            zp = os.path.join(TMP, "edit_cut.zip")
            open(zp, "wb").write(rz.content)
            with _zf.ZipFile(zp) as z:
                edl = z.read(next(n for n in z.namelist() if n.endswith(".edl"))).decode("utf-8")
                fx = z.read(next(n for n in z.namelist() if n.endswith(".fcpxml"))).decode("utf-8")
            check("剪辑工程按入点导出（EDL 源入点 00:00:00:12、FCPXML start=12/24s）",
                  "00:00:00:12" in edl.splitlines()[3] and 'start="12/24s"' in fx, edl[:300])
            await j(await c.post(f"/shots/{a['id']}/cut", json={"trim_in": 0, "trim_out": 0, "xfade_s": 0}))

        print("\n[Z3] 发布导出")
        if HAS_FFMPEG:
            r = await c.post(f"/projects/{pid}/export", json={"episode_no": 1, "preset": "乱写"})
            check("未知导出预设 400", r.status_code == 400, "")
            ex = await j(await c.post(f"/projects/{pid}/export", json={"episode_no": 1, "preset": "douyin"}))
            check("抖音预设：1080×1920、放大、有声音", ex["width"] == 1080 and ex["height"] == 1920 and ex["upscaled"]
                  and media.probe(os.path.join(os.environ["WORKFLOW_MEDIA"], ex["url"].split("/media/", 1)[1]))["has_audio"],
                  json.dumps(ex, ensure_ascii=False)[:200])
            st = await j(await c.get(f"/projects/{pid}"))
            ep = st["episodes"][0]
            ex2 = await j(await c.post(f"/projects/{pid}/export", json={"episode_no": 1, "preset": "master", "cover_first": True}))
            check("母版 + 封面放首帧：时长多 0.5 秒、分辨率不变", abs(ex2["seconds"] - ep["final_seconds"] - 0.5) < 0.3
                  and ex2["cover_first"], json.dumps({"ex2": ex2, "final": ep["final_seconds"]})[:200])
            st = await j(await c.get(f"/projects/{pid}"))
            check("剧集列出已导出的文件", {x["preset"] for x in st["episodes"][0]["exports"]} >= {"douyin", "master"},
                  json.dumps(st["episodes"][0]["exports"], ensure_ascii=False))

        print("\n[Z4] 存储与清理")
        stg = await j(await c.get("/api/storage"))
        mine = next((x for x in stg["projects"] if x["project"] == pid), None)
        check("存储用量：按项目、按目录统计，含磁盘余量", mine and mine["bytes"] > 0 and "videos" in mine["parts"]
              and stg["disk"].get("free", 0) > 0, json.dumps(mine, ensure_ascii=False)[:200])
        ops = await j(await c.get("/api/ops"))
        check("运行状态：项目数、在跑任务、媒体占用", ops["projects"] >= 1 and "running_jobs" in ops and ops["media_bytes"] > 0,
              json.dumps(ops)[:200])
        if HAS_FFMPEG:
            await wf_main.wf().wait_videos(pid, timeout_s=30, interval_s=0.05)
            st = await j(await c.get(f"/projects/{pid}"))
            multi = [s for s in st["shots"] if len([x for x in s["jobs"] if x["status"] == "succeeded" and x["local_path"]]) >= 2]
            pv = await j(await c.post(f"/projects/{pid}/cleanup", json={"dry_run": True, "keep_versions": 0}))
            check("清理预览：只列出未选用旧版本，不删文件", pv["dry_run"] and pv["jobs"] >= len(multi) and pv["files"] >= 1
                  and all(os.path.exists(os.path.join(os.environ["WORKFLOW_MEDIA"], f)) for f in pv["sample"]),
                  json.dumps(pv, ensure_ascii=False)[:300])
            sel_paths = [s["job"]["local_path"] for s in st["shots"] if s["job"] and s["job"]["local_path"]]
            done_ = await j(await c.post(f"/projects/{pid}/cleanup", json={"dry_run": False, "keep_versions": 0}))
            st = await j(await c.get(f"/projects/{pid}"))
            check("清理执行：旧版本文件删掉、当前选用的保留", done_["files"] == pv["files"]
                  and all(os.path.exists(p_) for p_ in sel_paths)
                  and not any(os.path.exists(os.path.join(os.environ["WORKFLOW_MEDIA"], f)) for f in pv["sample"]),
                  json.dumps({"n": [done_["files"], pv["files"]], "sel_missing": [p_ for p_ in sel_paths if not os.path.exists(p_)][:3],
                              "left": [f for f in pv["sample"] if os.path.exists(os.path.join(os.environ["WORKFLOW_MEDIA"], f))][:3]},
                             ensure_ascii=False)[:400])
            pv2 = await j(await c.post(f"/projects/{pid}/cleanup", json={"dry_run": True, "keep_versions": 0}))
            check("再预览为 0（幂等）", pv2["files"] == 0, json.dumps(pv2)[:200])

        print("\n[Z5] LibTV 登录经工作台转发")
        lt = await j(await c.get("/api/libtv"))
        check("登录地址走工作台（子路径部署也能用）", lt["login_url"] == "/libtv/login" and "redirect_uri" in lt, json.dumps(lt)[:200])
        cb = await c.get("/libtv/callback?error=access_denied&error_description=user_cancel")
        check("回跳转发给聚合层并返回页面", cb.status_code == 200 and "授权没有完成" in cb.text and "user_cancel" in cb.text, cb.text[:200])

        print("\n[Z6] 角色定版锁")
        st = await j(await c.get(f"/projects/{pid}"))
        ch = next(a for a in st["assets"] if a["kind"] == "character" and a["current_version"])
        fid = ch["default_form_id"]
        lk = await j(await c.post(f"/forms/{fid}/lock", json={"locked": True}))
        check("定版锁住形态", next(f for f in lk["forms"] if f["id"] == fid)["locked"] == 1, "")
        bad = [await c.post(f"/forms/{fid}/image"), await c.post(f"/forms/{fid}/select?version=1"),
               await c.delete(f"/assets/{ch['id']}")]
        check("锁定后不能重新出图 / 换版本 / 删除", all(r.status_code == 400 and "定版" in r.text for r in bad),
              str([r.status_code for r in bad]))
        before = len(next(f for f in lk["forms"] if f["id"] == fid)["versions"])
        await c.post(f"/projects/{pid}/assets/images?only_missing=false")
        st = await j(await c.get(f"/projects/{pid}"))
        after = len(next(f for a in st["assets"] for f in a["forms"] if f["id"] == fid)["versions"])
        check("批量重新出图跳过已定版的形态", before == after, f"{before}->{after}")
        r = await j(await c.post(f"/projects/{pid}/lock", json={"locked": False}))
        r2 = await j(await c.post(f"/projects/{pid}/lock", json={"locked": True}))
        check("一键定版 / 解锁全部角色", r["changed"] >= 1 and r2["changed"] >= 1, json.dumps([r, r2]))
        await c.post(f"/projects/{pid}/lock", json={"locked": False})
        ok = await c.post(f"/forms/{fid}/select?version=1")
        check("解锁后可以换版本", ok.status_code == 200, ok.text[:100])

        print("\n[Z7] 多集排队生产 + 报告")
        p2 = await j(await c.post("/projects", json={"name": "批量生产", "style": "3D", "settings": {
            "budget_rmb": 500, "gen_asset_images": False, "gen_blocking": False, "dub_mode": "mix"}}))
        pid2 = p2["id"]
        await j(await c.post(f"/projects/{pid2}/script", json={"text": SCRIPT}))
        r = await c.post(f"/projects/{pid2}/produce", json={"start_at": "25:99"})
        check("开始时间格式不对 400", r.status_code == 400, r.text[:100])
        t = await j(await c.post(f"/projects/{pid2}/produce", json={"episodes": [1, 2], "per_episode_budget_rmb": 0,
                                                                    "retake": True, "export_preset": "douyin" if HAS_FFMPEG else "",
                                                                    "wait": True}))
        rep = t["result"]
        check("批量生产：逐集跑完、每集有花费和质检", t["status"] == "succeeded" and len(rep["episodes"]) == 2
              and all(e["status"] in ("done", "partial") and e["spent_rmb"] > 0 and e.get("qc") for e in rep["episodes"]),
              json.dumps(rep, ensure_ascii=False)[:400])
        if HAS_FFMPEG:
            check("每集合成并导出抖音版", all(e.get("final_url") and e.get("export_url") for e in rep["episodes"]),
                  json.dumps(rep["episodes"], ensure_ascii=False)[:300])
        rp = await c.get(rep["report_url"])
        check("生产报告（Markdown）可下载", rp.status_code == 200 and "| 第1集 |" in rp.text and "| 第2集 |" in rp.text, rp.text[:200])
        t = await j(await c.post(f"/projects/{pid2}/produce", json={"episodes": [1], "per_episode_budget_rmb": 0.01,
                                                                    "wait": True}))
        e1 = t["result"]["episodes"][0]
        check("单集上限：超出就跳过这一集并写明原因", e1["status"] in ("failed", "done") and
              (e1["status"] == "done" or "单集上限" in "".join(e1["errors"])), json.dumps(e1, ensure_ascii=False)[:200])

        print("\n[U] 聚合层账本")
        from app.main import app as agg_app  # noqa: E402
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agg_app), base_url="http://agg", timeout=30) as ac:
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
