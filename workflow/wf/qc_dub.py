"""自动质检 + 配音。

质检（每个出片版本）：
  1. 技术检查（零成本，ffprobe）：时长是否接近分镜要求、画幅比例、分辨率、有台词时是否有音轨。
  2. 视觉检查（qwen3-vl，约 1 分钱/镜）：均匀抽 4 帧 + 绑定角色的定版图，核对人物一致、画面是否符合分镜、
     有无字幕/水印/崩坏。模拟模式或本地引擎下只做技术检查。
配音（每个镜头）：
  台词按行拆成「角色：台词」/旁白 → 每句用角色音色合成（qwen3-tts）→ 排进镜头时长（必要时加速）→ 一条配音轨。
  合成成片时 dub_mode：off 保留原声 | mix 配音为主、原声压低垫底 | replace 只用配音。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import parse_qs, urlparse

from . import db, llm, media
from .agg import AggError
from .errors import BudgetExceeded, NotFound, WorkflowError
from .util import short_hash

log = logging.getLogger("workflow.qcdub")

# 千问 TTS 常用音色（id 是接口里的 voice 参数）。gender: f/m；dialect 为方言音色。
VOICES = [
    {"id": "Cherry", "label": "芊悦 · 阳光女声", "gender": "f"},
    {"id": "Serena", "label": "苏瑶 · 温柔女声", "gender": "f"},
    {"id": "Maia", "label": "四月 · 知性女声", "gender": "f"},
    {"id": "Katerina", "label": "卡捷琳娜 · 御姐", "gender": "f"},
    {"id": "Bellona", "label": "燕铮莺 · 大气女声", "gender": "f"},
    {"id": "Elias", "label": "墨讲师 · 知性讲述", "gender": "f"},
    {"id": "Ethan", "label": "晨煦 · 阳光男声", "gender": "m"},
    {"id": "Ryan", "label": "甜茶 · 张力男声", "gender": "m"},
    {"id": "Kai", "label": "凯 · 磁性男声", "gender": "m"},
    {"id": "Moon", "label": "月白 · 率性男声", "gender": "m"},
    {"id": "Nofish", "label": "不吃鱼 · 青年男声", "gender": "m"},
    {"id": "Vincent", "label": "田叔 · 沙哑大叔", "gender": "m"},
    {"id": "Eldric Sage", "label": "沧明子 · 沉稳老者", "gender": "m"},
    {"id": "Arthur", "label": "徐大爷 · 老年男声", "gender": "m"},
    {"id": "Neil", "label": "阿闻 · 播音旁白", "gender": "m"},
    {"id": "Dylan", "label": "晓东 · 北京话", "gender": "m", "dialect": True},
    {"id": "Sunny", "label": "晴儿 · 四川话", "gender": "f", "dialect": True},
    {"id": "Eric", "label": "程川 · 四川话", "gender": "m", "dialect": True},
    {"id": "Rocky", "label": "阿强 · 粤语", "gender": "m", "dialect": True},
    {"id": "Kiki", "label": "阿清 · 粤语", "gender": "f", "dialect": True},
    {"id": "Marcus", "label": "秦川 · 陕西话", "gender": "m", "dialect": True},
    {"id": "Peter", "label": "李彼得 · 天津话", "gender": "m", "dialect": True},
    {"id": "Roy", "label": "阿杰 · 闽南话", "gender": "m", "dialect": True},
    {"id": "Li", "label": "老李 · 南京话", "gender": "m", "dialect": True},
]
VOICE_IDS = {v["id"] for v in VOICES}
_FEMALE = [v["id"] for v in VOICES if v["gender"] == "f" and not v.get("dialect") and v["id"] != "Elias"]
_MALE = [v["id"] for v in VOICES if v["gender"] == "m" and not v.get("dialect") and v["id"] != "Neil"]
_FEMALE_HINT = re.compile(r"女|她|妃|姬|娘|母|姐|妹|婆|后$|公主|少女|仙子|夫人|小姐|丫鬟|闺")
_OLD_HINT = re.compile(r"老|翁|爷|叟|长老|师尊|祖|父亲|爹")
NARRATOR = {"旁白", "画外音", "OS", "VO", "解说", "字幕"}
QC_PROMPT = """你是严格的短剧视频质检员。前几张是本镜头应出现的角色/场景定版参考图（会标注名字），后面是成片里均匀抽出的帧。
请按分镜要求逐项核对，输出 JSON（不要其他文字）：
{"character_consistency":0-100,"prompt_adherence":0-100,
 "issues":[{"type":"角色不一致|画面不符|字幕水印|画面崩坏|多余人物|其他","severity":"轻微|明显|严重","detail":"一句话"}],
 "prompt_conflict":"",
 "summary":"一句话结论"}
