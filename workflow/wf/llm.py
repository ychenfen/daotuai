"""AI 环节：一键剧本 / 场次切分 / 资产抽取 / 分镜 / 提示词 / 出图提示词 / 润色 / 对话。
真实 LLM（走聚合层）+ 离线确定性 mock。

WORKFLOW_LLM=auto（默认）：先调模型，失败或输出不合法就退回本地规则；
=real：只用模型（失败即报错）；=mock：只用本地规则（零成本、可测试）。
各接口也可以显式传 engine="local" 只用本地规则（对应刀徒的"本地解析/本地生成"）。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

import contextvars
import logging
from typing import Callable

from .agg import Agg, AggError

log = logging.getLogger("workflow.llm")

# 当前在为哪个项目调用模型：文本花费记到这个项目上（Workflow 在各入口设置；并发子任务自动继承）
current_project: contextvars.ContextVar[str] = contextvars.ContextVar("current_project", default="")
charge_hook: Callable[[str, float], None] | None = None


def record_charge(res: dict) -> None:
    rmb = float(((res or {}).get("billing") or {}).get("charged_rmb") or 0)
    pid = current_project.get()
    if rmb and pid and charge_hook:
        try:
            charge_hook(pid, rmb)
        except Exception:  # 记账失败不影响主流程
            log.exception("记录文本花费失败")

SIZES = ["史诗大远景", "全景", "中景", "近景", "特写"]
ANGLES = ["俯拍", "平视", "仰拍", "低机位", "过肩"]
MOVES = ["缓推", "固定机位", "横移跟随", "手持跟拍", "缓拉", "升降"]
POSITIONS = ["画面中央", "画面左侧", "画面右侧", "后景", "前景"]
NOT_NAMES = {"旁白", "画外音", "字幕", "场景", "镜头", "时间", "地点", "人物", "内景", "外景", "备注",
             "OS", "VO", "音效", "转场", "注", "同期", "众人", "所有人", "全体", "画面", "表演", "台词"}
NEGATIVE = "画面模糊、人物变形、五官崩坏、多余肢体、多手多脚、文字水印、字幕、低清晰度、比例错误、闪烁、背景音乐"
SUFFIX = "【视频包含音效，但不能有任何背景音乐（BGM）、不可以有字幕】"


def mode() -> str:
    return os.environ.get("WORKFLOW_LLM", "auto")


def use_model(engine: str | None) -> bool:
    if engine == "local":
        return False
    if engine == "ai":
        return mode() != "mock"
    return mode() != "mock"


def _strict(engine: str | None) -> bool:
    return mode() == "real" or engine == "ai" and mode() == "real"


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, type(default)(v)))
    except (TypeError, ValueError):
        return default


async def ask(agg: Agg, model: str, system: str, user: str, max_tokens: int, temperature: float = 0.2) -> str:
    res = await agg.chat(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                         max_tokens=max_tokens, temperature=temperature)
    record_charge(res)
    return res["choices"][0]["message"]["content"]


async def _ask_json(agg: Agg, model: str, system: str, user: str, max_tokens: int) -> dict | None:
    return _extract_json(await ask(agg, model, system, user, max_tokens))


# ======================================================================
# 剧本：一键剧本 / 场次切分
# ======================================================================

SCRIPT_SYSTEM = """你是短剧编剧。根据用户给的小说、故事或一句灵感，写分集短剧剧本。严格使用以下格式，不要任何解释：
第1集
1-1 日 外 地点
人物：甲、乙
△动作描写（镜头感强，一句一个画面）
甲（情绪/动作）：台词
乙：台词
…
第2集
2-1 夜 内 地点
…
要求：每集 %d 场左右、每集约 %d 秒；开头 3 秒有钩子，结尾留悬念；对白口语化；不要写镜头号。"""


def mock_script(idea: str, episodes: int, style: str) -> str:
    idea = (idea or "").strip() or "一个普通人意外获得改变命运的机会"
    hero, rival = "林舟", "沈岚"
    places = ["城郊旧仓库", "老街茶馆", "江边天台", "公司会议室", "雨夜车站"]
    out = []
    for n in range(1, max(1, episodes) + 1):
        p1, p2 = places[(n - 1) % len(places)], places[n % len(places)]
        out.append(f"第{n}集\n{n}-1 夜 内 {p1}\n人物：{hero}、{rival}\n"
                   f"△{style}质感。{idea}——第{n}集，{hero}推门而入，灯光忽明忽暗。\n"
                   f"{hero}（压低声音）：今晚必须有个结果。\n"
                   f"△{rival}背对着他，手里攥着一张旧照片。\n"
                   f"{rival}（冷笑）：你以为你还有选择？\n"
                   f"{n}-2 日 外 {p2}\n人物：{hero}\n"
                   f"△{hero}站在风里，回头望向远处<风声>。\n"
                   f"{hero}：这一次，我不会再退。\n")
    return "\n".join(out)


async def generate_script(agg: Agg, model: str, idea: str, episodes: int, style: str,
                          scenes_per_ep: int = 3, seconds_per_ep: int = 90, engine: str | None = None) -> tuple[str, str]:
    if use_model(engine):
        try:
            text = await ask(agg, model, SCRIPT_SYSTEM % (scenes_per_ep, seconds_per_ep),
                             f"画风：{style}\n集数：{episodes}\n素材：\n{idea[:12000]}", 8000, 0.7)
            if re.search(r"第\s*1\s*集", text or ""):
                return text.strip(), "real"
        except (AggError, KeyError, TypeError) as e:
            log.warning("模型调用失败，退回本地规则：%s", str(e)[:300])
            if _strict(engine):
                raise
    return mock_script(idea, episodes, style), "mock"


SPEAKER_RE = re.compile(r"^\s*([一-龥A-Za-z·]{1,8})\s*(?:[（(]([^）)]{0,40})[）)])?\s*[：:]")
SCENE_HEAD_RE = re.compile(
    r"^\s*(?:场景\s*[：:]\s*(?P<a>.+)|(?:样片\s*\d+\s*\S*\s*)?(?P<code>\d+\s*[-－]\s*\d+|\d+、)\s*"
    r"(?P<time>[日夜晨昏]|清晨|黄昏|傍晚|深夜)?\s*(?P<io>[内外]景?)?\s*(?P<b>\S.*))$")


def scene_heading(line: str) -> dict | None:
    m = SCENE_HEAD_RE.match(line)
    if not m:
        return None
    loc = (m.group("a") or m.group("b") or "").strip(" 　：:")
    loc = re.split(r"\s+", loc)[0] if loc else ""
    if not loc or len(loc) > 24:
        return None
    return {"location": loc, "time_of_day": (m.group("time") or "") if m.groupdict().get("time") else "",
            "heading": line.strip()}


def split_scenes(episode_no: int, text: str) -> list[dict]:
    """按场次标题切分一集。没有标题时整集算一场。"""
    scenes: list[dict] = []
    cur = None
    for ln in text.splitlines():
        head = scene_heading(ln)
        if head:
            cur = {**head, "lines": []}
            scenes.append(cur)
            continue
        if cur is None:
            cur = {"location": "", "time_of_day": "", "heading": "", "lines": []}
            scenes.append(cur)
        cur["lines"].append(ln)
    out = []
    for s in scenes:
        body = "\n".join(s.pop("lines")).strip()
        if not body and not s["heading"]:
            continue
        s["text"] = body
        s["code"] = f"{episode_no}-{len(out) + 1}"
        out.append(s)
    return out or [{"location": "", "time_of_day": "", "heading": "", "text": text.strip(), "code": f"{episode_no}-1"}]


def scene_characters(text: str, names: list[str]) -> list[str]:
    found = set()
    for ln in text.splitlines():
        m = re.match(r"^\s*人物\s*[：:]\s*(.+)$", ln)
        if m:
            found |= {n.strip() for n in re.split(r"[、，,\s]+", m.group(1)) if n.strip()}
    return [n for n in names if n in found or n in text]


# ======================================================================
# 资产抽取：角色(含形态) / 场景 / 道具 / 生物
# ======================================================================

ASSETS_SYSTEM = """你是短剧美术统筹。从剧本里抽取需要保持一致的资产，只输出 JSON，不要解释：
{"characters":[{"name":"角色名","aliases":["别称"],"description":"外貌、年龄、服装、标志性特征，80 字以内",
  "forms":[{"name":"形态名（如 蛋、战斗、重伤、黑化；没有就不写）","description":"该形态的外观差异"}]}],
 "creatures":[{"name":"非人生物名","description":"外观"}],
 "scenes":[{"name":"场景名","description":"空间、时间、光线、氛围，60 字以内"}],
 "props":[{"name":"道具名","description":"外观，40 字以内"}]}
