"""SQLite：项目 / 剧集 / 场次 / 资产(形态·版本) / 镜头 / 提示词 / 视频任务(历史) / 素材 / 后台任务 /
变更日志 / 对话 / 通知。

并发控制按"对象 + 字段"做（见 pipeline.Workflow.edit）：
- 用户可编辑的对象（shot / prompt / asset / form / scene）各自带 rev；
- 系统写回（视频回执、下载、合成、出图）只写系统字段，不推进用户对象的 rev，
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
SCHEMA_VERSION = 3


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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  style TEXT NOT NULL DEFAULT '真人',
  script_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  settings_json TEXT NOT NULL DEFAULT '{}',
  cover_path TEXT NOT NULL DEFAULT '',
  pinned INTEGER NOT NULL DEFAULT 0,
  text_charged_rmb REAL NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS scenes (                       -- 场次：剧本里的一场戏（"1-1 日 外 金鸡岭"）
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  no INTEGER NOT NULL,
  code TEXT NOT NULL DEFAULT '',
  heading TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  time_of_day TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  blocking_note TEXT NOT NULL DEFAULT '',                -- 站位说明（可编辑）
  blocking_url TEXT NOT NULL DEFAULT '',                 -- 站位图（系统字段）
  blocking_path TEXT NOT NULL DEFAULT '',
  blocking_prompt TEXT NOT NULL DEFAULT '',
  blocking_charged_rmb REAL NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                     -- character | scene | prop | creature | audio
  name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',         -- 审核意见 / 修改备注
  rev INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, kind, name)
);
CREATE TABLE IF NOT EXISTS asset_forms (                  -- 形态：孔宣·默认 / 蛋 / 战斗 / 重伤 …
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  current_version INTEGER NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT '',
  UNIQUE(asset_id, name)
);
CREATE TABLE IF NOT EXISTS asset_versions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  form_id TEXT NOT NULL REFERENCES asset_forms(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  media_type TEXT NOT NULL DEFAULT 'image',   -- image | audio
  prompt TEXT NOT NULL DEFAULT '',
  image_url TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',            -- generated | edited | uploaded | extracted | mock
  model TEXT NOT NULL DEFAULT '',
  charged_rmb REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(form_id, version)
);
CREATE TABLE IF NOT EXISTS shots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  scene_id TEXT NOT NULL DEFAULT '',
  shot_no INTEGER NOT NULL,
  duration_s REAL NOT NULL DEFAULT 5,
  scene TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  angle TEXT NOT NULL DEFAULT '',
  movement TEXT NOT NULL DEFAULT '',
  picture TEXT NOT NULL DEFAULT '',       -- 画面时间轴：【0-3秒】…【3-6秒】…
  performance TEXT NOT NULL DEFAULT '',   -- 表演
  blocking TEXT NOT NULL DEFAULT '',      -- 站位：人物空间关系
  dialogue TEXT NOT NULL DEFAULT '',
  sfx TEXT NOT NULL DEFAULT '',
  transition TEXT NOT NULL DEFAULT '',
  original_text TEXT NOT NULL DEFAULT '',
  characters_json TEXT NOT NULL DEFAULT '[]',
  scene_asset TEXT NOT NULL DEFAULT '',
  frame_url TEXT NOT NULL DEFAULT '',     -- 分镜图（系统字段）
  frame_path TEXT NOT NULL DEFAULT '',
  frame_charged_rmb REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'storyboard_ready',
  selected_job_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'ai',      -- ai | mock | manual
  rev INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS prompts (
  id TEXT PRIMARY KEY,
  shot_id TEXT NOT NULL UNIQUE REFERENCES shots(id) ON DELETE CASCADE,
  positive TEXT NOT NULL DEFAULT '',
  negative TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  bindings_json TEXT NOT NULL DEFAULT '[]',   -- @提及 → 资产 + 形态 + 版本
  unresolved_json TEXT NOT NULL DEFAULT '[]',
  rev INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (                    -- 本地素材：上传的图/视频/音频，或从成片截出的音色
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  media_type TEXT NOT NULL,               -- image | video | audio
  local_path TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'uploaded',
  source_job_id TEXT NOT NULL DEFAULT '',
  clip_start REAL NOT NULL DEFAULT 0,
  clip_seconds REAL NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  shot_id TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  version INTEGER NOT NULL DEFAULT 1,
  client_request_id TEXT NOT NULL DEFAULT '',
  agg_task_id TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  resolution TEXT NOT NULL,
  aspect_ratio TEXT NOT NULL DEFAULT '9:16',
  duration_s REAL NOT NULL,
  mode TEXT NOT NULL DEFAULT 'text',      -- text | first_frame | reference
  refs_json TEXT NOT NULL DEFAULT '[]',
  prompt_snapshot TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'submitted',
  url TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  poster_path TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS chat_sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT 'info',     -- info | success | warning | error
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eps_project ON episodes(project_id, no);
CREATE INDEX IF NOT EXISTS idx_scenes_ep ON scenes(episode_id, no);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_forms_asset ON asset_forms(asset_id, sort);
CREATE INDEX IF NOT EXISTS idx_versions_form ON asset_versions(form_id, version);
CREATE INDEX IF NOT EXISTS idx_shots_project ON shots(project_id, episode_id, shot_no);
CREATE INDEX IF NOT EXISTS idx_jobs_shot ON jobs(shot_id, version);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_materials_project ON materials(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_changes_obj ON changes(object_id, rev);
CREATE INDEX IF NOT EXISTS idx_chat_msgs ON chat_messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_notif ON notifications(read, id);
"""


def _is_legacy() -> bool:
    if not os.path.exists(DB_PATH):
        return False
    c = sqlite3.connect(DB_PATH)
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return "projects" in names and "meta" not in names
    finally:
        c.close()


# 增量迁移：已有库缺的列在启动时补上（不丢数据）
MIGRATIONS = [
    ("projects", "text_charged_rmb", "REAL NOT NULL DEFAULT 0"),
    ("episodes", "cover_path", "TEXT NOT NULL DEFAULT ''"),
    ("episodes", "cover_charged_rmb", "REAL NOT NULL DEFAULT 0"),
]


def backup(keep: int = 10) -> str:
    """把当前库在线备份到 data/backups/（SQLite 备份接口，不停服），只留最近 keep 份。"""
    if not os.path.exists(DB_PATH):
        return ""
    bdir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
    os.makedirs(bdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(DB_PATH))[0]
    dst = os.path.join(bdir, f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    with _lock:
        src = sqlite3.connect(DB_PATH)
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
    olds = sorted(f for f in os.listdir(bdir) if f.startswith(stem + "-") and f.endswith(".db"))
    for f in olds[:-keep]:
        try:
            os.remove(os.path.join(bdir, f))
        except OSError:
            pass
    return dst


def _migrate(c) -> None:
    for table, col, ddl in MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init() -> None:
    with _lock:
        if _is_legacy():
            # 旧版本结构差异较大：整库改名备份后另起新库（旧库里只有联调用的数据）
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(DB_PATH + suffix):
                    os.replace(DB_PATH + suffix, f"{DB_PATH}.legacy-{stamp}{suffix}")
        with tx() as c:
            c.executescript(SCHEMA)
            _migrate(c)
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def load(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
