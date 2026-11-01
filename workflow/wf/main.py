"""工作流 HTTP 层 + 网页界面（/ui）+ 本地素材（/media）。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, llm, media
from .agg import Agg, AggError, make_client
from .errors import WorkflowError
from .pipeline import DEFAULT_SETTINGS, Workflow
from .runner import Runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("workflow")

AUTO_TOPUP = float(os.environ.get("WORKFLOW_AUTO_TOPUP", "100"))
AUTO_POLL_S = float(os.environ.get("WORKFLOW_POLL_INTERVAL_S", "15"))
MAX_UPLOAD = 200 * 1024 * 1024
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
    try:
        b = db.backup()
        if b:
            log.info("数据库已备份：%s", b)
    except Exception:
        log.exception("数据库备份失败")
    n = Runner.recover()
    if n:
        log.warning("marked %d interrupted tasks as failed", n)
    poller = None
    if AUTO_POLL_S > 0:
        async def _loop():
            while True:
                await asyncio.sleep(AUTO_POLL_S)
                try:
                    await wf().poll_videos()
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


app = FastAPI(title="短剧工作台（内部自用）", version="0.3.0", lifespan=lifespan)
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


async def _body(request: Request) -> bytes:
    data = await request.body()
    if len(data) > MAX_UPLOAD:
        raise WorkflowError("文件太大（上限 200 MB）")
    return data


def _download(path: str, filename: str, background: BackgroundTasks) -> FileResponse:
    background.add_task(lambda: os.path.exists(path) and os.remove(path))
    return FileResponse(path, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


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
    return {"ok": True, "version": app.version, "aggregator": agg_ok, "llm_mode": llm.mode(),
            "ffmpeg": bool(media.ffmpeg_bin()), "media_root": media.root(),
            "live_models": [m["id"] for m in models if m.get("live")]}


@app.get("/api/models")
async def api_models():
    return {"items": await wf().agg.models()}


@app.get("/api/wallet")
async def api_wallet():
    return await wf().agg.wallet()


@app.get("/api/libtv")
async def api_libtv(check: bool = False):
    """LibTV 连接状态；check=true 时再做一次只读的 doctor（身份、准入、工具列表）。"""
    st = await wf().agg.libtv("status")
    st["login_url"] = "/libtv/login"   # 经工作台转发：挂在服务器子路径下时也能用（前端会加前缀）
    st["redirect_uri"] = os.environ.get("LIBTV_REDIRECT_URI", "http://127.0.0.1:8010/libtv/callback")
    if check and st.get("authorized"):
        st["doctor"] = await wf().agg.libtv("doctor")
    return st


@app.get("/libtv/login")
async def libtv_login_proxy():
    """转发到聚合层拿授权地址（聚合层只听 127.0.0.1，浏览器在服务器场景下直接访问不到）。"""
    r = await wf().agg.c.get("/libtv/login", follow_redirects=False)
    if r.status_code in (301, 302, 303, 307) and r.headers.get("location"):
        return RedirectResponse(r.headers["location"])
    return HTMLResponse(r.text, status_code=r.status_code)


@app.get("/libtv/callback")
async def libtv_callback_proxy(request: Request):
    """LibTV 授权完成后回跳到这里（服务器上 LIBTV_REDIRECT_URI=https://域名/前缀/libtv/callback），转给聚合层换令牌。"""
    r = await wf().agg.c.get("/libtv/callback", params=dict(request.query_params))
    return HTMLResponse(r.text, status_code=r.status_code)


@app.post("/api/libtv/logout")
async def api_libtv_logout():
    r = await wf().agg.c.post("/libtv/logout")
    return r.json()


@app.get("/api/defaults")
def api_defaults():
    return DEFAULT_SETTINGS


# ---------------- 通知 ----------------

@app.get("/notifications")
def notifications(limit: int = 50, unread_only: bool = False):
    return wf().notifications(limit, unread_only)


