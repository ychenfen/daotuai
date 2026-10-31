"""工作流 HTTP 层：剧本 → 分镜 → 提示词 → 视频，一键跑通。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db
from .agg import Agg, make_client
from .pipeline import Workflow, WorkflowError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("workflow")

MODEL_TEXT = os.environ.get("WORKFLOW_TEXT_MODEL", "gpt-6.1-sol")
MODEL_VIDEO = os.environ.get("WORKFLOW_VIDEO_MODEL", "seedance-2.5")
RESOLUTION = os.environ.get("WORKFLOW_RESOLUTION", "480P")
AUTO_TOPUP = float(os.environ.get("WORKFLOW_AUTO_TOPUP", "100"))
AUTO_POLL_S = float(os.environ.get("WORKFLOW_POLL_INTERVAL_S", "15"))

_state: dict = {"wf": None, "client": None}


def configure(client=None) -> Workflow:
    """给测试/嵌入方用：注入一个已建好的 httpx 客户端。"""
    db.init()
    _state["client"] = client or make_client()
    _state["wf"] = Workflow(Agg(_state["client"]), MODEL_TEXT, MODEL_VIDEO, RESOLUTION, AUTO_TOPUP)
    return _state["wf"]


def wf() -> Workflow:
    if _state["wf"] is None:
        configure()
    return _state["wf"]


class ProjectIn(BaseModel):
    name: str
    style: str = "真人"


class ScriptIn(BaseModel):
    text: str


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure()
    poller = None
    if AUTO_POLL_S > 0:
        async def _loop():
            while True:
                await asyncio.sleep(AUTO_POLL_S)
                try:
                    for p in wf().list():
                        if p["status"] in ("videos_running", "prompts_done"):
                            try:
                                await wf().poll_videos(p["id"])
                            except Exception:
                                log.exception("auto poll failed for %s", p["id"])
                except Exception:
                    log.exception("poller loop failed")
        poller = asyncio.create_task(_loop())
    try:
        yield
    finally:
        if poller:
            poller.cancel()
        client = _state.get("client")
        if client is not None:
            await client.aclose()


app = FastAPI(title="Daotu Workflow (internal)", version="0.1.0", lifespan=lifespan)
db.init()


@app.get("/healthz")
def healthz():
    return {"ok": True, "text_model": MODEL_TEXT, "video_model": MODEL_VIDEO,
            "resolution": RESOLUTION, "llm_mode": os.environ.get("WORKFLOW_LLM", "auto")}


@app.post("/projects")
def create_project(body: ProjectIn):
    return wf().create(body.name, body.style)


@app.get("/projects")
def list_projects():
    return {"items": wf().list()}


@app.get("/projects/{pid}")
def get_project(pid: str):
    try:
        return wf().get(pid)
    except WorkflowError as e:
        raise HTTPException(404, str(e))


@app.post("/projects/{pid}/script")
def set_script(pid: str, body: ScriptIn):
    try:
        return wf().set_script(pid, body.text)
    except WorkflowError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/parse")
def parse(pid: str):
    try:
        return wf().parse(pid)
    except WorkflowError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/storyboard")
async def storyboard(pid: str, episode_no: int | None = None, force: bool = False, max_shots: int = 8):
    try:
        return await wf().storyboard(pid, episode_no, force, max_shots)
    except WorkflowError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/prompts")
async def prompts(pid: str, force: bool = False):
    try:
        return await wf().prompts(pid, force)
    except WorkflowError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/videos")
async def videos(pid: str, force: bool = False, resolution: str | None = None):
    try:
        return await wf().submit_videos(pid, force, resolution)
    except WorkflowError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/videos/poll")
async def poll(pid: str):
    try:
        return await wf().poll_videos(pid)
    except WorkflowError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/run")
async def run_all(pid: str, max_shots: int = 8, resolution: str | None = None, force: bool = False):
    """一键：解析 → 分镜 → 提示词 → 提交视频。"""
    try:
        return await wf().run_all(pid, max_shots, resolution, force)
    except WorkflowError as e:
        raise HTTPException(400, str(e))
