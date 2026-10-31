"""SQLite：钱包、预扣、账本、幂等、异步任务。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("AGG_DB", os.path.join(os.path.dirname(__file__), "..", "data", "agg.db"))
_lock = threading.Lock()


def now() -> str:
    """UTC，SQLite 可比较格式（与 datetime('now','-30 minutes') 同构）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


@contextmanager
def tx():
    with _lock:
        c = _conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet (
  user_id TEXT PRIMARY KEY,
  balance_rmb REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holds (
  hold_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  amount_rmb REAL NOT NULL,
  status TEXT NOT NULL,              -- held | settled | refunded | expired
  ref TEXT,
  model TEXT,
  provider TEXT,
  cost_rmb REAL,
  created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  hold_id TEXT,
  kind TEXT NOT NULL,                -- topup | reserve | settle | refund | adjust
  amount_rmb REAL NOT NULL,
  balance_after REAL NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idem (
  key TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS libtv_jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'submitting',   -- submitting | running | succeeded | failed
  project_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  node_id TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  seconds REAL NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  model TEXT NOT NULL,
  provider TEXT NOT NULL,
  api_id TEXT NOT NULL,
  spec TEXT,
  status TEXT NOT NULL,              -- submitted | running | succeeded | failed | cancelled
  hold_id TEXT,
  request_json TEXT,
  result_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holds_status ON holds(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id);
"""

# 兼容老库：缺列就补
MIGRATIONS = {
    "holds": {"model": "TEXT", "provider": "TEXT", "cost_rmb": "REAL"},
    "tasks": {"cost_rmb": "REAL"},
}


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        existing = {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in cols.items():
            if name not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def ensure_wallet(c: sqlite3.Connection, user_id: str, initial: float = 0.0) -> sqlite3.Row:
    row = c.execute("SELECT * FROM wallet WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        c.execute("INSERT INTO wallet(user_id,balance_rmb,updated_at) VALUES(?,?,?)", (user_id, initial, now()))
        row = c.execute("SELECT * FROM wallet WHERE user_id=?", (user_id,)).fetchone()
    return row


def add_ledger(c, user_id, hold_id, kind, amount, balance_after, detail="") -> None:
    c.execute(
        "INSERT INTO ledger(user_id,hold_id,kind,amount_rmb,balance_after,detail,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, hold_id, kind, round(amount, 6), round(balance_after, 6), detail, now()),
    )


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