@app.post("/notifications/read")
def notifications_read(body: dict | None = Body(default=None)):
    return wf().mark_read((body or {}).get("ids"))


# ---------------- 项目 ----------------

@app.post("/projects")
def create_project(body: dict = Body(...)):
    return wf().create(body.get("name", ""), body.get("style") or "真人", body.get("settings"))


@app.get("/projects")
def list_projects():
    return {"items": wf().list()}


@app.post("/projects/import")
async def import_project(request: Request):
    return wf().import_project(await _body(request))


@app.get("/projects/{pid}")
def get_project(pid: str):
    return wf().get(pid)


@app.delete("/projects/{pid}")
def delete_project(pid: str):
    if runner().busy(pid):
        raise WorkflowError("项目有任务在跑，先等它结束")
    return wf().delete(pid)


@app.post("/projects/{pid}/pin")
def pin_project(pid: str, pinned: bool = True):
    return wf().set_pinned(pid, pinned)


@app.post("/projects/{pid}/cover")
async def project_cover(pid: str, request: Request, filename: str = "cover.png"):
    return wf().set_cover(pid, await _body(request), filename)


@app.get("/projects/{pid}/export")
def export_project(pid: str, background: BackgroundTasks):
    path, name = wf().export_project(pid)
    return _download(path, name, background)


@app.get("/projects/{pid}/download")
def download(pid: str, background: BackgroundTasks, what: str = "videos", episode_no: int | None = None):
    path, name = wf().build_zip(pid, what, episode_no)
    return _download(path, name, background)


@app.get("/projects/{pid}/edit-package")
def edit_package(pid: str, background: BackgroundTasks, episode_no: int | None = None):
    path, name = wf().build_edit_package(pid, episode_no)
    return _download(path, name, background)


@app.patch("/projects/{pid}/settings")
def update_settings(pid: str, body: dict = Body(...)):
    return wf().update_settings(pid, body)


# ---------------- 剧本 ----------------

@app.post("/projects/{pid}/script")
def set_script(pid: str, body: dict = Body(...)):
    return wf().set_script(pid, body.get("text", ""))


@app.post("/projects/{pid}/script/upload")
async def upload_script(pid: str, request: Request, filename: str = "script.txt", mode: str = "replace"):
    return wf().import_script_file(pid, await _body(request), filename, mode)


@app.post("/projects/{pid}/script/generate")
async def generate_script(pid: str, body: dict = Body(...)):
    return await wf().generate_script(pid, body.get("idea", ""), int(body.get("episodes") or 3),
                                      body.get("mode") or "replace", int(body.get("seconds_per_ep") or 90))


@app.post("/projects/{pid}/parse")
def parse(pid: str, force: bool = False):
    return wf().parse(pid, force)


# ---------------- 资产 ----------------

@app.post("/projects/{pid}/assets/extract")
async def extract_assets(pid: str, engine: str | None = None):
    return await wf().extract_assets(pid, engine)


@app.post("/projects/{pid}/assets")
def add_asset(pid: str, body: dict = Body(...)):
    return wf().add_asset(pid, body.get("kind", "character"), body.get("name", ""), body.get("description", ""),
                          body.get("aliases") or [], body.get("forms") or [])


@app.post("/projects/{pid}/assets/images")
async def asset_images(pid: str, only_missing: bool = True, kind: str | None = None):
    return await wf().gen_asset_images(pid, only_missing, [kind] if kind else None)


@app.post("/projects/{pid}/assets/bulk-upload")
async def assets_bulk_upload(pid: str, request: Request, filename: str = "assets.zip"):
    """批量上传：一个 zip（里面的文件按「资产名.png」「资产名-形态.png」命名），或单个文件。"""
    import io
    import zipfile
    data = await _body(request)
    files = []
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = [(n, z.read(n)) for n in z.namelist() if not n.endswith("/")]
    else:
        files = [(filename, data)]
    return wf().bulk_upload(pid, files)


