"""计费：报价 → 预扣 → 结算 / 退回，全部落账本；另含报表与过期回收。"""
from __future__ import annotations

import uuid

from . import db
from .config import Route, models


class InsufficientFunds(Exception):
    pass


class HoldNotFound(Exception):
    pass


POINTS_PER_RMB = 10.0  # 沿用刀徒：1 元 = 10 积分


def rmb_to_points(rmb: float) -> float:
    return round(rmb * POINTS_PER_RMB, 4)


# ---------------- 报价 ----------------

def quote(model_id: str, *, spec: str | None = None, seconds: float | None = None,
          images: int = 1, in_tokens: int = 0, out_tokens: int = 0, chars: int = 0,
          route: Route | None = None, basis: str = "price") -> dict:
    """basis=price 算零售价（收用户多少），basis=cost 算上游成本（我们花多少）。"""
    m = models()[model_id]
    price = m.pricing(route, basis)
    if m.unit == "tokens":
        rmb = (in_tokens / 1e6) * price["input_rmb_per_m"] + (out_tokens / 1e6) * price["output_rmb_per_m"]
    elif m.unit == "image":
        rmb = price["rmb_per_image"] * max(1, images)
    elif m.unit == "char":   # 语音合成按字符
        rmb = price["rmb_per_10k_chars"] * max(1, chars) / 10000
    else:  # output_second / input_second
        rate = price["rmb_per_second"][spec]
        rmb = rate * (seconds or 0)
    rmb = round(rmb, 6)
    return {"model": model_id, "unit": m.unit, "spec": spec, "basis": basis,
            "rmb": rmb, "points": rmb_to_points(rmb) if basis == "price" else None}


# ---------------- 钱包 ----------------

def balance(user_id: str) -> float:
    with db.tx() as c:
        return float(db.ensure_wallet(c, user_id)["balance_rmb"])


def topup(user_id: str, amount_rmb: float) -> float:
    with db.tx() as c:
        row = db.ensure_wallet(c, user_id)
        new_balance = float(row["balance_rmb"]) + amount_rmb
        c.execute("UPDATE wallet SET balance_rmb=?, updated_at=? WHERE user_id=?", (new_balance, db.now(), user_id))
        db.add_ledger(c, user_id, None, "topup", amount_rmb, new_balance, "manual topup")
        return new_balance


