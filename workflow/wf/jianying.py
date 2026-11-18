"""剪映草稿导出：按“剪辑”页时间线生成一份剪映草稿（片段修剪、叠化、配音、配乐、字幕），连同素材打包成 zip。

用 pyJianYingDraft（Apache-2.0）生成“新草稿”——剪映 6+ 的加密只挡“读取已有草稿”，不挡打开程序写的明文新草稿
（作者在剪映 5.9 / 10.8 实测；剪映升级可能失效，所以成片仍以我们自己的 ffmpeg 合成为准）。
剪映要求素材用绝对路径：素材在服务端生成，最终在本地电脑上打开，所以导出时指定“素材放在电脑哪个文件夹”，
草稿里的路径统一改写成那个文件夹。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile

from . import media
from .errors import WorkflowError

DEFAULT_TARGET = "D:/剪映素材"


def available() -> bool:
    try:
        import pymediainfo
        import pyJianYingDraft  # noqa: F401
        return bool(pymediainfo.MediaInfo.can_parse())
    except Exception:
        return False


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:40] or "draft"


def build_draft(proj: dict, ep: dict, shots: list[dict], target_root: str, srt_path: str = "") -> tuple[str, str]:
    if not available():
        raise WorkflowError("服务器缺少剪映草稿组件（pip install pyJianYingDraft；Linux 还要 apt install libmediainfo0v5）")
    import pyJianYingDraft as jy
    st = proj["settings"]
    w, h = media.preset_size(st["aspect_ratio"], 1920)
    name = _safe(f"{proj['name']}_第{ep['no']}集")
    target = (target_root or DEFAULT_TARGET).replace("\\", "/").rstrip("/") + "/" + name
    work = tempfile.mkdtemp(prefix="jy_")
    mat_dir = os.path.join(work, "素材")
    os.makedirs(mat_dir)
    try:
        clips = []
        for s in shots:
            j = s["job"] if s.get("job") and s["job"]["status"] == "succeeded" else None
            if not (j and j.get("local_path") and os.path.exists(j["local_path"])):
                continue
            full = float(media.probe(j["local_path"]).get("duration") or j.get("seconds") or s["duration_s"])
            t_in, t_out = float(s.get("trim_in") or 0), float(s.get("trim_out") or 0)
            if full - t_in - t_out < 0.5:
                t_in = t_out = 0.0
            n = len(clips) + 1
            vpath = os.path.join(mat_dir, f"{n:02d}_{s['code']}.mp4")
            shutil.copy(j["local_path"], vpath)
            dub = ""
            if s.get("dub") and s.get("dub_path") and os.path.exists(s["dub_path"]):
                dub = os.path.join(mat_dir, f"{n:02d}_{s['code']}_配音.wav")
                shutil.copy(s["dub_path"], dub)
            clips.append({"path": vpath, "in": t_in, "dur": full - t_in - t_out, "xfade": float(s.get("xfade_s") or 0),
                          "dub": dub, "code": s["code"]})
        if not clips:
            raise WorkflowError(f"第 {ep['no']} 集还没有已出片的镜头")
        folder = jy.DraftFolder(work)
        script = folder.create_draft(name, w, h, 30, allow_replace=True)
        script.append_track(jy.TrackSpec(jy.TrackType.video, "画面"))
        dub_mode = st.get("dub_mode") or "off"
        has_dub = any(c["dub"] for c in clips) and dub_mode != "off"
        if has_dub:
            script.append_track(jy.TrackSpec(jy.TrackType.audio, "配音"))
        orig_vol = 0.0 if dub_mode == "replace" else (float(st.get("dub_orig_volume") or 0.35) if has_dub else 1.0)
        t = 0.0
        for k, c in enumerate(clips):
            rng = jy.trange_seconds(t, duration=c["dur"])
            seg = jy.VideoSegment(c["path"], rng, source_timerange=jy.trange_seconds(c["in"], duration=c["dur"]),
                                  volume=orig_vol if c["dub"] else (1.0 if dub_mode != "replace" else 0.0))
            if c["xfade"] >= 0.1 and k + 1 < len(clips):
                seg.add_transition(jy.TransitionType.叠化, duration=int(min(c["xfade"], c["dur"] / 2) * 1_000_000))
            script.add_segment(seg, "画面")
            if c["dub"] and has_dub:
                dlen = float(media.wav_seconds(c["dub"]) or 0) - c["in"]
                if dlen > 0.2:
                    script.add_segment(jy.AudioSegment(c["dub"], jy.trange_seconds(t, duration=min(dlen, c["dur"])),
                                                       source_timerange=jy.trange_seconds(c["in"], duration=min(dlen, c["dur"]))),
                                       "配音")
            t += c["dur"]
        bgm = next((m["local_path"] for m in proj["materials"] if m["id"] == st.get("bgm_material_id")
                    and m["media_type"] == "audio" and os.path.exists(m["local_path"])), "")
        if bgm:
            bpath = os.path.join(mat_dir, "配乐" + os.path.splitext(bgm)[1])
            shutil.copy(bgm, bpath)
            blen = float(media.probe(bpath).get("duration") or 0)
            if blen > 0.5:
                script.append_track(jy.TrackSpec(jy.TrackType.audio, "配乐"))
                pos = 0.0
                while pos < t - 0.05:          # 配乐循环铺满全片
                    d = min(blen, t - pos)
                    script.add_segment(jy.AudioSegment(bpath, jy.trange_seconds(pos, duration=d),
                                                       source_timerange=jy.trange_seconds(0, duration=d),
                                                       volume=float(st.get("bgm_volume") or 0.25)), "配乐")
                    pos += d
        if srt_path and os.path.exists(srt_path):
            spath = os.path.join(mat_dir, "字幕.srt")
            shutil.copy(srt_path, spath)
            script.import_srt(spath, "字幕")
        script.save()
        draft_dir = os.path.join(work, name)
        # 素材路径改写成电脑上的目标文件夹
        src_prefix = mat_dir.replace("\\", "/")
        for fn in os.listdir(draft_dir):
            if fn.endswith(".json"):
                p = os.path.join(draft_dir, fn)
                txt = open(p, encoding="utf-8").read()
                txt = txt.replace(src_prefix, target + "/素材").replace(json.dumps(src_prefix)[1:-1], target + "/素材")
                open(p, "w", encoding="utf-8").write(txt)
        fd, zpath = tempfile.mkstemp(suffix=".zip", prefix="jianying_")
        os.close(fd)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            for root, _, files in os.walk(work):
                for fn in files:
                    full = os.path.join(root, fn)
                    z.write(full, os.path.join(name, os.path.relpath(full, work)))
            z.writestr(f"{name}/导入说明.txt", (
                f"1. 把整个“{name}”文件夹解压到：{target.rsplit('/', 1)[0]}/\n"
                f"   （解压后素材应在 {target}/素材/ 下；路径不对剪映会提示素材丢失）\n"
                f"2. 把里面的“{name}”草稿文件夹（含 draft_content.json）复制到剪映草稿目录，一般是\n"
                f"   C:/Users/<用户名>/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft/\n"
                "3. 重新打开剪映，在草稿列表里找到它。转场第一次打开会联网下载，显示“加载失败”时关掉草稿再开一次。\n"
                "草稿由 pyJianYingDraft 生成（剪映 10.8 实测可用）；剪映升级后若打不开，请改用“导出剪辑工程”（EDL/FCPXML）。\n"))
        return zpath, f"{name}-剪映草稿.zip"
    finally:
        shutil.rmtree(work, ignore_errors=True)