@app.get("/assets/{aid}")
def get_asset(aid: str):
    return wf().asset(aid)


@app.patch("/assets/{aid}")
def edit_asset(aid: str, body: dict = Body(...)):
    return wf().edit("asset", aid, body.get("fields") or {}, body.get("expected_rev"))


@app.delete("/assets/{aid}")
def delete_asset(aid: str):
    return wf().delete_asset(aid)


@app.post("/assets/{aid}/image")
async def asset_image(aid: str, form_id: str | None = None):
    return await wf().gen_asset_image(aid, form_id)


@app.post("/assets/{aid}/polish")
async def asset_polish(aid: str):
    return await wf().polish_asset(aid)


@app.post("/assets/{aid}/upload")
async def asset_upload(aid: str, request: Request, filename: str = "upload.png"):
    return wf().upload_asset_image(aid, await _body(request), filename)


@app.post("/assets/{aid}/select")
def asset_select(aid: str, version: int):
    return wf().select_asset_version(aid, version)


@app.post("/assets/{aid}/forms")
def add_form(aid: str, body: dict = Body(...)):
    return wf().add_form(aid, body.get("name", ""), body.get("description", ""))


@app.patch("/forms/{fid}")
def edit_form(fid: str, body: dict = Body(...)):
    return wf().edit("form", fid, body.get("fields") or {}, body.get("expected_rev"))


@app.delete("/forms/{fid}")
def delete_form(fid: str):
    return wf().delete_form(fid)


@app.post("/forms/{fid}/image")
async def form_image(fid: str):
    return await wf().gen_form_image(fid)


@app.post("/forms/{fid}/edit-image")
async def form_edit_image(fid: str, body: dict = Body(...)):
    return await wf().edit_form_image(fid, body.get("instruction", ""))


@app.post("/forms/{fid}/upscale")
async def form_upscale(fid: str):
    return await wf().upscale_form(fid)


@app.post("/forms/{fid}/upload")
async def form_upload(fid: str, request: Request, filename: str = "upload.png"):
    return wf().upload_form_media(fid, await _body(request), filename)


@app.post("/forms/{fid}/select")
def form_select(fid: str, version: int):
    return wf().select_version(fid, version)


# ---------------- 素材 ----------------

@app.post("/projects/{pid}/materials")
async def upload_material(pid: str, request: Request, filename: str = "material.png", name: str = ""):
    return wf().upload_material(pid, await _body(request), filename, name)


@app.delete("/materials/{mid}")
def delete_material(mid: str):
    return wf().delete_material(mid)


@app.post("/jobs/{jid}/extract-voice")
async def extract_voice(jid: str, body: dict = Body(...)):
    return await wf().extract_voice(jid, body.get("character", ""), float(body.get("start") or 0),
                                    float(body.get("seconds") or 5))


# ---------------- 分镜 / 站位图 / 分镜图 ----------------

@app.post("/projects/{pid}/storyboard")
async def storyboard(pid: str, episode_no: int | None = None, force: bool = False, max_shots: int | None = None,
                     engine: str | None = None):
    return await wf().storyboard(pid, episode_no, force, max_shots, engine)


@app.post("/projects/{pid}/shots")
def add_shot(pid: str, body: dict = Body(...)):
    return wf().add_shot(pid, int(body.get("episode_no") or 1), body.get("after_shot_no"), body.get("fields"))


@app.patch("/shots/{sid}")
def edit_shot(sid: str, body: dict = Body(...)):
    return wf().edit("shot", sid, body.get("fields") or {}, body.get("expected_rev"))


@app.delete("/shots/{sid}")
def delete_shot(sid: str):
    return wf().delete_shot(sid)


@app.patch("/scenes/{scid}")
def edit_scene(scid: str, body: dict = Body(...)):
    return wf().edit("scene", scid, body.get("fields") or {}, body.get("expected_rev"))


