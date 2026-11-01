"""剧本增强：故事圣经 → 分集节拍表 → 逐集写 → 代码指标 + 模型审核（A/B/C/D）→ 定向改写；剧本版本可撤销。

借鉴（2026-11 调研）：MYStudio 漫影（付费卡点按总集数 10/30/50/70/90% 定位、集末钩子三类、代码先算指标再让
审核采信、A-D 分级、屏幕文字改后期贴字）、火宝短剧（道具宁少勿滥、日常物品不入表）、Dramatron / AI Comic Builder
（分层生成，每层可人工改）、ViMax（重要人/物每次写全名、写清谁在哪）、MovieAgent（同框不超过两人）、
scriptify / OnlyShot（钩子分强弱、禁用心理描写）。提示词为本项目自写。

面向 AI 视频出片的可拍性约束（第 1 集实拍教训）：
- 屏幕/短信/牌匾上的字一律写成【贴字】行，画面只写“屏幕亮着红绿K线”这类可视描述；
- 关键道具写全名（如“营业部台式电脑”），并列出容易被误写成的东西（手机、平板），下游提示词出现就报警；
- 每场人物 ≤3，每个 △ 动作行只写一个主动作。
"""
from __future__ import annotations

import json
import logging
import re

from . import db, llm
from .agg import AggError
from .errors import NotFound, WorkflowError
from .util import short_hash

log = logging.getLogger("workflow.script")

GENRES = ["都市逆袭", "穿越", "重生复仇", "甜宠", "战神", "悬疑", "古风权谋", "玄幻修仙", "家庭伦理", "喜剧"]
HOOK_TYPES = ("主角下一步行动", "反派反击", "第三方态度")
MODES = ("压", "放")
SPEECH_CPS = 4.0          # 审核按每秒 4 字算台词（比分镜的 4.5 更保守，留表演空间）
LINE_MAX = 15             # 竖屏单句字幕不超过 15 字
BANNED = ("心想", "暗道", "心里想", "内心独白", "暗自思忖", "不由得想")
SCREEN_WORDS = re.compile(r"(屏幕|短信|微信|手机上|显示|写着|牌匾|横幅|弹幕|消息|通知|报纸|告示)")
TIEZI = re.compile(r"^\s*【贴字】")


def paywall_episodes(n: int) -> list[int]:
    """付费卡点集：10 集以上按总集数约 10/30/50/70/90% 定位；5~9 集只在中段和结尾；5 集以下只在最后一集。
    （集数少时按百分比会挤成每集都是卡点，失去意义。）"""
    if n < 5:
        return [n]
    ps = (0.5, 0.9) if n < 10 else (0.1, 0.3, 0.5, 0.7, 0.9)
    return sorted({max(1, min(n, round(n * p))) for p in ps})


# ======================================================================
# 提示词
# ======================================================================

BIBLE_SYSTEM = """你是短剧总编剧。先不写正文，只做“故事圣经 + 分集节拍表”，只输出 JSON：
{"title":"剧名","logline":"一句话：谁+想要什么+最大阻碍+反差卖点","genre":"类型","tone":"基调",
 "characters":[{"name":"全名","tags":"身份标签（3个以内）","desire":"欲望","weakness":"软肋","catchphrase":"口头禅",
   "look":"固定外貌：年龄段、发型、服装主色、标志物（用于出图，写具体）"}],
 "props":[{"name":"道具全名（带限定词，如“营业部台式电脑”）","look":"外观","avoid":["容易被误写成的东西"],"episodes":[1,2]}],
 "beats":[{"ep":1,"mode":"压|放","hook_open":"前3秒钩子（具体动作/冲突，不要旁白开场）","twist":"中段反转",
   "payoff":"本集爽点","hook_end":"集末钩子","hook_type":"主角下一步行动|反派反击|第三方态度",
   "characters":["本集出场人物"],"scenes":["场景"],"props":["本集关键道具全名"]}]}
规则：
1. 人物 3~6 个，主角欲望和软肋要能撑起整部剧；外貌字段写具体，后面出图要用。
2. 道具宁少勿滥：每集 0~3 个关键道具；手机、筷子这类随手物品不进表（除非它是剧情关键）。
   avoid 写最容易被画错成的替代物（如电脑→手机、平板）。
3. 每集只做“压”（主角受挫、积蓄情绪）或“放”（主角反击、释放爽感）之一，压放交替，前 3 集至少 1 次“放”。
4. 集末钩子只能是三类之一，且不能连续 3 集同一类。
5. 题材要可拍：每场出场人物不超过 3 个，避免大场面群戏、复杂特效连续动作。"""

