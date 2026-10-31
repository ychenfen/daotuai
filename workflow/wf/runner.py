"""后台任务：一键全流程等长操作放到后台跑，界面轮询 /tasks/{id} 看每一步进度。

同一项目同时只允许一个后台任务；服务重启时把没跑完的任务标成"中断"，可以再点一次续跑
（各环节都是幂等的：已有的资产/分镜/提示词会跳过，已经在跑的视频不会重复提交）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable

from . import db
from .errors import Busy, WorkflowError

log = logging.getLogger("workflow.runner")


class Progress:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.steps: list[dict] = []

    def _save(self, status: str | None = None) -> None:
        with db.tx() as c:
            if status:
                c.execute("UPDATE tasks SET steps_json=?, status=?, updated_at=? WHERE id=?",
                          (db.dump(self.steps), status, db.now(), self.task_id))
            else:
                c.execute("UPDATE tasks SET steps_json=?, updated_at=? WHERE id=?",
                          (db.dump(self.steps), db.now(), self.task_id))

    async def start(self, name: str) -> None:
        self.steps.append({"name": name, "status": "running", "started_at": db.now()})
        self._save("running")

    async def done(self, name: str, info: dict) -> None:
        for s in reversed(self.steps):
            if s["name"] == name:
                s.update(status="succeeded", finished_at=db.now(), info=info)
                break
        self._save()

    async def fail(self, name: str, error: str) -> None:
        for s in reversed(self.steps):
            if s["name"] == name:
                s.update(status="failed", finished_at=db.now(), error=error[:500])
                break
        self._save()


class Runner:
    def __init__(self):
        self._running: dict[str, asyncio.Task] = {}

    @staticmethod
    def recover() -> int:
        with db.tx() as c:
            n = c.execute("UPDATE tasks SET status='failed', error='服务重启，任务中断（可重新发起，已完成的环节会跳过）',"
                          " updated_at=? WHERE status IN ('queued','running')", (db.now(),)).rowcount
        return n

    def busy(self, pid: str) -> bool:
        t = self._running.get(pid)
        return bool(t and not t.done())

    def start(self, pid: str, kind: str, fn: Callable[[Progress], Awaitable[dict]]) -> dict:
        if self.busy(pid):
            raise Busy("这个项目已有任务在跑，等它结束或刷新查看进度")
        tid = "task_" + uuid.uuid4().hex[:12]
        with db.tx() as c:
            c.execute("INSERT INTO tasks(id,project_id,kind,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                      (tid, pid, kind, "queued", db.now(), db.now()))
        prog = Progress(tid)

        async def _run():
            try:
                result = await fn(prog)
                with db.tx() as c:
                    c.execute("UPDATE tasks SET status='succeeded', result_json=?, updated_at=? WHERE id=?",
                              (db.dump(result or {}), db.now(), tid))
            except Exception as e:  # 任务失败要落库，界面能看到原因
                log.exception("task %s failed", tid)
                with db.tx() as c:
                    c.execute("UPDATE tasks SET status='failed', error=?, updated_at=? WHERE id=?",
                              (str(e)[:1000], db.now(), tid))
            finally:
                self._running.pop(pid, None)

        self._running[pid] = asyncio.get_running_loop().create_task(_run())
        return self.get(tid)

    @staticmethod
    def get(tid: str) -> dict:
        with db.tx() as c:
            t = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t:
            raise WorkflowError(f"任务不存在：{tid}")
        t = dict(t)
        t["steps"] = db.load(t.pop("steps_json"), [])
        t["result"] = db.load(t.pop("result_json"), {})
        return t

    @staticmethod
    def list(pid: str, limit: int = 20) -> list[dict]:
        with db.tx() as c:
            rows = c.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                             (pid, limit)).fetchall()
        return [Runner.get(r["id"]) for r in rows]
