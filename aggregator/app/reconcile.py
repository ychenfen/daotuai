"""账单对账：导入各家费用中心导出的账单 CSV，按天和我们的账本（按官方单价估算的成本）对比。

各家导出格式不一样，这里按表头关键词自动认列（日期 / 金额 / 产品），只统计 AI 相关的行：
- aliyun（阿里云费用中心 → 账单详情）：百炼 / 通义 / 万相 / 千问 / DashScope / 模型服务 等
- volcengine（火山引擎费用中心 → 账单明细）：方舟 / 豆包 / Seedance / Seedream 等
- deepseek（DeepSeek 开放平台 → 用量导出）：全部
我们的账本日期按北京时间（UTC+8）归日，和账单一致。
"""
from __future__ import annotations

import csv
import io
import re

from . import db

ACCOUNTS = {
    "aliyun": {"label": "阿里云（百炼）", "providers": ("dashscope", "dashscope_video", "dashscope_tts"),
               "items": r"百炼|大模型|dashscope|通义|千问|qwen|万相|wan|模型服务|语音合成|tts|视觉理解"},
    "volcengine": {"label": "火山引擎（方舟）", "providers": ("volcengine_official",),
                   "items": r"方舟|ark|豆包|doubao|seedance|seedream|模型|推理"},
    "deepseek": {"label": "DeepSeek", "providers": ("deepseek",), "items": r".*"},
}
_DATE_KEYS = ("账单日期", "消费日期", "消费时间", "使用开始时间", "计费开始时间", "日期", "时间", "billingdate", "date", "time")
_AMOUNT_KEYS = ("应付金额", "实付金额", "优惠后金额", "现金支付", "原价", "金额", "费用", "pretaxamount", "amount", "cost", "price")
_ITEM_KEYS = ("产品名称", "商品名称", "产品", "商品", "计费项名称", "计费项", "服务名称", "模型", "product", "service", "model", "item")


class BillError(Exception):
    pass


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise BillError("账单文件编码认不出来（支持 UTF-8 / GBK）")


def _pick(headers: list[str], keys: tuple[str, ...]) -> int | None:
    low = [h.strip().lower().replace(" ", "") for h in headers]
    for k in keys:
        for i, h in enumerate(low):
            if k in h:
                return i
    return None


def _date(v: str) -> str | None:
    m = re.search(r"(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})", v or "")
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _money(v: str) -> float | None:
    v = re.sub(r"[,，¥￥\s元]", "", v or "")
    try:
        return float(v)
    except ValueError:
        return None


def parse_bill(raw: bytes, account: str) -> dict:
    if account not in ACCOUNTS:
        raise BillError(f"账户只能是：{'、'.join(ACCOUNTS)}")
    text = _decode(raw)
    rows = list(csv.reader(io.StringIO(text)))
    # 有的导出前几行是说明文字：找第一行能认出 日期+金额 两列的当表头
    for hi, head in enumerate(rows[:15]):
        di, ai = _pick(head, _DATE_KEYS), _pick(head, _AMOUNT_KEYS)
        if di is not None and ai is not None and di != ai:
            break
    else:
        raise BillError("没认出表头：需要有“日期/时间”和“金额/费用”两列")
    ii = _pick(head, _ITEM_KEYS)
    pat = re.compile(ACCOUNTS[account]["items"], re.I)
    days: dict[str, float] = {}
    kept = skipped = 0
    skipped_rmb = 0.0
    for r in rows[hi + 1:]:
        if len(r) <= max(di, ai):
            continue
        d, amt = _date(r[di]), _money(r[ai])
        if not d or amt is None:
            continue
        item = r[ii] if ii is not None and ii < len(r) else ""
        if ii is not None and not pat.search(item):
            skipped += 1
            skipped_rmb += amt
            continue
        days[d] = days.get(d, 0.0) + amt
        kept += 1
    if not days:
        raise BillError("账单里没有找到 AI 相关的消费行")
    return {"columns": {"date": head[di], "amount": head[ai], "item": head[ii] if ii is not None else ""},
            "days": days, "rows": kept, "skipped_rows": skipped, "skipped_rmb": round(skipped_rmb, 4)}


def ours(account: str, start: str, end: str) -> dict[str, dict]:
    provs = ACCOUNTS[account]["providers"]
    q = ",".join("?" * len(provs))
    with db.tx() as c:
        rows = c.execute(
            f"""SELECT date(closed_at, '+8 hours') d, COUNT(*) n, SUM(COALESCE(cost_rmb,0)) cost, SUM(amount_rmb) charged
                FROM holds WHERE status='settled' AND provider IN ({q})
                AND date(closed_at, '+8 hours') BETWEEN ? AND ? GROUP BY d""", (*provs, start, end)).fetchall()
    return {r["d"]: {"calls": r["n"], "cost": float(r["cost"] or 0), "charged": float(r["charged"] or 0)} for r in rows}


def reconcile(raw: bytes, account: str) -> dict:
    bill = parse_bill(raw, account)
    days = sorted(bill["days"])
    mine = ours(account, days[0], days[-1])
    out = []
    for d in sorted(set(days) | set(mine)):
        b = round(bill["days"].get(d, 0.0), 4)
        m = mine.get(d, {"calls": 0, "cost": 0.0, "charged": 0.0})
        diff = round(b - m["cost"], 4)
        flag = abs(diff) > max(0.5, 0.1 * max(b, m["cost"]))
        out.append({"date": d, "bill_rmb": b, "our_cost_rmb": round(m["cost"], 4), "calls": m["calls"],
                    "diff_rmb": diff, "flag": flag})
    tb = round(sum(x["bill_rmb"] for x in out), 4)
    to = round(sum(x["our_cost_rmb"] for x in out), 4)
    return {"account": account, "label": ACCOUNTS[account]["label"], "columns": bill["columns"],
            "bill_rows": bill["rows"], "skipped_rows": bill["skipped_rows"], "skipped_rmb": bill["skipped_rmb"],
            "range": [out[0]["date"], out[-1]["date"]], "days": out, "bill_total": tb, "our_total": to,
            "diff_total": round(tb - to, 4), "ratio": round(tb / to, 3) if to else None,
            "advice": ("账单比估算高 {:.0%}，建议把目录里的成本价上调".format(tb / to - 1) if to and tb > to * 1.1 else
                       "账单比估算低 {:.0%}，目录成本价偏保守".format(1 - tb / to) if to and tb < to * 0.9 else
                       "账单与估算基本一致（±10% 内）" if to else "这段时间我们这边没有记录")}
