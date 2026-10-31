"""SQLite：项目 / 剧集 / 分镜 / 提示词 / 视频任务。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("WORKFLOW_DB", os.path.join(os.path.dirname(__file__), "..", "data", "workflow.db"))
_lock = threading.Lock()


def now() -> str:
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
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  style TEXT NOT NULL DEFAULT '真人',
  script_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  no INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  raw_text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  episode_id TEXT NOT NULL,
  shot_no INTEGER NOT NULL,
  duration_s REAL NOT NULL DEFAULT 5,
  scene TEXT DEFAULT '',
  size TEXT DEFAULT '',
  angle TEXT DEFAULT '',
  movement TEXT DEFAULT '',
  picture TEXT DEFAULT '',
  dialogue TEXT DEFAULT '',
  sfx TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS prompts (
  id TEXT PRIMARY KEY,
  shot_id TEXT NOT NULL,
  positive TEXT NOT NULL DEFAULT '',
  negative TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  shot_id TEXT NOT NULL,
  agg_task_id TEXT NOT NULL,
  model TEXT NOT NULL,
  resolution TEXT NOT NULL,
  duration_s REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'submitted',
  url TEXT DEFAULT '',
  charged_rmb REAL DEFAULT 0,
  error TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_shot ON prompts(shot_id);
CREATE INDEX IF NOT EXISTS idx_shots_project ON shots(project_id, episode_id, shot_no);
CREATE INDEX IF NOT EXISTS idx_jobs_shot ON jobs(shot_id);
"""


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)


def row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
