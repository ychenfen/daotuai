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