EPISODE_SYSTEM = """你是短剧编剧。按“故事圣经 + 本集节拍”写第 %d 集剧本正文，只输出剧本，格式严格如下：
第%d集 集名
%d-1 日 外 地点 ·约20秒
人物：甲、乙
△动作描写（一行只写一个看得见的主动作）
甲（情绪）：台词
【贴字】屏幕上要出现的文字（视频模型画不准字，后期贴）
要求：
1. 本集约 %d 秒，%d 场左右；每场标题末尾写“·约N秒”，各场秒数加起来约等于本集时长。
2. 前 3 秒就是节拍里的开场钩子（动作或冲突，不要旁白开场）；结尾落在集末钩子上，停在悬念处。
3. 必须兑现节拍里的反转和爽点；人物、道具用圣经里的全名，提到关键道具每次都写全名，不要换成别的东西。
4. 台词口语化，每句不超过 15 字（长台词拆成几句）；台词按每秒 4 字算要念得完。
5. 每场出场人物不超过 3 个；△ 行只写一个动作，不写比喻和心理活动（不要“心想/暗道”）。
6. 屏幕、手机、短信、牌匾上的字一律写成单独的【贴字】行；△ 里只写画面（如“电脑屏幕上红绿K线跳动”）。"""

REVIEW_SYSTEM = """你是短剧剧本审核。下面给出：本集节拍、代码算好的指标（直接采信这些数字，不要自己数）、剧本正文。
按 6 项审核，只输出 JSON：
{"scores":{"开场钩子":0-10,"集末钩子":0-10,"节拍兑现":0-10,"情绪密度":0-10,"人设一致":0-10,"可拍性":0-10},
 "issues":[{"severity":"严重|中等|轻微","item":"6项之一","scene":"场次号如1-2，全集问题写空","detail":"问题","fix":"具体怎么改"}],
 "summary":"一句话"}
标准：开场 3 秒内要有动作或冲突；集末停在悬念；节拍里的反转和爽点要能在正文指出位置；每 60 秒至少 3 个情绪点；
人物言行符合人设；可拍性看每场人数、动作是否一行一个、屏幕文字是否写成【贴字】、道具是否用全名。
严重 = 不改不能拍或观众会划走；中等 = 明显影响效果；轻微 = 可改可不改。只写真实存在的问题。"""

REVISE_SYSTEM_EP = """你是短剧编剧，按审核意见修改本集剧本。只改被点名的场次和问题，其他内容和格式保持不变；
人物、道具继续用圣经里的全名；屏幕文字写成【贴字】行；每场标题末尾保留“·约N秒”。只输出改好的整集剧本。"""


# ======================================================================
# 本地规则（模拟/零成本）
# ======================================================================