要求：只列剧本里真实出现的；同一人物的不同叫法合并为一个并写进 aliases；角色外观有明显变化时作为 forms 列出。"""


def mock_assets(script: str, style: str) -> dict:
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    speakers = Counter()
    forms: dict[str, set[str]] = {}
    for ln in lines:
        m = SPEAKER_RE.match(ln)
        if m and m.group(1) not in NOT_NAMES and not m.group(1).startswith("第"):
            speakers[m.group(1)] += 1
    for ln in lines:
        m = re.match(r"^\s*人物\s*[：:]\s*(.+)$", ln)
        if m:
            for name in re.split(r"[、，,\s]+", m.group(1)):
                name = name.strip()
                if 1 <= len(name) <= 8 and name not in NOT_NAMES:
                    speakers[name] += 1
    # 「孔宣（蛋形态）」：既说明有这个角色，也说明有这个形态
    for ln in lines:
        for name, fm in re.findall(r"([一-龥]{2,6})[（(]([^）)]{1,6})形态[）)]", ln):
            known = next((n for n in speakers if name.endswith(n)), None)
            if known:
                name = known
            elif len(name) > 3:
                continue  # 前面粘着别的字，认不准是谁，交给模型抽取
            if name not in NOT_NAMES:
                speakers[name] += 0
                forms.setdefault(name, set()).add(fm)
    chars = []
    for name, _ in speakers.most_common(30):
        first = next((ln for ln in lines if name in ln and not SPEAKER_RE.match(ln)), "")
        hint = re.sub(r"^[△▲\-\s]+", "", first)[:40]
        chars.append({"name": name, "aliases": [],
                      "description": f"{style}风格角色{name}" + (f"，出场：{hint}" if hint else ""),
                      "forms": [{"name": f, "description": f"{name}的{f}形态"} for f in sorted(forms.get(name, []))]})
    scenes, seen = [], set()
    for ln in lines:
        h = scene_heading(ln)
        if h and h["location"] not in seen:
            seen.add(h["location"])
            scenes.append({"name": h["location"], "description": f"{style}风格场景：{h['heading']}"})
    return {"characters": chars, "creatures": [], "scenes": scenes, "props": []}


def _norm_assets(data: dict) -> dict:
    out: dict = {"characters": [], "creatures": [], "scenes": [], "props": []}
    for key in out:
        seen = set()
        for a in (data or {}).get(key) or []:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").strip()[:20]
            if not name or name in seen:
                continue
            seen.add(name)
            aliases = [str(x).strip()[:20] for x in (a.get("aliases") or []) if str(x).strip()]
            fms = []
            for f in a.get("forms") or []:
                fn = str((f or {}).get("name") if isinstance(f, dict) else f or "").strip()[:12]
                fn = fn.removesuffix("形态") or fn
                if fn and fn != "默认" and fn not in [x["name"] for x in fms]:
                    fms.append({"name": fn, "description": str((f or {}).get("description") or "")[:200]
                                if isinstance(f, dict) else ""})
            out[key].append({"name": name, "aliases": aliases[:6], "forms": fms[:8],
                             "description": str(a.get("description") or "")[:400]})
    return out


async def extract_assets(agg: Agg, script: str, style: str, model: str, engine: str | None = None) -> tuple[dict, str]:
    if use_model(engine):
        try:
            data = await _ask_json(agg, model, ASSETS_SYSTEM, f"画风：{style}\n\n剧本：\n{script[:16000]}", 4000)
            got = _norm_assets(data or {})
            if got["characters"] or got["scenes"]:
                return got, "real"
        except (AggError, KeyError, TypeError) as e:
            log.warning("模型调用失败，退回本地规则：%s", str(e)[:300])
            if _strict(engine):
                raise
    return mock_assets(script, style), "mock"


POLISH_SYSTEM = """你是影视美术设定师。把用户给的资产描述扩写成可直接用于 AI 出图的视觉描述：
外形、年龄、脸型五官、发型发色、服装材质与配色、标志性道具、气质；场景则写空间结构、材质、光线、色调、氛围。
只输出描述正文，150 字以内，不要标题和解释。"""


async def polish(agg: Agg, model: str, kind: str, name: str, description: str, style: str,
                 engine: str | None = None) -> tuple[str, str]:
    if use_model(engine):
        try:
            text = (await ask(agg, model, POLISH_SYSTEM, f"画风：{style}\n类型：{kind}\n名称：{name}\n现有描述：{description}",
                              600, 0.5)).strip()
            if len(text) >= 10:
                return text[:600], "real"
        except (AggError, KeyError, TypeError) as e:
            log.warning("模型调用失败，退回本地规则：%s", str(e)[:300])
            if _strict(engine):
                raise
    extra = {"character": "五官立体、服装层次清晰、配色统一、气质鲜明",
             "creature": "体型轮廓清晰、皮毛/鳞甲质感细腻、动态感强",
             "scene": "空间纵深明确、主光方向清楚、色调统一、氛围浓郁",
             "prop": "材质细节清晰、造型辨识度高"}.get(kind, "细节清晰")
    base = description.strip() or f"{style}风格的{name}"
    return (base if extra in base else f"{base}；{extra}")[:600], "mock"


# ======================================================================
# 分镜（每镜 = 一个视频生成单元，内部按时间段切画面）
# ======================================================================

STORYBOARD_SYSTEM = """你是短剧分镜师。把一集剧本按场次拆成"视频镜头卡"。每张卡是一次视频生成（%d~%d 秒），
卡内按时间段写画面。只输出 JSON，不要解释：
{"shots":[{"scene_code":"1-1","duration":15,"size":"景别（可多个，用顿号）","angle":"角度","movement":"运镜",
"characters":["本镜出场角色（用资产表里的名字；有形态时写成 名字（形态））"],
"segments":[{"start":0,"end":3,"picture":"这一段的画面，含 <音效>"}],
"performance":"表演要点：人物情绪、动作节奏、对手戏关系","blocking":"站位：人物在画面中的位置与朝向",
"dialogue":"角色：台词（多句换行），没有就空字符串","sfx":"音效","transition":"与下一镜的衔接"}]}
要求：
1. 时长在 %d~%d 秒之间，优先接近 %d 秒；segments 首尾相接覆盖整镜；
2. 必须保留原剧本的人物、动作、台词，不得新增情节；
3. 最多 %d 张卡；scene_code 用给定的场次编号；
4. 有台词的卡，时长不得短于台词正常念完的时间（约每秒 4 个字，另留 1 秒）；一张卡装不下的台词拆到下一张卡；
5. 每个时间段不短于 2 秒、只写一个看得见的主动作（写清起始姿态→动作→结束姿态）；5 秒的卡最多 2 段，
   一张卡最多两人对话。动作多就拆成多张卡——视频模型在一张卡里只能可靠地演出一两个动作。"""


SPEECH_CPS = 4.5   # 短剧台词正常语速：每秒约 4~5 个字


def speech_seconds(dialogue: str) -> float:
    """一张卡的台词按正常语速念完要几秒（含句间停顿和首尾留白）。没有台词返回 0。
    镜头时长不能短于它，否则配音只能加速到失真、视频模型自带的声音也会被截断。"""
    total, lines = 0.0, 0
    for ln in (dialogue or "").splitlines():
        ln = ln.strip().strip("{}").strip()
        m = re.match(r"^([^：:「“\"]{1,16})[：:]\s*(.+)$", ln)
        text = m.group(2) if m else ln
        text = re.sub(r"[（(][^）)]*[）)]", "", text)
        n = len(re.sub(r"[\W_]", "", text))
        if n:
            total += n / SPEECH_CPS + 0.35
            lines += 1
    return round(total + 1.0, 1) if lines else 0.0


_SEG_RE = re.compile(r"【(\d+(?:\.\d+)?)\s*[-~～—]\s*(\d+(?:\.\d+)?)\s*秒】")


def segments(picture: str) -> list[tuple[float, float, str]]:
    """画面时间轴 → [(起, 止, 这一段的文字)]。"""
    ms = list(_SEG_RE.finditer(picture or ""))
    out = []
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(picture)
        out.append((float(m.group(1)), float(m.group(2)), picture[m.end():end].strip()))
    return out


def shot_density(shot: dict) -> list[str]:
    """一张卡是否塞得太满（实测：5 秒里写 3 个动作，模型只会演出 1 个）。返回问题列表，空 = 正常。"""
    dur = float(shot.get("duration_s") or shot.get("duration") or 0) or 5.0
    segs = segments(shot.get("picture") or "")
    out = []
    short = [f"{a:g}-{b:g}秒" for a, b, _ in segs if b - a < 1.5]
    if short:
        out.append(f"时间段太短（{'、'.join(short[:3])}），动作来不及演")
    if len(segs) > max(2, int(dur / 2.0)):
        out.append(f"动作过密：{dur:g} 秒里写了 {len(segs)} 段")
    need = speech_seconds(shot.get("dialogue") or "")
    if need > dur + 0.5:
        out.append(f"台词约需 {need:g} 秒，镜头只有 {dur:g} 秒")
    speakers = {re.sub(r"[（(].*$", "", m.group(1)) for m in re.finditer(r"^\s*([^：:\n]{1,12})[：:]", shot.get("dialogue") or "", re.M)}
    speakers -= {"旁白", "画外音", "OS", "VO"}
    if len(speakers) >= 3:
        out.append(f"{len(speakers)} 人对话挤在一张卡里")
    return out


def fit_duration(duration: float, dialogue: str, lo: float, hi: float) -> float:
    """分镜给的时长 → 至少装得下台词，并落在模型支持的范围内。"""
    need = speech_seconds(dialogue)
    d = max(float(duration or 0), float(-(-need // 1)) if need else 0.0)
    return float(max(lo, min(hi, d or lo)))


def _segment_seconds(body: str, is_dialogue: bool) -> int:
    n = len(body)
    return _clamp(round(n / 5) + 1 if is_dialogue else round(n / 12) + 2, 2, 6, 3)


def mock_storyboard(scenes: list[dict], style: str, char_names: list[str], dur_min: int, dur_max: int,
                    target: int, max_shots: int) -> list[dict]:
    shots: list[dict] = []

    def close(cur, scene):
        if not cur["segs"]:
            return
        total = sum(s[1] for s in cur["segs"])
        scale = 1.0
        if total > dur_max:
            scale = dur_max / total
        t, picture = 0.0, []
        for text, sec in cur["segs"]:
            d = sec * scale
            picture.append(f"【{t:g}-{t + d:g}秒】{text}")
            t = round(t + d, 2)
        dur = _clamp(round(t), dur_min, dur_max, target)
        chars = [n for n in char_names if any(n in x[0] for x in cur["segs"]) or n in cur["speakers"]]
        i = len(shots)
        shots.append({
            "scene_code": scene["code"], "duration": dur,
            "size": "、".join([SIZES[i % len(SIZES)], SIZES[(i + 2) % len(SIZES)]]),
            "angle": ANGLES[i % len(ANGLES)], "movement": MOVES[i % len(MOVES)],
            "characters": chars, "picture": "".join(picture),
            "performance": "；".join(cur["perf"])[:400],
            "blocking": "，".join(f"{n}在{POSITIONS[k % len(POSITIONS)]}" for k, n in enumerate(chars)),
            "dialogue": "\n".join(cur["lines"])[:600], "sfx": "、".join(dict.fromkeys(cur["sfx"]))[:200],
            "transition": "硬切", "original_text": "\n".join(cur["raw"])[:2000],
        })

    for scene in scenes:
        cur = {"segs": [], "speakers": [], "lines": [], "perf": [], "sfx": [], "raw": []}
        for ln in [x.strip() for x in scene["text"].splitlines() if x.strip()]:
            if re.match(r"^\s*人物\s*[：:]", ln):
                continue
            body = re.sub(r"^[△▲\-\s]+", "", ln)
            sfx = re.findall(r"[<＜]([^>＞]{1,20})[>＞]", body)
            sm = SPEAKER_RE.match(body)
            if sm and sm.group(1) in ("旁白", "画外音", "OS", "VO"):   # 旁白进台词（配音用），不进人物
                words = body[sm.end():].strip()
                cur["lines"].append(f"旁白：{words}")
                seg, sec = f"（旁白：“{words}”）", _segment_seconds(words, True)
            elif sm and sm.group(1) not in NOT_NAMES:
                who, how = sm.group(1), (sm.group(2) or "").strip()
                words = body[sm.end():].strip()
                seg = f"{who}{'（' + how + '）' if how else ''}说：“{words}”"
                cur["speakers"].append(who)
                cur["lines"].append(f"{who}：{words}")
                if how:
                    cur["perf"].append(f"{who}{how}")
                sec = _segment_seconds(words, True)
            else:
                seg, sec = body, _segment_seconds(body, False)
            cur["segs"].append((seg, sec))
            cur["sfx"] += sfx
            cur["raw"].append(ln)
            if sum(s[1] for s in cur["segs"]) >= target:
                close(cur, scene)
                cur = {"segs": [], "speakers": [], "lines": [], "perf": [], "sfx": [], "raw": []}
        close(cur, scene)
    return shots[:max_shots]


def _normalize_shots(raw: list, max_shots: int, char_names: list[str], dur_min: int, dur_max: int,
                     codes: list[str]) -> list[dict]:
    out = []
    for s in raw[:max_shots]:
        if not isinstance(s, dict):
            continue
        segs = [x for x in (s.get("segments") or []) if isinstance(x, dict) and x.get("picture")]
        if segs:
            picture = "".join(f"【{_clamp(x.get('start'), 0, 60, 0.0):g}-{_clamp(x.get('end'), 0, 60, 0.0):g}秒】"
                              f"{str(x['picture']).strip()}" for x in segs)
        else:
            picture = str(s.get("picture") or "")
        chars = [str(x).strip() for x in (s.get("characters") or []) if str(x).strip()]
        base = [re.sub(r"[（(].*$", "", c) for c in chars]
        chars = [c for c, b in zip(chars, base) if not char_names or b in char_names]
        code = str(s.get("scene_code") or "")
        out.append({
            "scene_code": code if code in codes else (codes[0] if codes else ""),
            "duration": _clamp(s.get("duration"), dur_min, dur_max, float(dur_max)),
            "size": str(s.get("size") or "")[:40], "angle": str(s.get("angle") or "")[:40],
            "movement": str(s.get("movement") or "")[:60], "characters": chars[:12],
            "picture": picture[:2000], "performance": str(s.get("performance") or "")[:600],
            "blocking": str(s.get("blocking") or "")[:400], "dialogue": str(s.get("dialogue") or "")[:800],
            "sfx": str(s.get("sfx") or "")[:300], "transition": str(s.get("transition") or "")[:100],
            "original_text": "",
        })
    return out


async def storyboard(agg: Agg, scenes: list[dict], style: str, model: str, max_shots: int,
                     char_names: list[str], dur_min: int, dur_max: int, target: int,
                     engine: str | None = None) -> tuple[list[dict], str]:
    if use_model(engine):
        try:
            body = "\n\n".join(f"【场次 {s['code']}】{s['heading']}\n{s['text']}" for s in scenes)
            data = await _ask_json(
                agg, model, STORYBOARD_SYSTEM % (dur_min, dur_max, dur_min, dur_max, target, max_shots),
                f"画风：{style}\n角色表：{json.dumps(char_names, ensure_ascii=False)}\n\n剧本：\n{body[:9000]}", 6000)
            shots = _normalize_shots((data or {}).get("shots") or [], max_shots, char_names, dur_min, dur_max,
                                     [s["code"] for s in scenes])
            if shots:
                return shots, "real"
        except (AggError, KeyError, TypeError) as e:
            log.warning("模型调用失败，退回本地规则：%s", str(e)[:300])
            if _strict(engine):
                raise
    return mock_storyboard(scenes, style, char_names, dur_min, dur_max, target, max_shots), "mock"


# ======================================================================
# 视频提示词（刀徒格式：参考前缀 + 【镜头】时间轴 + 音效约束）
# ======================================================================

PROMPT_SYSTEM = """你是 AI 视频提示词工程师，目标模型是 %s。把分镜卡转成可直接提交的视频提示词，只输出 JSON：
{"items":[{"positive":"...","negative":"..."}]}（顺序与输入一致）
positive 必须按这个结构写：
第一行：参考资产：@角色（形态）、@角色……；整体环境参考@场景；角色声线参考@音色（有才写）
然后：【镜头1
景别：…
角度：…
运镜：…
画面：【0-3秒】…【3-6秒】…（逐段写清动作、表情、镜头运动，音效用<尖括号>）
表演：…
台词/旁白/音效：角色：{台词} <音效>
转场：…】
最后一行：""" + SUFFIX + """
要求：提到角色、场景必须写成「@名字」或「@名字（形态）」（取自资产表），系统据此挂参考图保证一致；不写"参考图"字样；
negative 写画面质量问题。"""


def _refs_line(shot: dict, voices: dict[str, str]) -> str:
    chars = shot.get("characters") or []
    parts = []
    if chars:
        parts.append("参考资产：" + "、".join(f"@{c}" for c in chars))
    if shot.get("scene_asset"):
        parts.append(f"整体环境参考@{shot['scene_asset']}")
    vs = [f"{re.sub(r'[（(].*$', '', c)}声线参考@{voices[re.sub(r'[（(].*$', '', c)]}"
          for c in chars if re.sub(r"[（(].*$", "", c) in voices]
    if vs:
        parts.append("，".join(dict.fromkeys(vs)))
    return "；".join(parts)


def _dialogue_braced(dialogue: str) -> str:
    out = []
    for ln in (dialogue or "").splitlines():
        m = re.match(r"^\s*([^：:]{1,10})[：:]\s*(.+)$", ln)
        out.append(f"{m.group(1)}：{{{m.group(2).strip()}}}" if m else ln.strip())
    return " ".join(x for x in out if x)


def mock_prompt(shot: dict, style: str, aspect: str, voices: dict[str, str] | None = None,
                fmt: str = "text", model: str = "") -> dict:
    voices = voices or {}
    dur = float(shot.get("duration_s") or shot.get("duration") or 5)
    picture = shot.get("picture") or shot.get("scene") or ""
    if "【" not in picture:
        picture = f"【0-{dur:g}秒】{picture}"
    sfx = "".join(f"<{x}>" for x in re.split(r"[、,，\s]+", shot.get("sfx") or "") if x)
    if fmt == "json":
        obj = {"镜头编号": shot.get("code") or str(shot.get("shot_no", "")), "目标视频模型": model,
               "提示词风格": style, "画幅": aspect, "动态前缀": _refs_line(shot, voices),
               "镜头列表": [{"镜头序号": "镜头1", "时间": f"0-{dur:g}秒", "景别": shot.get("size") or "",
                          "角度": shot.get("angle") or "", "运镜": shot.get("movement") or "",
                          "画面": picture, "表演": shot.get("performance") or "",
                          "台词/旁白/音效": (_dialogue_braced(shot.get("dialogue")) + " " + sfx).strip(),
                          "转场": shot.get("transition") or "硬切"}],
               "约束": SUFFIX}
        return {"positive": json.dumps(obj, ensure_ascii=False, indent=1), "negative": NEGATIVE}
    lines = []
    refs = _refs_line(shot, voices)
    if refs:
        lines.append(refs)
    lines += [f"{style}风格，{aspect} 画幅，电影级质感。", "【镜头1",
              f"景别：{shot.get('size') or '中景'}", f"角度：{shot.get('angle') or '平视'}",
              f"运镜：{shot.get('movement') or '固定机位'}", f"画面：{picture}"]
    if shot.get("performance"):
        lines.append(f"表演：{shot['performance']}")
    if shot.get("blocking"):
        lines.append(f"站位：{shot['blocking']}")
    talk = (_dialogue_braced(shot.get("dialogue")) + " " + sfx).strip()
    if talk:
        lines.append(f"台词/旁白/音效：{talk}")
    lines.append(f"转场：{shot.get('transition') or '硬切'}】")
    lines.append(SUFFIX)
    return {"positive": "\n".join(lines)[:3000], "negative": NEGATIVE}


async def prompts(agg: Agg, shots: list[dict], style: str, model: str, aspect: str, asset_names: list[str],
                  voices: dict[str, str], fmt: str = "text", video_model: str = "",
                  engine: str | None = None) -> tuple[list[dict], str]:
    if use_model(engine) and shots and fmt == "text":
        payload = [{"duration": s.get("duration_s"), "scene": s.get("scene_asset") or s.get("scene"),
                    "characters": s.get("characters") or [], "size": s.get("size"), "angle": s.get("angle"),
                    "movement": s.get("movement"), "picture": s.get("picture"), "performance": s.get("performance"),
                    "blocking": s.get("blocking"), "dialogue": s.get("dialogue"), "sfx": s.get("sfx"),
                    "transition": s.get("transition")} for s in shots]
        try:
            data = await _ask_json(
                agg, model, PROMPT_SYSTEM % (video_model or "通用视频模型"),
                f"画风：{style}；画幅：{aspect}\n资产表：{json.dumps(asset_names, ensure_ascii=False)}\n"
                f"声线：{json.dumps(voices, ensure_ascii=False)}\n分镜卡 JSON：\n{json.dumps(payload, ensure_ascii=False)}",
                6000)
            items = (data or {}).get("items") or []
            if items:
                out = []
                for i, s in enumerate(shots):
                    p = items[i] if i < len(items) and isinstance(items[i], dict) else None
                    if p and p.get("positive"):
                        pos = str(p["positive"])[:3000]
                        if SUFFIX not in pos:
                            pos += "\n" + SUFFIX
                        out.append({"positive": pos, "negative": str(p.get("negative") or NEGATIVE)[:400]})
                    else:
                        out.append(mock_prompt(s, style, aspect, voices, fmt, video_model))
                return out, "real"
        except (AggError, KeyError, TypeError) as e:
            log.warning("模型调用失败，退回本地规则：%s", str(e)[:300])
            if _strict(engine):
                raise
    return [mock_prompt(s, style, aspect, voices, fmt, video_model) for s in shots], "mock"


# ======================================================================
# 出图提示词（确定性，不调模型）
# ======================================================================

def asset_image_prompt(kind: str, name: str, description: str, style: str, form: str = "",
                       form_desc: str = "") -> str:
    who = f"{name}（{form}形态）" if form and form != "默认" else name
    desc = "；".join(x for x in (description, form_desc) if x)
    if kind in ("character", "creature"):
        return (f"生成{style}风格、影视级高精度角色设定图：{who}。{desc}。画面左侧为面部特写，右侧为全身正面、侧面、背面三视图，"
                f"纯色浅灰背景，五官清晰，服装与配饰细节完整，比例统一，无文字无水印。")
    if kind == "scene":
        return f"{style}风格影视场景概念图：{who}。{desc}。无人物，空间纵深清楚，主光方向明确，构图完整，无文字无水印。"
    return f"{style}风格道具设定图：{who}。{desc}。多角度展示，纯色背景，材质细节清晰，无文字无水印。"


def edit_image_prompt(kind: str, name: str, instruction: str, style: str) -> str:
    return f"在参考图基础上修改{name}：{instruction}。保持{style}风格、人物身份与其余细节不变，无文字无水印。"


def blocking_prompt(scene: dict, chars: list[str], style: str, aspect: str) -> str:
    who = "、".join(chars) if chars else "无人物"
    note = scene.get("blocking_note") or ""
    return (f"{style}风格场景站位图，{aspect}，场景：{scene.get('location') or scene.get('heading')}，"
            f"{scene.get('time_of_day') or ''}。人物到场后的位置关系：{who}。{note}"
            f"全景机位，人物全身可见、互不遮挡、比例正确，用于后续镜头的空间参考；无文字无水印。")


def cover_prompt(project: str, episode_title: str, text: str, style: str, cast: list[str]) -> str:
    hook = re.sub(r"\s+", " ", re.sub(r"[△▲]", "", text))[:80]
    who = "、".join(cast) if cast else "主角"
    return (f"{style}风格短剧竖版海报，9:16。剧名《{project}》，{episode_title}。主角：{who}（外观严格依照参考图）。"
            f"剧情看点：{hook}。电影级光影，人物居中有张力，背景体现剧情冲突，顶部留出标题区域。"
            f"画面中以醒目艺术字写剧名「{project}」，不要其他文字和水印。")


def frame_image_prompt(shot: dict, style: str, aspect: str) -> str:
    first = re.split(r"【[^】]*】", shot.get("picture") or "")
    first = next((x for x in first if x.strip()), shot.get("scene") or "")
    parts = [f"{style}风格电影分镜画面，{aspect}", f"{shot.get('size') or '中景'}，{shot.get('angle') or '平视'}",
             first.strip()[:200]]
    if shot.get("blocking"):
        parts.append(f"站位：{shot['blocking']}")
    parts.append("无文字，无水印")
    return "；".join(p for p in parts if p)


# ======================================================================
# AI 对话智能体
# ======================================================================

AGENTS = {
    "assistant": {"name": "AI 对话助手", "system": "你是短剧制作助手，回答简洁、可执行，用中文。"},
    "script": {"name": "剧本助手", "system": "你是资深短剧编剧，擅长钩子、反转、节奏和口语化台词。结合下面的项目剧本回答。",
               "context": "script"},
    "video_prompt": {"name": "视频提示词智能体",
                     "system": "你是 AI 视频提示词工程师。按“参考资产前缀 + 【镜头】景别/角度/运镜/画面时间轴/表演/台词音效/转场”"
                               "的结构写提示词，角色和场景用 @名字 引用。", "context": "assets"},
    "text": {"name": "文本对话", "system": "你是一个有帮助的中文助手。"},
}


def mock_chat_reply(agent: str, question: str, context: str) -> str:
    head = AGENTS.get(agent, AGENTS["assistant"])["name"]
    ctx = f"（已读取项目上下文 {len(context)} 字）" if context else ""
    return f"[{head}·模拟回复]{ctx} 你问的是：{question[:200]}。当前为模拟模式，切换到真实文本模型后会给出完整回答。"


# ======================================================================
# 按质检意见改提示词（重抽用）
# ======================================================================

REVISE_SYSTEM = """你是 AI 视频提示词修改师。给你一条视频提示词，以及质检员/审片人看完成片后指出的问题。
在不改变剧情、人物、台词的前提下修改提示词，让下一次生成避开这些问题：
- 没演出来的关键动作：在对应时间段开头单独写一句，写清起始姿态→动作→结束姿态，必要时改近景别、放慢节奏；
- 多余人物：明确写"画面中只有 @甲、@乙，没有其他人"；
- 字幕水印：强调画面不出现任何文字、字幕、水印；
- 角色不一致：强调外观与 @角色 参考一致（发型、服装、配色）；
- 画面崩坏：降低动作幅度，减少同时运动的人数。
- 要求屏幕/纸面上出现的具体文字或数字：视频模型画不准，改写成看得见的画面（如“电脑屏幕上是红绿交替的交易界面”），文字留给后期贴字；
保留原有结构和所有「@名字」「@名字（形态）」写法，不删除 @；只能引用资产表里已有的名字。只输出 JSON：
{"positive":"改好的完整提示词","changes":["改了什么（一句话）"]}"""

_MENTION_RE = re.compile(r"@[^\s@，,、；;：:。！？!?{}【】<>“”\"]+")
_FIX_HEAD = "【质检修正】"


def _fix_line(issue: dict, characters: list[str]) -> str:
    t, d = str(issue.get("type") or ""), str(issue.get("detail") or "").strip()
    if t == "多余人物":
        who = "、".join("@" + c for c in characters) if characters else "分镜里写到的角色"
        return f"画面中只有 {who}，不要出现其他人物"
    if t == "字幕水印":
        return "画面中不出现任何文字、字幕、水印、标志"
    if t == "角色不一致":
        return "角色外观（发型、服装、配色、体型）严格与参考一致，全程不变"
    if t == "画面崩坏":
        return "动作幅度适中、速度放慢，人物五官和肢体保持正常"
    if not d:
        return ""
    # 质检描述多是“分镜要求 X，但成片 Y”：只把要求 X 写进提示词，别把错误画面 Y 也写进去（实测会被模型照着演）
    m = re.search(r"分镜要求[：:]?\s*(.+?)[，,；;]\s*(?:但|而|然而)", d)
    if m:
        return f"必须清楚演出：{m.group(1).strip()}"
    head = re.split(r"[，,；;]\s*(?:但|而|然而)|但成片|成片中", d)[0].strip()
    return f"必须清楚演出：{head or d}"


def mock_revise(prompt: str, issues: list[dict], characters: list[str]) -> tuple[str, list[str]]:
    """本地规则：在提示词结尾的约束行前插入（或替换）一段【质检修正】。"""
    lines = []
    for i in issues:
        ln = _fix_line(i, characters)
        if ln and ln not in lines:
            lines.append(ln)
    base = re.sub(r"\n?" + re.escape(_FIX_HEAD) + r"[^\n]*", "", prompt or "").rstrip()
    if not lines:
        return base, []
    block = _FIX_HEAD + "；".join(lines) + "。"
    if SUFFIX in base:
        head, _, tail = base.rpartition(SUFFIX)
        return f"{head.rstrip()}\n{block}\n{SUFFIX}{tail}", lines
    return f"{base}\n{block}", lines


def _mention_base(m: str) -> str:
    return re.sub(r"[（(].*$", "", m.lstrip("@"))


async def revise_prompt(agg: Agg, model: str, prompt: str, issues: list[dict], characters: list[str],
                        engine: str | None = None, known: list[str] | None = None) -> tuple[str, list[str], str]:
    """返回 (新提示词, 改动说明, 来源 real/mock)。模型改坏了 @ 引用就退回本地规则。"""
    if use_model(engine) and issues:
        try:
            user = (f"原提示词：\n{prompt}\n\n问题：\n" +
                    "\n".join(f"- [{i.get('severity', '')}{i.get('type', '')}] {i.get('detail', '')}" for i in issues))
            data = await _ask_json(agg, model, REVISE_SYSTEM, user, 2500)
            new = str((data or {}).get("positive") or "").strip()
            old_m, new_m = set(_MENTION_RE.findall(prompt)), set(_MENTION_RE.findall(new))
            allowed = set(known or []) | {_mention_base(x) for x in old_m}
            # 中文里 @名字 后面常直接接动作（“@孔宣盯着屏幕”），按“以已知名字开头”判断，和 refs.resolve 的最长匹配一致
            extra = {x for x in new_m - old_m if not any(_mention_base(x).startswith(n) for n in allowed if n)}
            lost = {x for x in old_m if not any(y.startswith(x) for y in new_m)}
            if new and not lost and not extra and len(new) > len(prompt) * 0.6:
                if SUFFIX in prompt and SUFFIX not in new:
                    new = new.rstrip() + "\n" + SUFFIX
                return new, [str(x) for x in (data.get("changes") or [])][:6], "real"
            log.warning("revise_prompt: 模型输出不合格，改用本地规则：丢了 %s，新增未知 %s，长度 %d/%d",
                        sorted(lost), sorted(extra), len(new), len(prompt))
        except AggError as e:
            if _strict(engine):
                raise
            log.warning("revise_prompt 失败，改用本地规则：%s", e)
    new, changes = mock_revise(prompt, issues, characters)
    return new, changes, "mock"
