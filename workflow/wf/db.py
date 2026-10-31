"""SQLite：项目 / 剧集 / 资产(含版本) / 镜头 / 提示词 / 视频任务(含历史) / 后台任务 / 变更日志。

并发控制按"对象 + 字段"做（见 pipeline.Workflow.edit）：
- 用户可编辑的对象（shot / prompt / asset）各自带 rev；
- 系统写回（视频回执、下载、合成）只写 jobs / episodes 等系统字段，不推进用户对象的 rev，
  所以"后台回执抢跑导致保存失败"的问题从结构上不存在。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

WF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("WORKFLOW_DB", os.path.join(WF_ROOT, "data", "studio.db"))
_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
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
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  no INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  raw_text TEXT NOT NULL DEFAULT '',
  final_path TEXT NOT NULL DEFAULT '',
  srt_path TEXT NOT NULL DEFAULT '',
  final_seconds REAL NOT NULL DEFAULT 0,
  final_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                     -- character | scene | prop
  name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '',
  current_version INTEGER NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, kind, name)
);
CREATE TABLE IF NOT EXISTS asset_versions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  prompt TEXT NOT NULL DEFAULT '',
  image_url TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',        -- generated | uploaded | mock
  charged_rmb REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(asset_id, version)
);
CREATE TABLE IF NOT EXISTS shots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  shot_no INTEGER NOT NULL,
  duration_s REAL NOT NULL DEFAULT 5,
  scene TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  angle TEXT NOT NULL DEFAULT '',
  movement TEXT NOT NULL DEFAULT '',
  picture TEXT NOT NULL DEFAULT '',
  blocking TEXT NOT NULL DEFAULT '',      -- 站位：人物空间关系
  dialogue TEXT NOT NULL DEFAULT '',
  sfx TEXT NOT NULL DEFAULT '',
  characters_json TEXT NOT NULL DEFAULT '[]',
  scene_asset TEXT NOT NULL DEFAULT '',
  frame_url TEXT NOT NULL DEFAULT '',     -- 分镜图（系统字段）
  frame_path TEXT NOT NULL DEFAULT '',
  frame_charged_rmb REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'storyboard_ready',
  selected_job_id TEXT NOT NULL DEFAULT '',
  rev INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS prompts (
  id TEXT PRIMARY KEY,
  shot_id TEXT NOT NULL UNIQUE REFERENCES shots(id) ON DELETE CASCADE,
  positive TEXT NOT NULL DEFAULT '',
  negative TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  bindings_json TEXT NOT NULL DEFAULT '[]',   -- @提及 → 资产 + 版本
  unresolved_json TEXT NOT NULL DEFAULT '[]',
  rev INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  version INTEGER NOT NULL DEFAULT 1,
  client_request_id TEXT NOT NULL DEFAULT '',
  agg_task_id TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  resolution TEXT NOT NULL,
  duration_s REAL NOT NULL,
  mode TEXT NOT NULL DEFAULT 'text',      -- text | first_frame | reference
  refs_json TEXT NOT NULL DEFAULT '[]',
  prompt_snapshot TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'submitted',
  url TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  seconds REAL NOT NULL DEFAULT 0,
  quoted_rmb REAL NOT NULL DEFAULT 0,
  charged_rmb REAL NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | succeeded | failed
  steps_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  rev INTEGER NOT NULL,
  fields_json TEXT NOT NULL,
  actor TEXT NOT NULL,                    -- user | ai
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eps_project ON episodes(project_id, no);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_shots_project ON shots(project_id, episode_id, shot_no);
CREATE INDEX IF NOT EXISTS idx_jobs_shot ON jobs(shot_id, version);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_changes_obj ON changes(object_id, rev);
"""


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def load(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