def mock_bible(idea: str, episodes: int, genre: str = "") -> dict:
    idea = (idea or "").strip() or "一个普通人意外获得改变命运的能力"
    hero, rival, ally = "林舟", "沈岚", "老周"
    pay = set(paywall_episodes(episodes))
    beats = []
    for n in range(1, episodes + 1):
        mode = "压" if n % 2 == 1 else "放"
        beats.append({"ep": n, "mode": mode, "hook_open": f"{hero}被当众拦下，手里的东西摔在地上",
                      "twist": f"{rival}的底牌被{ally}一句话揭穿", "payoff": f"{hero}当众扳回一局",
                      "hook_end": f"{rival}拨通了一个神秘号码", "hook_type": HOOK_TYPES[(n - 1) % 3],
                      "paywall": n in pay, "characters": [hero, rival, ally][: 2 + n % 2],
                      "scenes": ["老街茶馆", "公司会议室"], "props": ["旧怀表"] if n == 1 else []})
    return {"title": idea[:12], "logline": idea[:60], "genre": genre or "都市逆袭", "tone": "反差喜剧",
            "characters": [
                {"name": hero, "tags": "落魄青年、隐藏天赋", "desire": "翻身", "weakness": "嘴硬心软", "catchphrase": "这一次我不退",
                 "look": "二十多岁，短发，深蓝夹克，左手腕旧怀表"},
                {"name": rival, "tags": "精英女总监", "desire": "证明算法", "weakness": "从不认输", "catchphrase": "数据不会骗人",
                 "look": "三十岁，盘发，白西装，金丝眼镜"},
                {"name": ally, "tags": "茶馆老板", "desire": "护住小店", "weakness": "好面子", "catchphrase": "喝茶喝茶",
                 "look": "五十多岁，光头，灰布褂"}],
            "props": [{"name": "旧怀表", "look": "铜色、表盖有划痕", "avoid": ["手表", "手机"], "episodes": [1]}],
            "beats": beats}


