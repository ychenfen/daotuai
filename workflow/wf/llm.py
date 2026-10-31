"""AI 环节：资产抽取 / 分镜 / 提示词 / 出图提示词。真实 LLM（走聚合层）+ 离线确定性 mock。

WORKFLOW_LLM=auto（默认）：先调模型，失败或输出不合法就退回 mock；
=real：只用模型（失败即报错）；=mock：只用本地规则（零成本、可测试）。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from .agg import Agg, AggError

SIZES = ["远景", "全景", "中景", "近景", "特写"]
ANGLES = ["平视", "俯拍", "仰拍", "低角度", "平视"]
MOVES = ["固定机位", "缓慢推镜", "缓慢拉镜", "横移跟随", "手持跟拍", "升降镜头"]
POSITIONS = ["画面中央", "画面左侧", "画面右侧", "后景", "前景"]
NOT_NAMES = {"旁白", "画外音", "字幕", "场景", "镜头", "时间", "地点", "人物", "内景", "外景", "备注",
             "OS", "VO", "音效", "转场", "注", "同期", "众人", "所有人", "全体"}
NEGATIVE = "画面模糊、人物变形、五官崩坏、多余肢体、多手多脚、文字水印、字幕、低清晰度、比例错误、闪烁"


def mode() -> str:
    return os.environ.get("WORKFLOW_LLM", "auto")


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


async def _ask_json(agg: Agg, model: str, system: str, user: str, max_tokens: int) -> dict | None:
    res = await agg.chat(model, [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                         max_tokens=max_tokens, temperature=0.2)
    return _extract_json(res["choices"][0]["message"]["content"])


# ======================================================================
# 资产抽取：角色 / 场景 / 道具
# ======================================================================

ASSETS_SYSTEM = """你是短剧美术统筹。从剧本里抽取需要保持一致的资产，只输出 JSON，不要解释：
{"characters":[{"name":"角色名","aliases":["别称"],"description":"外貌、年龄、服装、标志性特征，60 字以内"}],
 "scenes":[{"name":"场景名","description":"空间、时间、光线、氛围，60 字以内"}],
 "props":[{"name":"道具名","description":"外观，40 字以内"}]}