def reserve(user_id: str, amount_rmb: float, ref: str = "", model: str | None = None,
            provider: str | None = None) -> str:
    """冻结额度。返回 hold_id。余额不足直接拒绝（对应刀徒的预扣积分）。"""
    with db.tx() as c:
        row = db.ensure_wallet(c, user_id)
        bal = float(row["balance_rmb"])
        if amount_rmb > bal + 1e-9:
            raise InsufficientFunds(f"balance {bal:.4f} < required {amount_rmb:.4f}")
        new_balance = bal - amount_rmb
        hold_id = "hold_" + uuid.uuid4().hex[:16]
        c.execute("UPDATE wallet SET balance_rmb=?, updated_at=? WHERE user_id=?", (new_balance, db.now(), user_id))
        c.execute("""INSERT INTO holds(hold_id,user_id,amount_rmb,status,ref,model,provider,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (hold_id, user_id, round(amount_rmb, 6), "held", ref, model, provider, db.now()))
        db.add_ledger(c, user_id, hold_id, "reserve", -amount_rmb, new_balance, ref)
        return hold_id


def settle(hold_id: str, actual_rmb: float, detail: str = "", cost_rmb: float | None = None) -> dict:
    """按实际用量结算：预扣差额退回（或补扣），账本只记差额，保证账本可加总。"""
    with db.tx() as c:
        hold = c.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        if hold is None:
            raise HoldNotFound(hold_id)
        if hold["status"] != "held":
            return {"hold_id": hold_id, "status": hold["status"], "charged_rmb": float(hold["amount_rmb"]),
                    "idempotent": True}
        user_id = hold["user_id"]
        held = float(hold["amount_rmb"])
        delta = round(held - actual_rmb, 6)  # >0 退差额，<0 补扣
        row = c.execute("SELECT * FROM wallet WHERE user_id=?", (user_id,)).fetchone()
        new_balance = max(0.0, float(row["balance_rmb"]) + delta)
        c.execute("UPDATE wallet SET balance_rmb=?, updated_at=? WHERE user_id=?", (new_balance, db.now(), user_id))
        c.execute("""UPDATE holds SET status='settled', amount_rmb=?, cost_rmb=?, closed_at=?
                     WHERE hold_id=?""",
                  (round(actual_rmb, 6), None if cost_rmb is None else round(cost_rmb, 6), db.now(), hold_id))
        db.add_ledger(c, user_id, hold_id, "settle", delta, new_balance, detail)
        return {"hold_id": hold_id, "status": "settled", "held_rmb": held,
                "charged_rmb": round(actual_rmb, 6), "cost_rmb": cost_rmb,
                "adjust_rmb": delta, "balance_rmb": new_balance}


def refund(hold_id: str, reason: str = "") -> dict:
    with db.tx() as c:
        hold = c.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        if hold is None:
            raise HoldNotFound(hold_id)
        if hold["status"] != "held":
            return {"hold_id": hold_id, "status": hold["status"], "refunded_rmb": 0.0}
        user_id, held = hold["user_id"], float(hold["amount_rmb"])
        row = c.execute("SELECT * FROM wallet WHERE user_id=?", (user_id,)).fetchone()
        new_balance = float(row["balance_rmb"]) + held
        c.execute("UPDATE wallet SET balance_rmb=?, updated_at=? WHERE user_id=?", (new_balance, db.now(), user_id))
        c.execute("UPDATE holds SET status='refunded', closed_at=? WHERE hold_id=?", (db.now(), hold_id))
        db.add_ledger(c, user_id, hold_id, "refund", held, new_balance, reason)
        return {"hold_id": hold_id, "status": "refunded", "refunded_rmb": held, "balance_rmb": new_balance}


def ledger(user_id: str, limit: int = 50) -> list[dict]:
    with db.tx() as c:
        rows = c.execute(
            "SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]


def attach_provider(hold_id: str, provider: str, cost_rmb: float | None = None) -> None:
    """预扣发生在选路之前，选完路把 provider / 预估成本补到 hold 上，供报表使用。"""
    with db.tx() as c:
        c.execute("UPDATE holds SET provider=?, cost_rmb=COALESCE(?, cost_rmb) WHERE hold_id=?",
                  (provider, None if cost_rmb is None else round(cost_rmb, 6), hold_id))


# ---------------- 汇总 / 报表 ----------------

def wallet_summary(user_id: str) -> dict:
    with db.tx() as c:
        row = db.ensure_wallet(c, user_id)
        totals = {r["kind"]: round(float(r["s"]), 6) for r in c.execute(
            "SELECT kind, SUM(amount_rmb) s FROM ledger WHERE user_id=? GROUP BY kind", (user_id,)).fetchall()}
        open_holds = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount_rmb),0) s FROM holds WHERE user_id=? AND status='held'",
            (user_id,)).fetchone()
        spent = c.execute(
            "SELECT COALESCE(SUM(amount_rmb),0) s FROM holds WHERE user_id=? AND status='settled'",
            (user_id,)).fetchone()["s"]
        cost = c.execute(
            "SELECT COALESCE(SUM(cost_rmb),0) s FROM holds WHERE user_id=? AND status='settled'",
            (user_id,)).fetchone()["s"]
    return {
        "user": user_id,
        "balance_rmb": float(row["balance_rmb"]),
        "points": rmb_to_points(float(row["balance_rmb"])),
        "total_topup_rmb": round(totals.get("topup", 0.0), 6),
        "total_spent_rmb": round(float(spent), 6),
        "total_cost_rmb": round(float(cost), 6),
        "gross_margin_rmb": round(float(spent) - float(cost), 6),
        "open_holds": {"count": int(open_holds["n"]), "amount_rmb": round(float(open_holds["s"]), 6)},
        "by_kind": totals,
    }


def report(days: int = 7, limit: int = 100) -> dict:
    """按模型 / 渠道统计收入、成本、毛利率。"""
    since = f"-{max(1, int(days))} days"
    with db.tx() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT COALESCE(model,'?') model, COALESCE(provider,'?') provider,
                      COUNT(*) n, SUM(amount_rmb) revenue, SUM(COALESCE(cost_rmb,0)) cost
               FROM holds
               WHERE status='settled' AND closed_at >= datetime('now', ?)
               GROUP BY model, provider ORDER BY revenue DESC LIMIT ?""",
            (since, limit)).fetchall()]
        by_model = [dict(r) for r in c.execute(
            """SELECT COALESCE(model,'?') model, COUNT(*) n, SUM(amount_rmb) revenue,
                      SUM(COALESCE(cost_rmb,0)) cost
               FROM holds WHERE status='settled' AND closed_at >= datetime('now', ?)
               GROUP BY model ORDER BY revenue DESC""", (since,)).fetchall()]
        refunds = c.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(amount_rmb),0) s FROM holds
               WHERE status IN ('refunded','expired') AND closed_at >= datetime('now', ?)""",
            (since,)).fetchone()

    def enrich(r):
        rev, cost = float(r["revenue"] or 0), float(r["cost"] or 0)
        r["revenue"] = round(rev, 6)
        r["cost"] = round(cost, 6)
        r["margin"] = round(rev - cost, 6)
        r["margin_pct"] = round((rev - cost) / rev * 100, 2) if rev else 0.0
        return r

    rows = [enrich(r) for r in rows]
    by_model = [enrich(r) for r in by_model]
    total_rev = round(sum(r["revenue"] for r in rows), 6)
    total_cost = round(sum(r["cost"] for r in rows), 6)
    return {
        "period_days": days,
        "totals": {"revenue_rmb": total_rev, "cost_rmb": total_cost,
                   "margin_rmb": round(total_rev - total_cost, 6),
                   "margin_pct": round((total_rev - total_cost) / total_rev * 100, 2) if total_rev else 0.0},
        "by_model": by_model,
        "by_model_provider": rows,
        "refunds": {"count": int(refunds["n"]), "amount_rmb": round(float(refunds["s"]), 6)},
    }


def reconcile(orphan_minutes: int = 30) -> dict:
    """对账：找挂着没结算的预扣、统计账本、给出异常。"""
    with db.tx() as c:
        orphans = [dict(r) for r in c.execute(
            "SELECT hold_id,user_id,amount_rmb,model,provider,ref,created_at FROM holds "
            "WHERE status='held' AND created_at < datetime('now', ?) ORDER BY created_at LIMIT 200",
            (f"-{int(orphan_minutes)} minutes",)).fetchall()]
        totals = {r["kind"]: round(float(r["s"]), 6) for r in c.execute(
            "SELECT kind, SUM(amount_rmb) s FROM ledger GROUP BY kind").fetchall()}
        hold_stats = {r["status"]: int(r["n"]) for r in c.execute(
            "SELECT status, COUNT(*) n FROM holds GROUP BY status").fetchall()}
        users = c.execute("SELECT COUNT(*) n FROM wallet").fetchone()["n"]
        balance_sum = c.execute("SELECT COALESCE(SUM(balance_rmb),0) s FROM wallet").fetchone()["s"]

    accounted = totals.get("topup", 0.0) + totals.get("reserve", 0.0) + \
        totals.get("settle", 0.0) + totals.get("refund", 0.0)
    drift = round(float(balance_sum) - accounted, 6)
    return {
        "ok": not orphans and abs(drift) < 1e-6,
        "users": int(users),
        "balance_sum_rmb": round(float(balance_sum), 6),
        "ledger_totals": totals,
        "drift_rmb": drift,
        "holds": hold_stats,
        "orphan_holds": orphans,
        "note": "drift 应为 0：topup + reserve + settle + refund 应等于钱包余额之和",
    }


def reap_orphans(older_than_minutes: int = 30, dry_run: bool = False) -> dict:
    """回收超时未结算的预扣：默认全额退回并把 hold 标记 expired。"""
    cutoff = f"-{int(older_than_minutes)} minutes"
    with db.tx() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT hold_id,user_id,amount_rmb,model,provider,ref,created_at FROM holds "
            "WHERE status='held' AND created_at < datetime('now', ?)", (cutoff,)).fetchall()]
    reaped = []
    if not dry_run:
        for r in rows:
            res = refund(r["hold_id"], f"orphan reaped ({older_than_minutes}m)")
            with db.tx() as c:
                c.execute("UPDATE holds SET status='expired' WHERE hold_id=? AND status='refunded'", (r["hold_id"],))
            reaped.append({**r, "refunded_rmb": res.get("refunded_rmb", 0.0)})
    return {"dry_run": dry_run, "eligible": len(rows),
            "refunded_rmb": round(sum(x["amount_rmb"] for x in rows), 6),
            "holds": reaped if not dry_run else rows}