@app.post("/projects/{pid}/blocking")
async def blocking(pid: str, body: dict | None = Body(default=None)):
    body = body or {}
    return await wf().gen_blocking(pid, body.get("scene_ids"), body.get("only_missing", True), body.get("episode_no"))


@app.post("/projects/{pid}/covers")
async def covers(pid: str, episode_no: int | None = None):
    return await wf().gen_covers(pid, episode_no)


@app.post("/projects/{pid}/frames")
async def frames(pid: str, body: dict | None = Body(default=None)):
    body = body or {}
    return await wf().gen_frames(pid, body.get("shot_ids"), body.get("only_missing", True))


# ---------------- 提示词 ----------------

@app.post("/projects/{pid}/prompts")
async def prompts(pid: str, force: bool = False, episode_no: int | None = None, engine: str | None = None,
                  body: dict | None = Body(default=None)):
    body = body or {}
    return await wf().prompts(pid, force or bool(body.get("force")), body.get("shot_ids"),
                              episode_no if episode_no is not None else body.get("episode_no"),
                              engine or body.get("engine"))


@app.post("/projects/{pid}/prompts/from-script")
async def prompts_from_script(pid: str, episode_no: int | None = None):
    return await wf().prompts_from_script(pid, episode_no)


@app.patch("/prompts/{prid}")
def edit_prompt(prid: str, body: dict = Body(...)):
    return wf().edit("prompt", prid, body.get("fields") or {}, body.get("expected_rev"))


@app.post("/projects/{pid}/references/resolve")
def resolve_references(pid: str):
    return wf().resolve_references(pid)


# ---------------- 视频 ----------------

def _video_args(body: dict | None, force: bool, resolution: str | None) -> dict:
    body = body or {}
    dur = body.get("duration")
    return {"shot_ids": body.get("shot_ids"), "force": force or bool(body.get("force")),
            "resolution": body.get("resolution") or resolution, "model": body.get("model"),
            "duration": float(dur) if dur not in (None, "", "shot") else None,
            "episode_no": body.get("episode_no")}


@app.post("/projects/{pid}/videos/quote")
async def videos_quote(pid: str, force: bool = False, resolution: str | None = None,
                       body: dict | None = Body(default=None)):
    return await wf().quote_videos(pid, **_video_args(body, force, resolution))


@app.post("/projects/{pid}/videos")
async def videos(pid: str, force: bool = False, resolution: str | None = None,
                 body: dict | None = Body(default=None)):
    body = body or {}
    return await wf().submit_videos(pid, mode=body.get("mode"), aspect_ratio=body.get("aspect_ratio"),
                                    form_ids=body.get("form_ids"), material_ids=body.get("material_ids"),
                                    accept_stale=bool(body.get("accept_stale")),
                                    allow_missing=bool(body.get("allow_missing")),
                                    **_video_args(body, force, resolution))


@app.post("/projects/{pid}/videos/tool")
async def video_tool(pid: str, body: dict = Body(...)):
    """视频编辑 / 动作迁移：结果是该镜头的新版本。"""
    return await wf().submit_tool(pid, body.get("shot_id", ""), body.get("tool", ""), body.get("instruction", ""),
                                  body.get("model"), body.get("resolution"), body.get("source_job_id"),
                                  body.get("source_material_id"), body.get("form_id"))


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


# ---------------- 质检 / 配音 ----------------

@app.post("/projects/{pid}/qc")
async def qc_project(pid: str, body: dict = Body(default={})):
    return await wf().qc_project(pid, body.get("episode_no"), body.get("shot_ids"), bool(body.get("force")))


@app.post("/jobs/{jid}/qc")
async def qc_job(jid: str, force: bool = True):
    return await wf().qc_job(jid, force=force)


@app.get("/api/voices")
async def voices(project: str | None = None):
    return wf().voices(project)