要求：只列剧本里真实出现的；同一人物的不同叫法合并为一个，并写进 aliases；角色形态有明显变化（如"蛋形态"）时单列。"""

SPEAKER_RE = re.compile(r"^\s*([一-龥A-Za-z·]{1,8})\s*(?:[（(][^）)]{0,20}[）)])?\s*[：:]")
SCENE_HEAD_RE = re.compile(
    r"^\s*(?:场景\s*[：:]\s*(?P<a>.+)|\d+\s*[-－.]\s*\d+\s*(?:[日夜晨昏]\s*)?(?:[内外]景?\s*)?(?P<b>.+))$")


def scene_heading(line: str) -> str | None:
    m = SCENE_HEAD_RE.match(line)
    if not m:
        return None
    name = (m.group("a") or m.group("b") or "").strip(" 　：:")
    return name[:20] or None


def mock_assets(script: str, style: str) -> dict:
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    speakers = Counter()
    for ln in lines:
        m = SPEAKER_RE.match(ln)
        if m and m.group(1) not in NOT_NAMES and not m.group(1).startswith("第"):
            speakers[m.group(1)] += 1
    # 人物表："人物：A、B、C"
    for ln in lines:
        m = re.match(r"^\s*人物\s*[：:]\s*(.+)$", ln)
        if m:
            for name in re.split(r"[、，,\s]+", m.group(1)):
                name = name.strip()
                if 1 <= len(name) <= 8 and name not in NOT_NAMES:
                    speakers[name] += 1
    chars = []
    for name, _ in speakers.most_common(20):
        first = next((ln for ln in lines if name in ln and not SPEAKER_RE.match(ln)), "")
        hint = re.sub(r"^[△▲\-\s]+", "", first)[:40]
        chars.append({"name": name, "aliases": [],
                      "description": f"{style}风格角色{name}" + (f"，出场：{hint}" if hint else "")})
    scenes, seen = [], set()
    for ln in lines:
        name = scene_heading(ln)
        if name and name not in seen:
            seen.add(name)
            scenes.append({"name": name, "description": f"{style}风格场景：{name}"})
    return {"characters": chars, "scenes": scenes, "props": []}


def _norm_assets(data: dict) -> dict:
    out = {"characters": [], "scenes": [], "props": []}
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
            out[key].append({"name": name, "aliases": aliases[:6],
                             "description": str(a.get("description") or "")[:300]})
    return out


async def extract_assets(agg: Agg, script: str, style: str, model: str) -> tuple[dict, str]:
    if mode() != "mock":
        try:
            data = await _ask_json(agg, model, ASSETS_SYSTEM, f"画风：{style}\n\n剧本：\n{script[:12000]}", 3000)
            got = _norm_assets(data or {})
            if got["characters"] or got["scenes"]:
                return got, "real"
        except (AggError, KeyError, TypeError):
            if mode() == "real":
                raise
    return mock_assets(script, style), "mock"


# ======================================================================
# 分镜
# ======================================================================

STORYBOARD_SYSTEM = """你是短剧分镜师。把一集剧本拆成镜头，只输出 JSON，不要解释：
{"shots":[{"shot_no":1,"duration":5,"scene":"场景名（用资产表里的名字）","size":"景别","angle":"角度",
"movement":"运镜","characters":["本镜出场角色（用资产表里的名字）"],
"picture":"画面内容（可直接给分镜师看的描述）","blocking":"站位：每个人物在画面中的位置与朝向",
"dialogue":"台词，没有就空字符串","sfx":"音效，没有就空字符串"}]}
要求：
1. 每个镜头 4~15 秒，按剧情节奏给时长；
2. 必须保留原剧本的人物、动作、台词，不得新增情节；
3. shot_no 从 1 连续编号；最多 %d 个镜头；
4. characters 和 scene 只能用资产表里的名字。"""


def _names_in(text: str, names: list[str]) -> list[str]:
    hits = [(text.find(n), n) for n in names if n and n in text]
    return [n for _, n in sorted(hits)]


def mock_storyboard(episode_text: str, style: str, max_shots: int,
                    char_names: list[str], scene_names: list[str]) -> list[dict]:
    current_scene = scene_names[0] if scene_names else ""
    paras = []
    for ln in [x.strip() for x in episode_text.splitlines() if x.strip()]:
        head = scene_heading(ln)
        if head:
            current_scene = head
            continue
        if len(ln) >= 4:
            paras.append((ln, current_scene))
    if not paras:
        paras = [(episode_text.strip()[:200] or "空镜头", current_scene)]
    shots = []
    for i, (p, scene) in enumerate(paras[:max_shots], start=1):
        body = re.sub(r"^[△▲\-\s]+", "", p)
        dialogue = ""
        sm = SPEAKER_RE.match(body)
        chars = _names_in(body, char_names)
        if sm:
            dialogue = body[sm.end():].strip()
            if sm.group(1) in char_names and sm.group(1) not in chars:
                chars.insert(0, sm.group(1))
        else:
            m = re.search(r"[“「\"]([^”」\"]{2,60})[”」\"]", body)
            if m:
                dialogue = m.group(1)
        sfx = " ".join(re.findall(r"[<＜]([^>＞]{1,20})[>＞]", body))
        blocking = "，".join(f"{n}在{POSITIONS[k % len(POSITIONS)]}" for k, n in enumerate(chars))
        shots.append({
            "shot_no": i,
            "duration": _clamp(round(len(body) / 10) + 3, 4, 15, 5),
            "scene": scene or body[:12],
            "size": SIZES[(i - 1) % len(SIZES)],
            "angle": ANGLES[(i - 1) % len(ANGLES)],
            "movement": MOVES[(i - 1) % len(MOVES)],
            "characters": chars,
            "picture": f"{style}风格，{body[:180]}",
            "blocking": blocking,
            "dialogue": dialogue[:200],
            "sfx": sfx,
        })
    return shots


def _normalize_shots(raw: list, max_shots: int, char_names: list[str]) -> list[dict]:
    out = []
    for s in raw[:max_shots]:
        if not isinstance(s, dict):
            continue
        chars = [str(x).strip() for x in (s.get("characters") or []) if str(x).strip()]
        out.append({
            "shot_no": len(out) + 1,
            "duration": _clamp(s.get("duration"), 4, 15, 5),
            "scene": str(s.get("scene") or "")[:60],
            "size": str(s.get("size") or "")[:20],
            "angle": str(s.get("angle") or "")[:20],
            "movement": str(s.get("movement") or "")[:40],
            "characters": [c for c in chars if not char_names or c in char_names][:8],
            "picture": str(s.get("picture") or "")[:800],
            "blocking": str(s.get("blocking") or "")[:300],
            "dialogue": str(s.get("dialogue") or "")[:400],
            "sfx": str(s.get("sfx") or "")[:200],
        })
    return out


async def storyboard(agg: Agg, episode_text: str, style: str, model: str, max_shots: int,
                     char_names: list[str], scene_names: list[str]) -> tuple[list[dict], str]:
    if mode() != "mock":
        try:
            table = json.dumps({"角色": char_names, "场景": scene_names}, ensure_ascii=False)
            data = await _ask_json(agg, model, STORYBOARD_SYSTEM % max_shots,
                                   f"画风：{style}\n资产表：{table}\n\n剧本：\n{episode_text[:6000]}", 4000)
            shots = _normalize_shots((data or {}).get("shots") or [], max_shots, char_names)
            if shots:
                return shots, "real"
        except (AggError, KeyError, TypeError):
            if mode() == "real":
                raise
    return mock_storyboard(episode_text, style, max_shots, char_names, scene_names), "mock"


# ======================================================================
# 视频提示词（用 @资产名 引用参考素材）
# ======================================================================

PROMPT_SYSTEM = """你是 AI 视频提示词工程师。把分镜转成视频模型可直接执行的提示词，只输出 JSON：
{"items":[{"shot_no":1,"positive":"...","negative":"..."}]}
要求：
1. positive 写成一个连续时间轴：景别/角度/运镜/画面/站位/台词音效，中文，300 字以内；
2. 提到角色或场景时，必须写成「@名字」（名字取自资产表），系统会据此挂参考图，保证角色一致；
3. 不要写"参考图"之类的话；
4. negative 写画面质量问题（模糊、变形、多手多脚、文字水印等）。"""


def mock_prompt(shot: dict, style: str, aspect: str) -> dict:
    end = float(shot.get("duration") or shot.get("duration_s") or 5)
    chars = shot.get("characters") or []
    lines = [f"【镜头{shot['shot_no']}（0-{end:g}秒）"]
    if shot.get("scene_asset"):
        lines.append(f"场景：@{shot['scene_asset']}")
    if chars:
        lines.append("人物：" + "、".join(f"@{c}" for c in chars))
    lines += [f"景别：{shot.get('size') or '中景'}",
              f"角度：{shot.get('angle') or '平视'}",
              f"运镜：{shot.get('movement') or '固定机位'}",
              f"画面：{shot.get('picture') or shot.get('scene') or ''}"]
    if shot.get("blocking"):
        lines.append(f"站位：{shot['blocking']}")
    if shot.get("dialogue"):
        lines.append(f"台词：{shot['dialogue']}")
    if shot.get("sfx"):
        lines.append(f"音效：{shot['sfx']}")
    lines.append("转场：硬切】")
    positive = f"{style}风格，{aspect} 画幅，电影级质感。" + "\n".join(lines)
    return {"positive": positive[:1500], "negative": NEGATIVE}


def _normalize_prompts(raw: list, shots: list[dict], style: str, aspect: str) -> list[dict]:
    """按输入顺序对齐（不同集的 shot_no 会重复，不能只靠编号匹配）。"""
    out = []
    for i, s in enumerate(shots):
        p = raw[i] if i < len(raw) and isinstance(raw[i], dict) else None
        if p and p.get("positive"):
            out.append({"positive": str(p["positive"])[:1500],
                        "negative": str(p.get("negative") or NEGATIVE)[:400]})
        else:
            out.append(mock_prompt(s, style, aspect))
    return out


async def prompts(agg: Agg, shots: list[dict], style: str, model: str, aspect: str,
                  asset_names: list[str]) -> tuple[list[dict], str]:
    if mode() != "mock" and shots:
        payload = [{"shot_no": s["shot_no"], "duration": s.get("duration_s") or s.get("duration"),
                    "scene": s.get("scene_asset") or s.get("scene"), "characters": s.get("characters") or [],
                    "size": s.get("size"), "angle": s.get("angle"), "movement": s.get("movement"),
                    "picture": s.get("picture"), "blocking": s.get("blocking"),
                    "dialogue": s.get("dialogue"), "sfx": s.get("sfx")} for s in shots]
        try:
            data = await _ask_json(
                agg, model, PROMPT_SYSTEM,
                f"画风：{style}；画幅：{aspect}\n资产表：{json.dumps(asset_names, ensure_ascii=False)}\n"
                f"分镜 JSON：\n{json.dumps(payload, ensure_ascii=False)}", 4000)
            items = (data or {}).get("items") or []
            if items:
                return _normalize_prompts(items, shots, style, aspect), "real"
        except (AggError, KeyError, TypeError):
            if mode() == "real":
                raise
    return [mock_prompt(s, style, aspect) for s in shots], "mock"


# ======================================================================
# 出图提示词（确定性，不调模型）
# ======================================================================

def asset_image_prompt(kind: str, name: str, description: str, style: str) -> str:
    if kind == "character":
        return (f"{style}风格角色设定图，{name}：{description}。全身正面站姿，纯色背景，"
                f"五官清晰，服装细节完整，无文字。")
    if kind == "scene":
        return f"{style}风格场景概念图，{name}：{description}。无人物，构图完整，光线明确，无文字。"
    return f"{style}风格道具设定图，{name}：{description}。纯色背景，细节清晰，无文字。"


def frame_image_prompt(shot: dict, style: str, aspect: str) -> str:
    parts = [f"{style}风格电影分镜画面，{aspect}",
             f"{shot.get('size') or '中景'}，{shot.get('angle') or '平视'}",
             shot.get("picture") or shot.get("scene") or ""]
    if shot.get("blocking"):
        parts.append(f"站位：{shot['blocking']}")
    parts.append("无文字，无水印")
    return "；".join(p for p in parts if p)