def mock_episode(bible: dict, beat: dict, seconds: int) -> str:
    n = beat["ep"]
    chars = beat.get("characters") or [c["name"] for c in bible["characters"][:2]]
    a, b = chars[0], chars[1] if len(chars) > 1 else chars[0]
    s1, s2 = ((beat.get("scenes") or ["老街茶馆", "公司会议室"]) + ["街口"] * 2)[:2]
    half = max(10, seconds // 2)
    return (f"第{n}集 {beat.get('payoff', '')[:8]}\n"
            f"{n}-1 日 内 {s1} ·约{half}秒\n人物：{a}、{b}\n"
            f"△{beat['hook_open']}。\n{a}（咬牙）：这一次我不退。\n{b}（冷笑）：你拿什么跟我赌？\n"
            f"△{a}把手按在桌上。\n"
            f"{n}-2 日 外 {s2} ·约{seconds - half}秒\n人物：{a}、{b}\n"
            f"△{beat['twist']}。\n{a}：输的人，是你。\n【贴字】{beat['payoff'][:12]}\n"
            f"△{beat['hook_end']}。\n")


# ======================================================================
# 代码指标（审核直接采信，不让模型自己数）
# ======================================================================

def _lines(text: str):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def metrics(ep_text: str, target_s: int, props: list[dict] | None = None) -> dict:
    scenes = llm.split_scenes(1, ep_text)
    declared, issues = 0.0, []
    est_total = 0.0
    long_lines, crowded, screen_unmarked, banned, busy = [], [], [], [], []
    avoid = {w: p["name"] for p in (props or []) for w in (p.get("avoid") or []) if w}
    misnamed = []
    for k, sc in enumerate(scenes, start=1):
        m = re.search(r"约\s*(\d+)\s*秒", sc.get("heading") or "")
        declared += float(m.group(1)) if m else 0
        people = re.search(r"^\s*人物\s*[：:]\s*(.+)$", sc["text"], re.M)
        n_people = len([x for x in re.split(r"[、，,\s]+", people.group(1)) if x]) if people else 0
        if n_people > 3:
            crowded.append(f"第{k}场 {n_people} 人")
        est = 0.0
        for ln in _lines(sc["text"]):
            if ln.startswith("人物"):
                continue
            if TIEZI.match(ln):
                est += 1.0
                continue
            if ln.startswith("△"):
                est += 2.0
                body = ln[1:]
                if SCREEN_WORDS.search(body) and re.search(r"[“\"「『].+[”\"」』]", body):
                    screen_unmarked.append(body[:30])
                if len(re.findall(r"[，,；;]", body)) >= 3 or len(body) > 45:
                    busy.append(body[:24])
                for w, name in avoid.items():
                    if w in body and name not in body:
                        misnamed.append(f"“{w}”（应为{name}）")
            sp = llm.SPEAKER_RE.match(ln)
            if sp:
                words = re.sub(r"[（(][^）)]*[）)]", "", ln[sp.end():]).strip()
                n = len(re.sub(r"[\W_]", "", words))
                est += n / SPEECH_CPS + 0.3
                for piece in re.split(r"[。！？!?…]+", words):
                    if len(re.sub(r"[\W_]", "", piece)) > LINE_MAX:
                        long_lines.append(piece[:20])
                for w, name in avoid.items():
                    if w in words and name not in words:
                        misnamed.append(f"“{w}”（应为{name}）")
            if any(b in ln for b in BANNED):
                banned.append(ln[:20])
        est_total += est
    total = declared or est_total
    out = {"scenes": len(scenes), "declared_seconds": round(declared, 1), "estimated_seconds": round(est_total, 1),
           "target_seconds": target_s, "long_lines": long_lines[:8], "crowded_scenes": crowded,
           "screen_text_unmarked": screen_unmarked[:5], "busy_actions": busy[:5], "banned_words": banned[:5],
           "misnamed_props": sorted(set(misnamed))[:5], "tiezi": sum(1 for ln in _lines(ep_text) if TIEZI.match(ln))}
    if target_s and abs(total - target_s) > max(10, target_s * 0.15):
        issues.append({"severity": "中等", "item": "可拍性", "scene": "", "detail": f"时长约 {total:.0f} 秒，目标 {target_s} 秒",
                       "fix": "增删场次或台词，让各场秒数合计接近目标"})
    if est_total and declared and est_total > declared * 1.4:
        issues.append({"severity": "中等", "item": "可拍性", "scene": "",
                       "detail": f"按台词和动作估算要 {est_total:.0f} 秒，标注只有 {declared:.0f} 秒",
                       "fix": "删减台词或调大场次时长"})
    for name, sev, msg, fix in (("long_lines", "轻微", "台词单句超过 15 字", "拆成短句"),
                                ("crowded_scenes", "中等", "同场人物超过 3 人", "拆场或让次要人物退场"),
                                ("screen_text_unmarked", "中等", "屏幕/短信文字写在动作行里", "改成单独的【贴字】行"),
                                ("busy_actions", "轻微", "一行动作写了多个动作", "一行只写一个主动作"),
                                ("banned_words", "轻微", "出现心理描写", "改成看得见的表情和动作"),
                                ("misnamed_props", "严重", "关键道具被写成了别的东西", "改回圣经里的道具全名")):
        if out[name]:
            issues.append({"severity": sev, "item": "可拍性", "scene": "", "detail": f"{msg}：{'、'.join(out[name][:3])}",
                           "fix": fix})
    out["issues"] = issues
    return out


def grade(issues: list[dict]) -> str:
    sev = [i.get("severity") for i in issues]
    s, m = sev.count("严重"), sev.count("中等")
    if s >= 3:
        return "D"
    if s or m > 4:
        return "C"
    if m > 2:
        return "B"
    return "A"


# ======================================================================
# 模型调用
# ======================================================================

async def make_bible(agg, model: str, idea: str, episodes: int, genre: str, engine: str | None) -> tuple[dict, str]:
    if llm.use_model(engine):
        try:
            data = await llm._ask_json(agg, model, BIBLE_SYSTEM,
                                       f"类型：{genre or '按素材判断'}\n集数：{episodes}\n素材：\n{idea[:12000]}", 6000)
            if data and data.get("characters") and len(data.get("beats") or []) >= max(1, episodes - 1):
                return normalize_bible(data, episodes), "real"
            log.warning("故事圣经输出不完整，改用本地模板")
        except AggError as e:
            if llm._strict(engine):
                raise
            log.warning("故事圣经生成失败，改用本地模板：%s", e)
    return normalize_bible(mock_bible(idea, episodes, genre), episodes), "mock"


def normalize_bible(b: dict, episodes: int) -> dict:
    pay = set(paywall_episodes(episodes))
    beats = {int(x.get("ep") or i + 1): x for i, x in enumerate(b.get("beats") or []) if isinstance(x, dict)}
    out_beats, last = [], []
    for n in range(1, episodes + 1):
        x = dict(beats.get(n) or {"ep": n})
        x["ep"] = n
        x["mode"] = x.get("mode") if x.get("mode") in MODES else ("压" if n % 2 else "放")
        x["hook_type"] = x.get("hook_type") if x.get("hook_type") in HOOK_TYPES else HOOK_TYPES[(n - 1) % 3]
        if len(last) >= 2 and last[-1] == last[-2] == x["hook_type"]:   # 同类钩子不连续 3 集
            x["hook_type"] = next(h for h in HOOK_TYPES if h != last[-1])
        last.append(x["hook_type"])
        x["paywall"] = n in pay                                          # 卡点位置由代码定，不信模型
        for k in ("hook_open", "twist", "payoff", "hook_end"):
            x[k] = str(x.get(k) or "")[:120]
        for k in ("characters", "scenes", "props"):
            x[k] = [str(v) for v in (x.get(k) or [])][:6]
        out_beats.append(x)
    props = [p for p in (b.get("props") or []) if isinstance(p, dict) and p.get("name")][:12]
    for p in props:
        p["avoid"] = [str(w) for w in (p.get("avoid") or []) if w and str(w) not in p["name"]][:4]
    return {"title": str(b.get("title") or "")[:30], "logline": str(b.get("logline") or "")[:200],
            "genre": str(b.get("genre") or ""), "tone": str(b.get("tone") or ""),
            "characters": [c for c in (b.get("characters") or []) if isinstance(c, dict) and c.get("name")][:8],
            "props": props, "beats": out_beats}


def _bible_brief(bible: dict, beat: dict) -> str:
    chars = "\n".join(f"- {c['name']}：{c.get('tags', '')}；欲望 {c.get('desire', '')}；软肋 {c.get('weakness', '')}；"
                      f"口头禅“{c.get('catchphrase', '')}”；外貌 {c.get('look', '')}" for c in bible["characters"])
    props = "\n".join(f"- {p['name']}（{p.get('look', '')}；不要写成：{'、'.join(p.get('avoid') or []) or '—'}）"
                      for p in bible["props"]) or "（无）"
    return (f"剧名：{bible['title']}｜一句话：{bible['logline']}｜类型：{bible['genre']}｜基调：{bible['tone']}\n"
            f"人物：\n{chars}\n关键道具：\n{props}\n"
            f"本集节拍：{json.dumps(beat, ensure_ascii=False)}")


async def write_episode(agg, model: str, bible: dict, beat: dict, seconds: int, scenes: int,
                        prev_hook: str, engine: str | None) -> tuple[str, str]:
    n = beat["ep"]
    if llm.use_model(engine):
        try:
            text = await llm.ask(agg, model, EPISODE_SYSTEM % (n, n, n, seconds, scenes),
                                 _bible_brief(bible, beat) + (f"\n上集结尾：{prev_hook}" if prev_hook else ""), 4000, 0.7)
            text = (text or "").strip()
            if re.search(rf"第\s*{n}\s*集", text) and llm.scene_heading_any(text):
                return text, "real"
            log.warning("第 %d 集输出格式不对，改用本地模板", n)
        except AggError as e:
            if llm._strict(engine):
                raise
            log.warning("第 %d 集生成失败，改用本地模板：%s", n, e)
    return mock_episode(bible, beat, seconds), "mock"


async def review_episode(agg, model: str, text: str, beat: dict | None, m: dict, engine: str | None) -> dict:
    issues = list(m["issues"])
    scores, summary, source = {}, "", "mock"
    if llm.use_model(engine):
        try:
            facts = {k: v for k, v in m.items() if k != "issues"}
            data = await llm._ask_json(agg, model, REVIEW_SYSTEM,
                                       f"本集节拍：{json.dumps(beat or {}, ensure_ascii=False)}\n"
                                       f"代码指标：{json.dumps(facts, ensure_ascii=False)}\n剧本：\n{text[:8000]}", 2500)
            if data and isinstance(data.get("issues"), list):
                issues += [i for i in data["issues"] if isinstance(i, dict) and i.get("detail")][:12]
                scores = {k: v for k, v in (data.get("scores") or {}).items() if isinstance(v, (int, float))}
                summary, source = str(data.get("summary") or ""), "real"
        except AggError as e:
            if llm._strict(engine):
                raise
            log.warning("剧本审核失败，只用代码指标：%s", e)
    for i in issues:
        if i.get("severity") not in ("严重", "中等", "轻微"):
            i["severity"] = "中等"
    g = grade(issues)
    return {"grade": g, "scores": scores, "issues": issues, "metrics": {k: v for k, v in m.items() if k != "issues"},
            "text_hash": short_hash(text),
            "summary": summary or ("代码指标全部通过" if not issues else "；".join(i["detail"] for i in issues[:2])),
            "source": source, "at": db.now()}


async def revise_episode(agg, model: str, text: str, review: dict, bible: dict | None, beat: dict | None,
                         engine: str | None) -> tuple[str, str]:
    targets = [i for i in review["issues"] if i["severity"] in ("严重", "中等")] or review["issues"]
    if not targets:
        return text, "none"
    if llm.use_model(engine):
        try:
            ctx = _bible_brief(bible, beat or {"ep": 0}) if bible else ""
            new = await llm.ask(agg, model, REVISE_SYSTEM_EP,
                                f"{ctx}\n审核意见：\n" + "\n".join(
                                    f"- [{i['severity']}][{i.get('scene') or '全集'}] {i['detail']} → {i.get('fix', '')}"
                                    for i in targets) + f"\n\n原剧本：\n{text}", 4000, 0.5)
            new = (new or "").strip()
            head = re.match(r"\s*(第\s*\d+\s*集[^\n]*)", text)
            if new and llm.scene_heading_any(new) and len(new) > len(text) * 0.5:
                if head and not re.match(r"\s*第\s*\d+\s*集", new):
                    new = head.group(1) + "\n" + new
                return new, "real"
        except AggError as e:
            if llm._strict(engine):
                raise
            log.warning("剧本改写失败，改用本地规则：%s", e)
    return mock_fix(text, bible), "mock"


def mock_fix(text: str, bible: dict | None) -> str:
    """本地规则能修的：屏幕文字挪到【贴字】、道具名改回全名、删心理描写词、长句切开。"""
    out = []
    avoid = {w: p["name"] for p in ((bible or {}).get("props") or []) for w in (p.get("avoid") or [])}
    for ln in text.splitlines():
        s = ln
        for w, name in avoid.items():
            if w in s and name not in s and not llm.scene_heading(s):
                s = s.replace(w, name)
        for b in BANNED:
            s = s.replace(b, "")
        if s.strip().startswith("△") and SCREEN_WORDS.search(s):
            q = re.search(r"[“\"「『](.+?)[”\"」』]", s)
            if q:
                out.append(s.replace(q.group(0), "").rstrip("，,：: "))
                out.append(f"【贴字】{q.group(1)}")
                continue
        out.append(s)
    return "\n".join(out)


# ======================================================================
# 工作流接入
# ======================================================================

class ScriptMixin:
    # ---------------- 版本 ----------------
    def _snapshot_script(self, pid: str, note: str) -> None:
        p = self._project_row(pid)
        if not p["script_text"].strip():
            return
        with db.tx() as c:
            c.execute("INSERT INTO script_versions(project_id,text,note,created_at) VALUES(?,?,?,?)",
                      (pid, p["script_text"], note[:100], db.now()))
            c.execute("DELETE FROM script_versions WHERE project_id=? AND id NOT IN (SELECT id FROM script_versions "
                      "WHERE project_id=? ORDER BY id DESC LIMIT 30)", (pid, pid))

    def script_versions(self, pid: str) -> dict:
        with db.tx() as c:
            rows = [dict(r) for r in c.execute("SELECT id, note, created_at, length(text) chars FROM script_versions "
                                               "WHERE project_id=? ORDER BY id DESC", (pid,))]
        return {"project": pid, "items": rows}

    def restore_script(self, pid: str, vid: int) -> dict:
        with db.tx() as c:
            r = c.execute("SELECT text FROM script_versions WHERE id=? AND project_id=?", (vid, pid)).fetchone()
        if not r:
            raise NotFound("没有这个剧本版本")
        self._snapshot_script(pid, "撤销前")
        return self.set_script(pid, r["text"])

    # ---------------- 圣经 ----------------
    def bible(self, pid: str) -> dict:
        p = self._project_row(pid)
        return db.load(p["bible_json"] or "", {}) or {}

    def set_bible(self, pid: str, bible: dict) -> dict:
        if not isinstance(bible, dict) or not bible.get("beats"):
            raise WorkflowError("故事圣经格式不对：至少要有分集节拍 beats")
        b = normalize_bible(bible, len(bible["beats"]))
        with db.tx() as c:
            c.execute("UPDATE projects SET bible_json=?, updated_at=? WHERE id=?", (db.dump(b), db.now(), pid))
        return b

    # ---------------- 分层写剧本 ----------------
    async def write_script(self, pid: str, idea: str, episodes: int = 3, seconds_per_ep: int = 60, genre: str = "",
                           auto_fix: bool = True, mode: str = "replace", bible: dict | None = None) -> dict:
        """故事圣经 → 分集节拍 → 逐集并发写 → 代码指标 + 模型审核 → C 档定向改写（≤2 轮）、D 档重写一次。"""
        p = self._project_row(pid)
        st = self.settings(pid)
        if not (idea or "").strip() and not bible:
            raise WorkflowError("先写一句灵感、故事梗概或贴一段小说")
        episodes = max(1, min(30, int(episodes)))
        seconds_per_ep = max(20, min(300, int(seconds_per_ep)))
        llm.current_project.set(pid)
        model, engine = st["text_model"], st["engine"]
        if bible:
            b, b_src = normalize_bible(bible, episodes), "given"
        else:
            b, b_src = await make_bible(self.agg, model, idea, episodes, genre, engine)
        scenes = max(2, min(6, round(seconds_per_ep / 25)))

        async def one(beat):
            prev = b["beats"][beat["ep"] - 2]["hook_end"] if beat["ep"] > 1 else ""
            text, src = await write_episode(self.agg, model, b, beat, seconds_per_ep, scenes, prev, engine)
            rounds, history = 0, []
            rv = await review_episode(self.agg, model, text, beat, metrics(text, seconds_per_ep, b["props"]), engine)
            history.append(rv["grade"])
            while auto_fix and rv["grade"] in ("C", "D") and rounds < 2:
                if rv["grade"] == "D" and rounds == 0:
                    text, src = await write_episode(self.agg, model, b, beat, seconds_per_ep, scenes, prev, engine)
                else:
                    text, _ = await revise_episode(self.agg, model, text, rv, b, beat, engine)
                rounds += 1
                rv = await review_episode(self.agg, model, text, beat, metrics(text, seconds_per_ep, b["props"]), engine)
                history.append(rv["grade"])
            rv["history"] = history
            return beat["ep"], text, src, rv

        results = await self.gather_limited(one(bt) for bt in b["beats"])
        texts, reviews, sources = [], {}, set()
        for r in sorted([r for r in results if not isinstance(r, Exception)], key=lambda x: x[0]):
            n, text, src, rv = r
            texts.append(text.strip())
            reviews[str(n)] = rv
            sources.add(src)
        errs = [str(r) for r in results if isinstance(r, Exception)]
        if not texts:
            raise WorkflowError("剧本生成失败：" + "；".join(errs[:2]))
        script = "\n\n".join(texts)
        self._snapshot_script(pid, "写剧本前")
        if mode == "append" and p["script_text"].strip():
            script = p["script_text"].rstrip() + "\n\n" + script
        with db.tx() as c:
            c.execute("UPDATE projects SET bible_json=?, script_review_json=? WHERE id=?",
                      (db.dump(b), db.dump(reviews), pid))
        out = self.set_script(pid, script)
        grades = "".join(reviews[k]["grade"] for k in sorted(reviews, key=int))
        source = "real" if sources == {"real"} and b_src != "mock" else ("mock" if sources == {"mock"} else "mixed")
        self.notify(pid, "success" if set(grades) <= {"A", "B"} else "warning",
                    f"剧本完成：{len(texts)} 集，评级 {grades}", f"来源：{source}；卡点集：{paywall_episodes(episodes)}")
        out.update(generated_source=source, bible=b, reviews=reviews, errors=errs)
        return out

    # ---------------- 体检 / 改写 现有剧本 ----------------
    def _episode_blocks(self, pid: str) -> list[tuple[str, str]]:
        p = self._project_row(pid)
        if not p["script_text"].strip():
            raise WorkflowError("剧本是空的")
        return self.split_episodes(p["script_text"])

    async def review_script(self, pid: str, episode_no: int | None = None, seconds_per_ep: int | None = None) -> dict:
        st = self.settings(pid)
        b = self.bible(pid)
        blocks = self._episode_blocks(pid)
        target = int(seconds_per_ep or 0) or 60
        beats = {x["ep"]: x for x in b.get("beats", [])}
        llm.current_project.set(pid)
        todo = [(n, title, body) for n, (title, body) in enumerate(blocks, start=1) if episode_no in (None, n)]
        if not todo:
            raise NotFound(f"第 {episode_no} 集不存在")
        outs = await self.gather_limited(
            review_episode(self.agg, st["text_model"], f"{title}\n{body}", beats.get(n),
                           metrics(body, target, b.get("props")), st["engine"]) for n, title, body in todo)
        p = self._project_row(pid)
        reviews = db.load(p["script_review_json"] or "", {}) or {}
        for (n, _, _), rv in zip(todo, outs):
            if isinstance(rv, Exception):
                raise rv
            reviews[str(n)] = rv
        with db.tx() as c:
            c.execute("UPDATE projects SET script_review_json=? WHERE id=?", (db.dump(reviews), pid))
        return {"project": pid, "reviews": {str(n): reviews[str(n)] for n, _, _ in todo},
                "grades": {str(n): reviews[str(n)]["grade"] for n, _, _ in todo}}

    async def revise_script(self, pid: str, episode_no: int) -> dict:
        st = self.settings(pid)
        b = self.bible(pid)
        blocks = self._episode_blocks(pid)
        if not 1 <= episode_no <= len(blocks):
            raise NotFound(f"第 {episode_no} 集不存在")
        p = self._project_row(pid)
        reviews = db.load(p["script_review_json"] or "", {}) or {}
        title, body = blocks[episode_no - 1]
        rv = reviews.get(str(episode_no))
        if not rv or rv.get("text_hash") != short_hash(f"{title}\n{body}"):   # 剧本改过（或撤销过）就重新体检
            rv = (await self.review_script(pid, episode_no))["reviews"][str(episode_no)]
        beat = next((x for x in b.get("beats", []) if x["ep"] == episode_no), None)
        llm.current_project.set(pid)
        new, source = await revise_episode(self.agg, st["text_model"], f"{title}\n{body}", rv, b or None, beat, st["engine"])
        if source == "none":
            return {"project": pid, "episode": episode_no, "changed": False, "review": rv}
        self._snapshot_script(pid, f"改写第{episode_no}集前")
        new_blocks = [f"{t}\n{bd}" for t, bd in blocks]
        new_blocks[episode_no - 1] = new.strip()
        self.set_script(pid, "\n\n".join(new_blocks))
        target = int(rv.get("metrics", {}).get("target_seconds") or 60)
        after = (await self.review_script(pid, episode_no, target))["reviews"][str(episode_no)]
        return {"project": pid, "episode": episode_no, "changed": True, "source": source,
                "before": rv["grade"], "after": after["grade"], "review": after}