@app.post("/assets/{aid}/voice")
async def set_voice(aid: str, body: dict = Body(...)):
    return wf().set_voice(aid, body.get("voice", ""))


@app.post("/projects/{pid}/voice-sample")
async def voice_sample(pid: str, body: dict = Body(...)):
    return await wf().voice_sample(pid, body.get("voice", ""), body.get("text", ""))


@app.post("/projects/{pid}/dub")
async def dub(pid: str, body: dict = Body(default={})):
    return await wf().dub_shots(pid, body.get("shot_ids"), body.get("episode_no"), bool(body.get("force")))


@app.delete("/projects/{pid}/dub")
async def clear_dub(pid: str, shot_id: str | None = None):
    return wf().clear_dub(pid, [shot_id] if shot_id else None)


# ---------------- 重抽 / 审片 / 拆卡 ----------------

@app.post("/projects/{pid}/retake")
async def retake(pid: str, body: dict = Body(default={})):
    return await wf().retake_shots(pid, body.get("shot_ids"), body.get("episode_no"),
                                   tuple(body.get("verdicts") or ("fail",)), body.get("engine"))


@app.post("/jobs/{jid}/review")
def review_job(jid: str, body: dict = Body(...)):
    return wf().review_job(jid, body.get("status", ""), body.get("note", ""))


@app.get("/projects/{pid}/density")
def density(pid: str, episode_no: int | None = None):
    return wf().density(pid, episode_no)


@app.get("/api/storage")
def storage():
    return wf().storage()


@app.get("/api/ops")
def ops_status():
    return wf().ops_status()


@app.post("/projects/{pid}/cleanup")
def cleanup(pid: str, body: dict = Body(default={})):
    return wf().cleanup(pid, bool(body.get("dry_run", True)), int(body.get("keep_versions", 1)))


@app.post("/projects/{pid}/export")
async def export_episode(pid: str, body: dict = Body(...)):
    return await wf().export_episode(pid, int(body.get("episode_no") or 1), body.get("preset") or "douyin",
                                     bool(body.get("cover_first")))


@app.get("/api/export-presets")
def export_presets():
    return {"items": [{"id": k, **v} for k, v in media.PRESETS.items()]}


@app.post("/shots/{sid}/cut")
def set_cut(sid: str, body: dict = Body(...)):
    return wf().set_cut(sid, body.get("trim_in"), body.get("trim_out"), body.get("xfade_s"))


@app.post("/shots/{sid}/split")
async def split_shot(sid: str):
    return await wf().split_shot(sid)


# ---------------- AI 对话 ----------------

@app.get("/chat/agents")
def chat_agents():
    return {"items": wf().chat_agents()}


@app.get("/chat/sessions")
def chat_sessions(project_id: str | None = None):
    return {"items": wf().chat_list(project_id)}


@app.post("/chat/sessions")
def chat_create(body: dict = Body(...)):
    return wf().chat_create(body.get("agent", "assistant"), body.get("project_id") or "", body.get("title") or "",
                            body.get("model") or "")


@app.get("/chat/sessions/{sid}")
def chat_get(sid: str):
    return wf().chat_get(sid)


@app.delete("/chat/sessions/{sid}")
def chat_delete(sid: str):
    return wf().chat_delete(sid)


@app.delete("/chat/sessions")
def chat_clear():
    return wf().chat_delete(None)


@app.post("/chat/sessions/{sid}/messages")
async def chat_send(sid: str, body: dict = Body(...)):
    return await wf().chat_send(sid, body.get("content", ""), body.get("model"))


# ---------------- 一键（后台任务） ----------------

@app.post("/projects/{pid}/run")
async def run_all(pid: str, resolution: str | None = None, wait: bool = False):
    """一键成片：分集 → 资产 → 参考图 → 分镜 → 站位图 → (分镜图) → 提示词 → 预算内提交视频 → 等出片 → 合成。
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
