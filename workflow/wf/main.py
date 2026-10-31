"""工作流 HTTP 层 + 网页界面（/）+ 本地素材（/media）。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, llm, media
from .agg import Agg, AggError, make_client
from .pipeline import Conflict, Workflow, WorkflowError
from .runner import Runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("workflow")

AUTO_TOPUP = float(os.environ.get("WORKFLOW_AUTO_TOPUP", "100"))
AUTO_POLL_S = float(os.environ.get("WORKFLOW_POLL_INTERVAL_S", "15"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_state: dict[str, Any] = {"wf": None, "client": None, "runner": None}


def configure(client=None) -> Workflow:
    """给测试/嵌入方用：注入一个已建好的 httpx 客户端。"""
    db.init()
    _state["client"] = client or make_client()
    _state["wf"] = Workflow(Agg(_state["client"]), AUTO_TOPUP)
    _state["runner"] = Runner()
    return _state["wf"]


def wf() -> Workflow:
    if _state["wf"] is None:
        configure()
    return _state["wf"]


def runner() -> Runner:
    if _state["runner"] is None:
        configure()
    return _state["runner"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure()
    n = Runner.recover()
    if n:
        log.warning("marked %d interrupted tasks as failed", n)
    poller = None
    if AUTO_POLL_S > 0:
        async def _loop():
            while True:
                await asyncio.sleep(AUTO_POLL_S)
                try:
                    r = await wf().poll_videos()
                    if r["done"] or r["failed"]:
                        log.info("auto poll: %d done, %d failed, %d running",
                                 len(r["done"]), len(r["failed"]), len(r["running"]))
                except Exception:
                    log.exception("auto poll failed")
        poller = asyncio.create_task(_loop())
    try:
        yield
    finally:
        if poller:
            poller.cancel()
        client = _state.get("client")
        if client is not None:
            await client.aclose()


app = FastAPI(title="刀徒工作流（内部自用）", version="0.2.0", lifespan=lifespan)
db.init()


# ---------------- 错误统一成 {"error": {...}} ----------------

@app.exception_handler(WorkflowError)
async def _wf_error(_: Request, e: WorkflowError):
    body: dict[str, Any] = {"message": str(e)}
    if getattr(e, "detail", None):
        body["detail"] = e.detail
    return JSONResponse(status_code=getattr(e, "status", 400), content={"error": body})


@app.exception_handler(AggError)
async def _agg_error(_: Request, e: AggError):
    return JSONResponse(status_code=502, content={"error": {"message": f"聚合层：{e}", "code": e.code}})


@app.exception_handler(media.MediaError)
async def _media_error(_: Request, e: media.MediaError):
    return JSONResponse(status_code=500, content={"error": {"message": str(e)}})


# ---------------- 界面与素材 ----------------

@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/ui/")


if os.path.isdir(STATIC_DIR):
    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
app.mount("/media", StaticFiles(directory=media.root(), check_dir=False), name="media")


@app.get("/healthz")
async def healthz():
    try:
        models = await wf().agg.models()
        agg_ok = True
    except Exception as e:  # 聚合层没起来时也要能打开界面
        models, agg_ok = [], False
        log.warning("aggregator unreachable: %s", e)
    return {"ok": True, "aggregator": agg_ok, "llm_mode": llm.mode(),
            "ffmpeg": bool(media.ffmpeg_bin()), "media_root": media.root(),
            "live_models": [m["id"] for m in models if m.get("live")]}


@app.get("/api/models")
async def api_models():
    return {"items": await wf().agg.models()}


@app.get("/api/wallet")
async def api_wallet():
    return await wf().agg.wallet()


@app.get("/api/defaults")
def api_defaults():
    from .pipeline import DEFAULT_SETTINGS
    return DEFAULT_SETTINGS


# ---------------- 项目 ----------------

@app.post("/projects")
def create_project(body: dict = Body(...)):
    return wf().create(body.get("name", ""), body.get("style") or "真人", body.get("settings"))


@app.get("/projects")
def list_projects():
    return {"items": wf().list()}


@app.get("/projects/{pid}")
def get_project(pid: str):
    return wf().get(pid)


@app.delete("/projects/{pid}")
def delete_project(pid: str):
    if runner().busy(pid):
        raise WorkflowError("项目有任务在跑，先等它结束")
    return wf().delete(pid)


@app.patch("/projects/{pid}/settings")
def update_settings(pid: str, body: dict = Body(...)):
    return wf().update_settings(pid, body)


@app.post("/projects/{pid}/script")
def set_script(pid: str, body: dict = Body(...)):
    return wf().set_script(pid, body.get("text", ""))


@app.post("/projects/{pid}/parse")
def parse(pid: str, force: bool = False):
    return wf().parse(pid, force)


# ---------------- 资产 ----------------

@app.post("/projects/{pid}/assets/extract")
async def extract_assets(pid: str):
    return await wf().extract_assets(pid)


@app.post("/projects/{pid}/assets")
def add_asset(pid: str, body: dict = Body(...)):
    return wf().add_asset(pid, body.get("kind", "character"), body.get("name", ""),
                          body.get("description", ""), body.get("aliases") or [])


@app.post("/projects/{pid}/assets/images")
async def asset_images(pid: str, only_missing: bool = True):
    return await wf().gen_asset_images(pid, only_missing)


@app.patch("/assets/{aid}")
def edit_asset(aid: str, body: dict = Body(...)):
    return wf().edit("asset", aid, body.get("fields") or {}, body.get("expected_rev"))


@app.delete("/assets/{aid}")
def delete_asset(aid: str):
    return wf().delete_asset(aid)


@app.post("/assets/{aid}/image")
async def asset_image(aid: str):
    return await wf().gen_asset_image(aid)


@app.post("/assets/{aid}/upload")
async def asset_upload(aid: str, request: Request, filename: str = "upload.png"):
    """请求体直接放图片二进制（不依赖 multipart）。"""
    data = await request.body()
    if len(data) > 20 * 1024 * 1024:
        raise WorkflowError("图片不能超过 20 MB")
    return wf().upload_asset_image(aid, data, filename)


@app.post("/assets/{aid}/select")
def asset_select(aid: str, version: int):
    return wf().select_asset_version(aid, version)


# ---------------- 分镜 / 分镜图 ----------------

@app.post("/projects/{pid}/storyboard")
async def storyboard(pid: str, episode_no: int | None = None, force: bool = False, max_shots: int | None = None):
    return await wf().storyboard(pid, episode_no, force, max_shots)


@app.patch("/shots/{sid}")
def edit_shot(sid: str, body: dict = Body(...)):
    return wf().edit("shot", sid, body.get("fields") or {}, body.get("expected_rev"))


@app.post("/projects/{pid}/frames")
async def frames(pid: str, body: dict | None = Body(default=None)):
    body = body or {}
    return await wf().gen_frames(pid, body.get("shot_ids"), body.get("only_missing", True))


# ---------------- 提示词 ----------------

@app.post("/projects/{pid}/prompts")
async def prompts(pid: str, force: bool = False, body: dict | None = Body(default=None)):
    body = body or {}
    return await wf().prompts(pid, force or bool(body.get("force")), body.get("shot_ids"))


@app.patch("/prompts/{prid}")
def edit_prompt(prid: str, body: dict = Body(...)):
    return wf().edit("prompt", prid, body.get("fields") or {}, body.get("expected_rev"))


@app.post("/projects/{pid}/references/resolve")
def resolve_references(pid: str):
    return wf().resolve_references(pid)


# ---------------- 视频 ----------------

def _video_args(body: dict | None, force: bool, resolution: str | None) -> dict:
    body = body or {}
    return {"shot_ids": body.get("shot_ids"), "force": force or bool(body.get("force")),
            "resolution": body.get("resolution") or resolution, "model": body.get("model")}


@app.post("/projects/{pid}/videos/quote")
async def videos_quote(pid: str, force: bool = False, resolution: str | None = None,
                       body: dict | None = Body(default=None)):
    return await wf().quote_videos(pid, **_video_args(body, force, resolution))


@app.post("/projects/{pid}/videos")
async def videos(pid: str, force: bool = False, resolution: str | None = None,
                 body: dict | None = Body(default=None)):
    args = _video_args(body, force, resolution)
    return await wf().submit_videos(pid, mode=(body or {}).get("mode"), **args)


@app.post("/projects/{pid}/videos/poll")
async def poll(pid: str):
    wf().get(pid)
    return await wf().poll_videos(pid)


@app.post("/jobs/{jid}/cancel")
async def cancel_job(jid: str):
    return await wf().cancel_job(jid)


@app.post("/shots/{sid}/select")
def select_job(sid: str, job_id: str):
    return wf().select_job(sid, job_id)


# ---------------- 成片 ----------------

@app.post("/projects/{pid}/assemble")
async def assemble(pid: str, episode_no: int | None = None, allow_partial: bool = False):
    return await wf().assemble(pid, episode_no, allow_partial)


# ---------------- 一键（后台任务） ----------------

@app.post("/projects/{pid}/run")
async def run_all(pid: str, resolution: str | None = None, wait: bool = False):
    """一键：分集 → 资产 → 参考图 → 分镜 → (分镜图) → 提示词 → 预算内提交视频 → 等出片 → 合成。
    默认放后台，返回任务；wait=true 时同步跑完再返回（脚本用）。"""
    p = wf().get(pid)
    if not p["script_text"].strip():
        raise WorkflowError("先写剧本")
    task = runner().start(pid, "run_all", lambda prog: wf().run_all(pid, prog, resolution))
    if wait:
        while runner().busy(pid):
            await asyncio.sleep(0.2)
        return runner().get(task["id"])
    return task


@app.get("/projects/{pid}/tasks")
def list_tasks(pid: str):
    return {"items": runner().list(pid)}


@app.get("/tasks/{tid}")
def get_task(tid: str):
    return runner().get(tid)


# 兼容：Conflict 需要 409 + 细节
__all__ = ["app", "configure", "Conflict"]