character_consistency：只看角色长相、发型、服装是否与参考图一致。
prompt_adherence：只看画面是否演出了分镜写的动作、情绪、景别和关键道具。分镜要求的关键动作没有出现，最多 50 分；
  画面内容与分镜基本无关，最多 30 分。
severity：严重 = 观众一眼能看出、必须重抽；明显 = 影响观感、建议重抽；轻微 = 可以接受。
prompt_conflict：最后会给出当时发给视频模型的提示词。如果提示词本身和分镜不一致（例如分镜写电脑屏幕、提示词写手机），
  一句话写出哪里不一致、应该以分镜为准改成什么；一致就给空字符串。成片照着错误提示词演，问题在提示词，不在模型。
不算问题的：屏幕/纸面上应出现的具体文字、数字没画出来（视频模型画不准，后期贴字；只看有没有对应的屏幕或道具）；
屏幕、招牌、道具上自然出现的界面数字、图表、小字（这是真实质感；“不出现文字”只管字幕、水印、压在画面上的大字标题）；
角色服装与场景时代不同（以角色参考图为准，穿越题材常见）。
只写看得到的问题，不要编造；没有问题 issues 给空数组。"""

# 质检分数：不用模型自己给的总分（实测它会给 92 分，同时在评语里写“与分镜严重不符”），
# 按两个分项取较低者为主，再按问题严重程度扣分。
_THROTTLED = re.compile(r"Throttling|rate.?limit|RateQuota|\b429\b|too many requests", re.I)
_SEV_PENALTY = {"严重": 20, "明显": 8, "轻微": 2}
_HARD_TYPES = {"角色不一致", "画面崩坏", "字幕水印"}
_SEVERE_WORDS = re.compile(r"严重|完全不符|完全没有|未执行|没有执行|未按|没有按|未出现|没有出现|崩坏|错误切入")


def _sub(v) -> int | None:
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return None


def vision_score(vis: dict) -> tuple[int, bool, bool]:
    """视觉结果 → (分数, 必须重抽, 建议人工看)。"""
    cc, pa = _sub(vis.get("character_consistency")), _sub(vis.get("prompt_adherence"))
    subs = [x for x in (cc, pa) if x is not None]
    if subs:
        base = round(0.6 * min(subs) + 0.4 * (sum(subs) / len(subs)))
    else:
        base = _sub(vis.get("score")) or 0
    hard = severe = False
    penalty = 0
    for i in vis.get("issues") or []:
        sev = i.get("severity") or ("严重" if _SEVERE_WORDS.search(str(i.get("detail", ""))) else "明显")
        if sev not in _SEV_PENALTY:
            sev = "明显"
        i["severity"] = sev
        penalty += _SEV_PENALTY[sev]
        if sev == "严重":
            severe = True
            if i.get("type") in _HARD_TYPES:
                hard = True
    if _SEVERE_WORDS.search(str(vis.get("summary", ""))):
        severe = True
    score = max(0, min(100, base - min(penalty, 40)))
    if cc is not None and cc < 60:
        hard = True
    if severe and score > 74:      # 有严重问题就不能直接通过
        score = 74
    return score, hard, severe or (pa is not None and pa < 60)


def qc_view(raw: str) -> dict | None:
    q = db.load(raw, None) if raw else None
    if q:
        q["frame_urls"] = [media.url_of(p) for p in q.get("frames", []) if p]
    return q


class QcDubMixin:
    # ==================================================================
    # 自动质检
    # ==================================================================

    _qc_view = staticmethod(qc_view)

    def _tech_checks(self, j: dict, s: dict, st: dict, info: dict) -> list[dict]:
        checks = []
        want = float(j.get("duration_s") or s.get("duration_s") or 0)
        got = float(info.get("duration") or j.get("seconds") or 0)
        if want and got:
            ok = abs(got - want) <= max(1.5, want * 0.2)
            checks.append({"name": "时长", "ok": ok, "level": "warn",
                           "detail": f"要求 {want:g}s，实际 {got:.1f}s"})
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        aspect = j.get("aspect_ratio") or st.get("aspect_ratio") or "9:16"
        if w and h:
            try:
                a, b = [float(x) for x in aspect.split(":")]
                ok = abs((w / h) - (a / b)) / (a / b) <= 0.04
            except ValueError:
                ok = True
            checks.append({"name": "画幅", "ok": ok, "level": "error", "detail": f"{w}×{h}，要求 {aspect}"})
            res = str(j.get("resolution") or st.get("resolution") or "").upper().rstrip("P")
            if res.isdigit():
                ok = min(w, h) >= int(res) * 0.95
                checks.append({"name": "分辨率", "ok": ok, "level": "warn", "detail": f"短边 {min(w, h)}，要求 {res}P"})
        if self._dub_lines(s.get("dialogue") or "") and st.get("dub_mode", "off") == "off":
            checks.append({"name": "音轨", "ok": bool(info.get("has_audio")), "level": "warn",
                           "detail": "有音轨" if info.get("has_audio") else "有台词但视频没有声音（可开配音补上）"})
        return checks

    async def _qc_refs(self, pid: str, s: dict, assets: list[dict], limit: int = 3) -> list[tuple[str, str]]:
        """本镜头角色（含形态）+ 场景的定版图 → [(名字, 本地缩略图)]。"""
        names = [n for n in (s.get("characters") or [])]
        if s.get("scene_asset"):
            names.append(s["scene_asset"])
        out, seen = [], set()
        for n in names:
            base = re.sub(r"[（(].*$", "", n)
            form_name = (re.search(r"[（(]([^）)]+)", n) or [None, "默认"])[1]
            a = next((x for x in assets if x["name"] == base or base in x["aliases"]), None)
            if not a or a["kind"] == "audio" or a["id"] in seen:
                continue
            f = next((f for f in a["forms"] if f["name"] == form_name), None) \
                or next((f for f in a["forms"] if f["is_default"]), None)
            if not f or not f.get("local_path") or not os.path.exists(f["local_path"]) or f["media_type"] != "image":
                continue
            seen.add(a["id"])
            dst = media.path_for(pid, "qc", f"ref_{f['id']}_v{f['current_version']}.jpg")
            if not os.path.exists(dst):
                try:
                    await media.thumb(f["local_path"], dst)
                except Exception as e:  # 参考图缩略失败就跳过这一张
                    log.info("qc ref thumb failed: %s", e)
                    continue
            out.append((f"{a['name']}（{f['name']}）", dst if os.path.exists(dst) else f["local_path"]))
            if len(out) >= limit:
                break
        return out

    async def _qc_vision(self, pid: str, s: dict, frames: list[str], refs_: list[tuple[str, str]],
                         model: str, prompt: str = "") -> tuple[dict | None, float, str]:
        content: list[dict] = [{"type": "text", "text": QC_PROMPT}]
        for name, path in refs_:
            content += [{"type": "text", "text": f"参考：{name}"},
                        {"type": "image_url", "image_url": {"url": media.data_uri(path)}}]
        for i, p in enumerate(frames, 1):
            content += [{"type": "text", "text": f"成片第 {i} 帧"},
                        {"type": "image_url", "image_url": {"url": media.data_uri(p)}}]
        content.append({"type": "text", "text": (
            f"分镜 {s['code']}（{s.get('duration_s')}秒，{s.get('size', '')} {s.get('angle', '')} {s.get('movement', '')}）\n"
            f"画面：{s.get('picture', '')}\n表演：{s.get('performance', '')}\n人物：{'、'.join(s.get('characters') or [])}"
            + (f"\n\n当时发给视频模型的提示词：\n{prompt[:1500]}" if prompt else ""))})
        llm.current_project.set(pid)
        res = await self.agg.chat(model, [{"role": "user", "content": content}], max_tokens=700, temperature=0.1)
        llm.record_charge(res)
        charged = float(((res or {}).get("billing") or {}).get("charged_rmb") or 0)
        text = (((res.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        data = llm._extract_json(text)
        if not data or not any(k in data for k in ("score", "character_consistency", "prompt_adherence")):
            return None, charged, (text or "")[:200]
        data["issues"] = [i for i in (data.get("issues") or []) if isinstance(i, dict)][:8]
        data["model_score"] = _sub(data.get("score"))
        data["score"], data["hard_fail"], data["needs_review"] = vision_score(data)
        return data, charged, ""

    async def qc_job(self, job_id: str, force: bool = False, vision: bool | None = None) -> dict:
        with db.tx() as c:
            row = c.execute("SELECT j.*, s.project_id pid FROM jobs j JOIN shots s ON s.id=j.shot_id WHERE j.id=?",
                            (job_id,)).fetchone()
        if not row:
            raise NotFound(f"没有这个视频版本：{job_id}")
        j = dict(row)
        if j["qc_json"] and not force:
            return self._qc_view(j["qc_json"])
        if j["status"] != "succeeded":
            raise WorkflowError("这个版本还没出片成功，不能质检")
        if not j["local_path"] or not os.path.exists(j["local_path"]):
            raise WorkflowError("视频没有下载到本地，不能质检")
        pid = j["pid"]
        proj = self.get(pid)
        st = proj["settings"]
        s = next(x for x in proj["shots"] if x["id"] == j["shot_id"])
        info = await asyncio.to_thread(media.probe, j["local_path"])
        checks = self._tech_checks(j, s, st, info)
        frames = []
        try:
            frames = await media.sample_frames(j["local_path"], os.path.dirname(
                media.path_for(pid, "qc", f"{job_id}_f1.jpg")), 4, prefix=f"{job_id}_f")
        except Exception as e:
            checks.append({"name": "抽帧", "ok": False, "level": "warn", "detail": str(e)[:120]})
        use_vision = (st.get("engine") != "local" and llm.mode() != "mock" and bool(frames)
                      and media.is_mock_url(j["url"]) is False) if vision is None else (vision and bool(frames))
        vis, charged, note, model = None, 0.0, "", st.get("qc_model") or "qwen3-vl-plus"
        if use_vision:
            if self._spend(pid)["remaining_rmb"] < 0.05:
                note = "预算不足，跳过视觉质检"
            else:
                try:
                    refs_ = await self._qc_refs(pid, s, proj["assets"])
                    vis, charged, bad = await self._qc_vision(pid, s, frames, refs_, model, j.get("prompt_snapshot") or "")
                    if vis is None:
                        note = f"视觉模型没给出可解析的结果：{bad}"
                except AggError as e:
                    note = f"视觉质检失败：{str(e)[:160]}"
        else:
            note = "模拟/本地模式：只做技术检查"
        errors = [c for c in checks if not c["ok"] and c["level"] == "error"]
        warns = [c for c in checks if not c["ok"] and c["level"] != "error"]
        if vis:
            score = int(vis["score"]) - 15 * len(errors) - 5 * len(warns)
        else:
            score = 100 - 30 * len(errors) - 10 * len(warns)
        score = max(0, min(100, score))
        th = int(st.get("qc_pass_score") or 75)
        verdict = "fail" if errors or score < th - 20 or (vis or {}).get("hard_fail") else (
            "pass" if score >= th and not (vis or {}).get("needs_review") else "warn")
        issues = [{"type": c["name"], "detail": c["detail"]} for c in errors + warns]
        issues += (vis or {}).get("issues", [])
        summary = (vis or {}).get("summary") or (
            "技术检查全部通过" if not issues else "；".join(i["detail"] for i in issues[:3]))
        q = {"score": score, "verdict": verdict, "checks": checks, "issues": issues, "summary": summary,
             "vision": bool(vis), "model": model if vis else "",
             "character_consistency": (vis or {}).get("character_consistency"),
             "prompt_adherence": (vis or {}).get("prompt_adherence"),
             "prompt_conflict": str((vis or {}).get("prompt_conflict") or "").strip()[:300],
             "note": note, "frames": frames, "charged_rmb": round(charged, 5), "at": db.now()}
        with db.tx() as c:
            c.execute("UPDATE jobs SET qc_json=? WHERE id=?", (db.dump(q), job_id))
        return self._qc_view(db.dump(q))

    async def qc_project(self, pid: str, episode_no: int | None = None, shot_ids: list[str] | None = None,
                         force: bool = False) -> dict:
        proj = self.get(pid)
        targets = [s for s in proj["shots"] if s["job"] and s["job"]["status"] == "succeeded"
                   and (episode_no is None or s["episode_no"] == episode_no)
                   and (not shot_ids or s["id"] in shot_ids)]
        if not targets:
            raise WorkflowError("没有已出片的镜头可以质检")
        rs = await self.gather_limited(self.qc_job(s["job"]["id"], force=force) for s in targets)
        items, errs = [], []
        for s, r in zip(targets, rs):
            if isinstance(r, Exception):
                errs.append({"code": s["code"], "error": str(r)[:160]})
            else:
                items.append({"code": s["code"], "shot": s["id"], "job": s["job"]["id"], "score": r["score"],
                              "verdict": r["verdict"], "summary": r["summary"]})
        bad = [x["code"] for x in items if x["verdict"] == "fail"]
        warn = [x["code"] for x in items if x["verdict"] == "warn"]
        self.notify(pid, "error" if bad else ("warning" if warn else "success"),
                    f"质检完成：{len(items)} 镜，{len(bad)} 不合格，{len(warn)} 待看",
                    ("不合格：" + "、".join(bad[:20]) if bad else "") + (" 待看：" + "、".join(warn[:20]) if warn else ""))
        return {"project": pid, "checked": len(items), "pass": len(items) - len(bad) - len(warn),
                "warn": len(warn), "fail": len(bad), "items": items, "errors": errs}

    def _schedule_auto_qc(self, jobs: list[tuple[str, str]]) -> None:
        """出片成功后后台质检（不阻塞轮询）。jobs: [(project_id, job_id)]。"""
        todo = [(p, j) for p, j in jobs if self.settings(p).get("auto_qc")]
        if not todo:
            return
        if not hasattr(self, "_bg"):
            self._bg = set()

        async def run():
            fails: dict[str, list[str]] = {}
            results: dict[str, list[tuple[str, dict]]] = {}
            for p, jid in todo:
                try:
                    q = await self.qc_job(jid)
                    if q:
                        results.setdefault(p, []).append((jid, q))
                    if q and q["verdict"] == "fail":
                        with db.tx() as c:
                            r = c.execute("SELECT s.shot_no, e.no FROM jobs j JOIN shots s ON s.id=j.shot_id "
                                          "JOIN episodes e ON e.id=s.episode_id WHERE j.id=?", (jid,)).fetchone()
                        fails.setdefault(p, []).append(f"{r['no']}-{r['shot_no']}：{q['summary'][:40]}")
                except Exception as e:  # 质检失败不影响出片
                    log.warning("auto qc %s failed: %s", jid, e)
            for p, lines in fails.items():
                self.notify(p, "warning", f"自动质检：{len(lines)} 个镜头不合格，建议重抽", "\n".join(lines[:10]))
            for p, rs in results.items():   # 重抽版本择优 + 自动重抽
                try:
                    await self.after_auto_qc(p, rs)
                except Exception as e:
                    log.warning("after_auto_qc %s failed: %s", p, e)

        t = asyncio.get_running_loop().create_task(run())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def drain_background(self) -> None:
        """测试/关闭时等后台质检跑完。"""
        while getattr(self, "_bg", None):
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ==================================================================
    # 配音
    # ==================================================================

    @staticmethod
    def _dub_lines(dialogue: str) -> list[tuple[str, str]]:
        """「角色（表演）：台词」/「旁白：…」/ 无前缀 → [(说话人, 台词)]；台词里的括号表演提示去掉。"""
        out = []
        for ln in (dialogue or "").splitlines():
            ln = ln.strip().strip("{}").strip()
            if not ln:
                continue
            m = re.match(r"^([^：:「“\"]{1,16})[：:]\s*(.+)$", ln)
            speaker, text = (m.group(1), m.group(2)) if m else ("旁白", ln)
            speaker = re.sub(r"[（(][^）)]*[）)]", "", speaker).strip() or "旁白"
            text = re.sub(r"[（(][^）)]*[）)]", "", text).strip().strip("“”\"「」").strip()
            if re.fullmatch(r"[\W_]*", text):
                continue
            out.append((speaker, text[:600]))
        return out

    @staticmethod
    def _auto_voice(a: dict | None, used: set[str]) -> str:
        text = (a or {}).get("name", "") + (a or {}).get("description", "")[:120]
        female = bool(_FEMALE_HINT.search(text))
        pool = _FEMALE if female else _MALE
        if not female and _OLD_HINT.search(text):
            pool = ["Eldric Sage", "Arthur", "Vincent"] + pool
        seed = int(short_hash((a or {}).get("id", text) or "x"), 16) if a else 0
        for k in range(len(pool)):
            v = pool[(seed + k) % len(pool)]
            if v not in used:
                return v
        return pool[seed % len(pool)]

    def voice_map(self, proj: dict) -> dict[str, dict]:
        """角色名/别名 → {voice, asset_id, auto}。没指定音色的角色自动分配（同项目尽量不重复）。"""
        chars = [a for a in proj["assets"] if a["kind"] in ("character", "creature")]
        used = {a["tts_voice"] for a in chars if a.get("tts_voice")}
        out = {}
        for a in chars:
            v, auto = a.get("tts_voice") or "", False
            if not v:
                v, auto = self._auto_voice(a, used), True
                used.add(v)
            for k in [a["name"]] + a["aliases"]:
                out[k] = {"voice": v, "asset_id": a["id"], "auto": auto, "name": a["name"]}
        return out

    def voices(self, pid: str | None = None) -> dict:
        out = {"voices": VOICES}
        if pid:
            proj = self.get(pid)
            vm = self.voice_map(proj)
            out["narrator"] = proj["settings"].get("narrator_voice") or "Neil"
            out["characters"] = [{"asset_id": a["id"], "name": a["name"], **{k: vm[a["name"]][k] for k in ("voice", "auto")}}
                                 for a in proj["assets"] if a["name"] in vm]
        return out

    def set_voice(self, aid: str, voice: str) -> dict:
        voice = (voice or "").strip()
        if voice and voice not in VOICE_IDS:
            raise WorkflowError(f"没有这个音色：{voice}")
        with db.tx() as c:
            r = c.execute("SELECT project_id, kind FROM assets WHERE id=?", (aid,)).fetchone()
            if not r:
                raise NotFound(f"没有这个资产：{aid}")
            if r["kind"] not in ("character", "creature"):
                raise WorkflowError("只有角色/生物可以设置配音音色")
            c.execute("UPDATE assets SET tts_voice=?, updated_at=? WHERE id=?", (voice, db.now(), aid))
        return {"asset": aid, "voice": voice}

    def _add_audio_spend(self, pid: str, rmb: float) -> None:
        if rmb:
            with db.tx() as c:
                c.execute("UPDATE projects SET audio_charged_rmb=audio_charged_rmb+? WHERE id=?", (rmb, pid))

    def _tts_sem(self) -> asyncio.Semaphore:
        """语音合成单独限并发：百炼 TTS 的频率限制比文本低，2026-10-31 实测 4 路并发就有镜头被限流。"""
        loop = asyncio.get_running_loop()
        sem = getattr(self, "_tts_sem_obj", None)
        if sem is None or getattr(self, "_tts_sem_loop", None) is not loop:
            sem = asyncio.Semaphore(max(1, int(os.environ.get("WORKFLOW_TTS_PARALLEL", "2"))))
            self._tts_sem_obj, self._tts_sem_loop = sem, loop
        return sem

    async def _tts_call(self, model: str, text: str, voice: str) -> dict:
        """限流（Throttling / rate limit / 429）时退避重试，其余错误直接抛。"""
        tries = max(1, int(os.environ.get("WORKFLOW_TTS_RETRIES", "5")))
        delay = float(os.environ.get("WORKFLOW_TTS_BACKOFF_S", "2"))
        for k in range(tries):
            try:
                async with self._tts_sem():
                    return await self.agg.tts(model, text, voice, idem=f"tts-{short_hash(model + voice + text)}")
            except AggError as e:
                if k == tries - 1 or not _THROTTLED.search(f"{e} {e.code or ''} {e.status or ''}"):
                    raise
                log.info("tts throttled, retry %d in %.1fs", k + 1, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        raise WorkflowError("unreachable")

    async def _tts_to_file(self, pid: str, model: str, text: str, voice: str, path: str) -> float:
        res = await self._tts_call(model, text, voice)
        url = res.get("url") or ""
        if not url or media.is_mock_url(url):
            secs = float((parse_qs(urlparse(url).query).get("sec") or [0])[0] or 0) or max(0.6, len(text) * 0.22)
            media.tone_wav(path, secs, seed=voice + text)
        else:
            data = await self.agg.fetch(url)
            with open(path, "wb") as f:
                f.write(data)
        rmb = float((res.get("billing") or {}).get("charged_rmb") or 0)
        self._add_audio_spend(pid, rmb)
        return rmb

    async def voice_sample(self, pid: str, voice: str, text: str = "") -> dict:
        if voice not in VOICE_IDS:
            raise WorkflowError(f"没有这个音色：{voice}")
        st = self.settings(pid)
        text = (text or "你好，我是这部短剧里的角色，这是我的声音。").strip()[:80]
        path = media.path_for(pid, "dub", f"sample_{voice.replace(' ', '_')}_{short_hash(text)}.wav")
        rmb = 0.0
        if not os.path.exists(path):
            rmb = await self._tts_to_file(pid, st.get("tts_model") or "qwen3-tts-flash", text, voice, path)
        return {"voice": voice, "url": media.url_of(path), "charged_rmb": rmb}

    def _dub_plan(self, proj: dict, s: dict) -> list[dict]:
        vm = self.voice_map(proj)
        narrator = proj["settings"].get("narrator_voice") or "Neil"
        plan = []
        for speaker, text in self._dub_lines(s.get("dialogue") or ""):
            hit = None if speaker in NARRATOR else (vm.get(speaker) or next(
                (v for k, v in vm.items() if k and (k in speaker or speaker in k)), None))
            plan.append({"speaker": hit["name"] if hit else ("旁白" if speaker in NARRATOR else speaker),
                         "voice": hit["voice"] if hit else narrator, "text": text})
        return plan

    async def dub_shots(self, pid: str, shot_ids: list[str] | None = None, episode_no: int | None = None,
                        force: bool = False) -> dict:
        proj = self.get(pid)
        st = proj["settings"]
        model = st.get("tts_model") or "qwen3-tts-flash"
        targets = [s for s in proj["shots"] if (not shot_ids or s["id"] in shot_ids)
                   and (episode_no is None or s["episode_no"] == episode_no)]
        work = []
        for s in targets:
            plan = self._dub_plan(proj, s)
            if not plan:
                continue
            dur = float((s["job"] or {}).get("seconds") or 0) if s["job"] and s["job"]["status"] == "succeeded" \
                else 0.0
            dur = dur or float(s["duration_s"] or 5)
            key = short_hash(json.dumps([model, dur, plan], ensure_ascii=False))
            old = s.get("dub") or {}
            if not force and old.get("key") == key and s.get("dub_path") and os.path.exists(s["dub_path"]):
                continue
            work.append((s, plan, dur, key))
        if not work:
            return {"project": pid, "dubbed": 0, "skipped": len(targets), "charged_rmb": 0.0, "items": []}
        chars = sum(len(x["text"]) for _, plan, _, _ in work for x in plan)
        try:
            m = await self.model_info(model)
            est = float((m.get("price") or {}).get("rmb_per_10k_chars") or 1.0) * chars / 10000
        except WorkflowError:
            est = chars / 10000
        if est > self._spend(pid)["remaining_rmb"]:
            raise BudgetExceeded(f"配音预计 {est:.3f} 元（{chars} 字），超过项目剩余预算",
                                 {"estimate_rmb": round(est, 4), "chars": chars, **self._spend(pid)})
        llm.current_project.set(pid)

        async def one(s, plan, dur, key):
            folder_key = f"{s['id']}_{key}"
            paths = []
            charged = 0.0
            for i, x in enumerate(plan):
                p = media.path_for(pid, "dub", f"{folder_key}_{i:02d}.wav")
                charged += await self._tts_to_file(pid, model, x["text"], x["voice"], p)
                paths.append({**x, "path": p})
            out = media.path_for(pid, "dub", f"{s['id']}_dub.wav")
            track = await media.dub_track(paths, out, dur)
            info = {"key": key, "model": model, "seconds": dur, "tempo": track["tempo"],
                    "overflow": track["overflow"], "lines": track["lines"], "charged_rmb": round(charged, 5),
                    "at": db.now()}
            with db.tx() as c:
                c.execute("UPDATE shots SET dub_path=?, dub_json=? WHERE id=?", (out, db.dump(info), s["id"]))
                c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id=?",
                          (s["episode_id"],))
            return {"code": s["code"], "shot": s["id"], "lines": len(plan), "tempo": track["tempo"],
                    "overflow": track["overflow"], "charged_rmb": round(charged, 5), "url": media.url_of(out)}

        rs = await self.gather_limited(one(*w) for w in work)
        items, errs = [], []
        for w, r in zip(work, rs):
            if isinstance(r, Exception):
                errs.append({"code": w[0]["code"], "error": str(r)[:200]})
            else:
                items.append(r)
        if errs and not items:
            raise WorkflowError("配音失败：" + "；".join(f"{e['code']} {e['error']}" for e in errs[:3]))
        over = [x["code"] for x in items if x["overflow"]]
        self.notify(pid, "warning" if over or errs else "success", f"配音完成：{len(items)} 个镜头",
                    ("台词超出镜头时长（已截断）：" + "、".join(over) if over else "")
                    + ("；失败：" + "、".join(e["code"] for e in errs) if errs else ""))
        return {"project": pid, "dubbed": len(items), "skipped": len(targets) - len(work), "items": items,
                "errors": errs, "charged_rmb": round(sum(x["charged_rmb"] for x in items), 5)}

    def clear_dub(self, pid: str, shot_ids: list[str] | None = None) -> dict:
        with db.tx() as c:
            rows = c.execute("SELECT id, dub_path, episode_id FROM shots WHERE project_id=? AND dub_path!=''",
                             (pid,)).fetchall()
            n = 0
            for r in rows:
                if shot_ids and r["id"] not in shot_ids:
                    continue
                c.execute("UPDATE shots SET dub_path='', dub_json='' WHERE id=?", (r["id"],))
                c.execute("UPDATE episodes SET final_path='', srt_path='', final_seconds=0 WHERE id=?",
                          (r["episode_id"],))
                n += 1
        return {"project": pid, "cleared": n}
