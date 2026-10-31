"""分镜 / 提示词生成：真实 LLM（走聚合层）+ 离线确定性 mock。

WORKFLOW_LLM=auto（默认）：先调模型，失败或输出不合法就退回 mock；
=real：只用模型；=mock：只用本地规则（零成本、可测试）。
"""
from __future__ import annotations

import json
import os
import re

from .agg import Agg, AggError

MODE = os.environ.get("WORKFLOW_LLM", "auto")

SIZES = ["远景", "全景", "中景", "近景", "特写"]
ANGLES = ["平视", "俯拍", "仰拍", "低角度", "荷兰角"]
MOVES = ["固定机位", "缓慢推镜", "缓慢拉镜", "横移跟随", "手持跟拍", "升降镜头"]

STORYBOARD_SYSTEM = """你是短剧分镜师。把一集剧本拆成镜头，只输出 JSON，不要解释。
JSON 结构：
{"shots":[{"shot_no":1,"duration":5,"scene":"场景","size":"景别","angle":"角度",
"movement":"运镜","picture":"画面内容（可直接给分镜师看的描述）",
"dialogue":"台词，没有就空字符串","sfx":"音效，没有就空字符串"}]}
要求：
1. 每个镜头 4~15 秒，按剧情节奏给时长；
2. 必须保留原剧本的人物、动作、台词，不得新增情节；
3. shot_no 从 1 连续编号；最多 %d 个镜头。"""

PROMPT_SYSTEM = """你是 AI 视频提示词工程师。把分镜转成视频模型可直接执行的提示词，只输出 JSON：
{"items":[{"shot_no":1,"positive":"...","negative":"..."}]}
要求：
1. positive 写成一个连续时间轴：景别/角度/运镜/画面/台词音效，中文，300 字以内；
2. 保留人物外观与空间关系描述，不要写"参考图"之类的话；
3. negative 写画面质量问题（模糊、变形、多手多脚、文字水印等）。"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
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


# ---------------- 分镜 ----------------

def mock_storyboard(episode_text: str, style: str, max_shots: int) -> list[dict]:
    paras = [p.strip() for p in re.split(r"\n+", episode_text) if len(p.strip()) >= 8]
    if not paras:
        paras = [episode_text.strip()[:200] or "空镜头"]
    shots = []
    for i, p in enumerate(paras[:max_shots], start=1):
        body = re.sub(r"^[△▲\-\s]+", "", p)
        dialogue = ""
        m = re.search(r"[“「\"]([^”」\"]{2,60})[”」\"]", body)
        if m:
            dialogue = m.group(1)
        sfx = " ".join(re.findall(r"[<＜]([^>＞]{1,20})[>＞]", body))
        chars = len(body)
        shots.append({
            "shot_no": i,
            "duration": _clamp(round(chars / 12) + 3, 4, 15, 5),
            "scene": body[:24],
            "size": SIZES[(i - 1) % len(SIZES)],
            "angle": ANGLES[(i - 1) % len(ANGLES)],
            "movement": MOVES[(i - 1) % len(MOVES)],
            "picture": f"{style}风格，{body[:180]}",
            "dialogue": dialogue,
            "sfx": sfx,
        })
    return shots


def _normalize_shots(raw: list, max_shots: int) -> list[dict]:
    out = []
    for i, s in enumerate(raw[:max_shots], start=1):
        if not isinstance(s, dict):
            continue
        out.append({
            "shot_no": i,
            "duration": _clamp(s.get("duration"), 4, 15, 5),
            "scene": str(s.get("scene") or "")[:200],
            "size": str(s.get("size") or "")[:20],
            "angle": str(s.get("angle") or "")[:20],
            "movement": str(s.get("movement") or "")[:40],
            "picture": str(s.get("picture") or "")[:800],
            "dialogue": str(s.get("dialogue") or "")[:400],
            "sfx": str(s.get("sfx") or "")[:200],
        })
    return out


async def storyboard(agg: Agg, episode_text: str, style: str, model: str, max_shots: int = 8) -> tuple[list[dict], str]:
    """返回 (shots, source)，source ∈ real|mock。"""
    if MODE != "mock":
        try:
            res = await agg.chat(model, [
                {"role": "system", "content": STORYBOARD_SYSTEM % max_shots},
                {"role": "user", "content": f"画风：{style}\n\n剧本：\n{episode_text[:6000]}"},
            ], max_tokens=2500, temperature=0.2)
            data = _extract_json(res["choices"][0]["message"]["content"])
            shots = _normalize_shots((data or {}).get("shots") or [], max_shots)
            if shots:
                return shots, "real"
        except (AggError, KeyError, TypeError):
            if MODE == "real":
                raise
    return mock_storyboard(episode_text, style, max_shots), "mock"


# ---------------- 提示词 ----------------

def mock_prompt(shot: dict, style: str) -> dict:
    start = 0.0
    end = float(shot.get("duration") or 5)
    lines = [
        f"【镜头{shot['shot_no']}（{start:g}-{end:g}秒）",
        f"景别：{shot.get('size') or '中景'}",
        f"角度：{shot.get('angle') or '平视'}",
        f"运镜：{shot.get('movement') or '固定机位'}",
        f"画面：{shot.get('picture') or shot.get('scene') or ''}",
    ]
    if shot.get("dialogue"):
        lines.append(f"台词：{shot['dialogue']}")
    if shot.get("sfx"):
        lines.append(f"音效：{shot['sfx']}")
    lines.append("转场：硬切】")
    positive = f"{style}风格，9:16 竖屏，电影级质感。" + "\n".join(lines)
    negative = "画面模糊、人物变形、多余肢体、文字水印、低清晰度、比例错误"
    return {"positive": positive[:900], "negative": negative}


def _normalize_prompts(raw: list, shots: list[dict], style: str) -> list[dict]:
    """按输入顺序对齐（不同集的 shot_no 会重复，不能只靠编号匹配）。"""
    out = []
    for i, s in enumerate(shots):
        p = raw[i] if i < len(raw) and isinstance(raw[i], dict) else None
        if p and p.get("positive"):
            out.append({"shot_no": s["shot_no"], "positive": str(p["positive"])[:1500],
                        "negative": str(p.get("negative") or "")[:400]})
        else:
            out.append({"shot_no": s["shot_no"], **mock_prompt(s, style)})
    return out


async def prompts(agg: Agg, shots: list[dict], style: str, model: str) -> tuple[list[dict], str]:
    if MODE != "mock" and shots:
        payload = [{"shot_no": s["shot_no"], "duration": s["duration"], "size": s["size"],
                    "angle": s["angle"], "movement": s["movement"], "picture": s["picture"],
                    "dialogue": s["dialogue"], "sfx": s["sfx"]} for s in shots]
        try:
            res = await agg.chat(model, [
                {"role": "system", "content": PROMPT_SYSTEM},
                {"role": "user", "content": f"画风：{style}\n分镜 JSON：\n{json.dumps(payload, ensure_ascii=False)}"},
            ], max_tokens=3000, temperature=0.2)
            data = _extract_json(res["choices"][0]["message"]["content"])
            got = _normalize_prompts((data or {}).get("items") or [], shots, style)
            if got:
                return got, "real"
        except (AggError, KeyError, TypeError):
            if MODE == "real":
                raise
    return [_normalize_prompts([], shots, style)][0], "mock"
