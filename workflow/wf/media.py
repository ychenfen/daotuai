"""本地素材：下载、mock 占位图/占位视频、ffprobe、SRT 字幕、ffmpeg 按集拼接成片。

目录：<工作区>/media/<project_id>/{assets,frames,videos,exports}/...
HTTP 上以 /media/<相对路径> 暴露给前端。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import mimetypes
import os
import shutil
import struct
import subprocess
import tempfile
import wave
import zlib

from . import db

MEDIA_ROOT = os.environ.get("WORKFLOW_MEDIA") or os.path.join(os.path.dirname(db.WF_ROOT), "media")


class MediaError(Exception):
    pass


def root() -> str:
    os.makedirs(MEDIA_ROOT, exist_ok=True)
    return MEDIA_ROOT


def path_for(pid: str, sub: str, name: str) -> str:
    d = os.path.join(root(), pid, sub)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def url_of(path: str) -> str:
    """本地绝对路径 → /media/... URL；不在 media 目录下的返回空串。"""
    if not path:
        return ""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root()))
    if rel.startswith(".."):
        return ""
    return "/media/" + rel.replace(os.sep, "/")


def data_uri(path: str) -> str:
    """把本地图片转成 data URI，提交给上游当参考图（上游临时 URL 常 24h 过期）。"""
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def is_mock_url(url: str) -> bool:
    return url.startswith("https://mock.local/")


# ---------------- 占位素材（mock 模式下也能走完整条线） ----------------

def _color(seed: str) -> tuple[int, int, int]:
    h = hashlib.md5(seed.encode()).digest()
    return 60 + h[0] % 150, 60 + h[1] % 150, 60 + h[2] % 150


def placeholder_png(path: str, seed: str, w: int = 288, h: int = 512) -> str:
    """纯 Python 写一张纯色 PNG。"""
    r, g, b = _color(seed)
    row = b"\x00" + bytes([r, g, b]) * w
    raw = zlib.compress(row * h, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return path


def ffmpeg_bin() -> str | None:
    return os.environ.get("FFMPEG") or shutil.which("ffmpeg")


def ffprobe_bin() -> str | None:
    p = os.environ.get("FFPROBE") or shutil.which("ffprobe")
    if p:
        return p
    ff = ffmpeg_bin()
    if ff:
        cand = os.path.join(os.path.dirname(ff), "ffprobe" + (".exe" if ff.lower().endswith(".exe") else ""))
        if os.path.exists(cand):
            return cand
    return None


def _run(args: list[str], cwd: str | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, **kw)
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "ignore")[-800:]
        raise MediaError(f"{os.path.basename(args[0])} 失败（{p.returncode}）：{tail}")
    return p


def aspect_size(aspect: str, base: int = 720) -> tuple[int, int]:
    """9:16 → (720, 1280)；16:9 → (1280, 720)；1:1 → (720, 720)。宽高取偶数。"""
    try:
        a, b = [float(x) for x in aspect.split(":")]
    except ValueError:
        a, b = 9.0, 16.0
    if a <= b:
        w, h = base, int(round(base * b / a))
    else:
        w, h = int(round(base * a / b)), base
    return w - w % 2, h - h % 2


async def placeholder_mp4(path: str, seconds: float, seed: str, aspect: str = "9:16") -> bool:
    ff = ffmpeg_bin()
    if not ff:
        return False
    w, h = aspect_size(aspect, 360)
    r, g, b = _color(seed)
    args = [ff, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=0x{r:02x}{g:02x}{b:02x}:s={w}x{h}:r=24:d={seconds:.3f}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", f"{seconds:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", path]
    await asyncio.to_thread(_run, args)
    return True


# ---------------- 探测 ----------------

def probe(path: str) -> dict:
    fp = ffprobe_bin()
    if not fp:
        return {}
    p = _run([fp, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
              "-of", "json", path], timeout=60)
    data = json.loads(p.stdout.decode("utf-8", "ignore") or "{}")
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"duration": float((data.get("format") or {}).get("duration") or 0),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "width": int(v.get("width") or 0), "height": int(v.get("height") or 0)}


def wav_seconds(path: str) -> float:
    """wav 时长（不依赖 ffprobe）；不是 wav 时退回 ffprobe。
    百炼 TTS 返回的是流式写出的 wav，文件头里的长度是占位值（常见 0x7FFFFFFF），
    只信文件头会把几秒的台词算成几小时 → 配音被一律加速到上限并误报超时长。所以和文件实际大小交叉核对。"""
    try:
        with wave.open(path, "rb") as w:
            rate = float(w.getframerate() or 1)
            frame_bytes = max(1, w.getnchannels() * w.getsampwidth())
            nframes = w.getnframes()
        with open(path, "rb") as f:
            head = f.read(4096)
        i = head.find(b"data")
        if i >= 0:
            by_size = max(0, os.path.getsize(path) - (i + 8)) // frame_bytes
            nframes = min(nframes, by_size) if nframes else by_size
        return nframes / rate
    except (wave.Error, EOFError, OSError):
        return float(probe(path).get("duration") or 0)


def tone_wav(path: str, seconds: float, seed: str = "") -> str:
    """模拟配音：一段很轻的提示音（按字数估时长），零成本、不依赖 ffmpeg。"""
    rate = 24000
    n = int(max(0.3, seconds) * rate)
    freq = 180 + sum(seed.encode("utf-8")) % 200
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / 1200, (n - i) / 1200)
        frames += struct.pack("<h", int(2400 * env * math.sin(2 * math.pi * freq * i / rate)))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


# ---------------- 截取 ----------------

async def sample_frames(src: str, out_dir: str, n: int = 4, width: int = 448, prefix: str = "f") -> list[str]:
    """均匀抽 n 帧（避开首尾 5%），缩到 width 宽的 jpg，给质检看。"""
    ff = ffmpeg_bin()
    if not ff:
        raise MediaError("没有 ffmpeg，抽不了帧")
    dur = float(probe(src).get("duration") or 0) or 1.0
    os.makedirs(out_dir, exist_ok=True)
    out = []
    for i in range(n):
        at = dur * (0.05 + 0.9 * (i + 0.5) / n)
        dst = os.path.join(out_dir, f"{prefix}{i + 1}.jpg")
        await asyncio.to_thread(_run, [ff, "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", src,
                                       "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", dst], None, 60)
        if os.path.exists(dst):
            out.append(dst)
    return out


async def thumb(src: str, dst: str, width: int = 448) -> str:
    """参考图缩小成 jpg（发给视觉模型时不传 2K 原图）。"""
    ff = ffmpeg_bin()
    if not ff:
        return src
    await asyncio.to_thread(_run, [ff, "-y", "-loglevel", "error", "-i", src, "-vf", f"scale={width}:-2",
                                   "-q:v", "5", dst], None, 60)
    return dst if os.path.exists(dst) else src


def _dub_track_sync(lines: list[dict], out_path: str, duration: float, gap: float = 0.25) -> dict:
    """把一句句配音按顺序排进镜头时长里：放不下就加速（最多 1.35 倍），仍放不下就截断并标记 overflow。
    lines: [{path, speaker, text}]；返回每句在镜头内的起止时间（字幕用）。"""
    ff = ffmpeg_bin()
    if not ff:
        raise MediaError("没有 ffmpeg，合不了配音轨")
    lines = [dict(x) for x in lines if x.get("path") and os.path.exists(x["path"])]
    if not lines:
        raise MediaError("没有可用的配音片段")
    for x in lines:
        x["raw_seconds"] = round(wav_seconds(x["path"]), 3)
    lead = 0.3
    avail = max(0.5, duration - lead - 0.2)
    total = sum(x["raw_seconds"] for x in lines) + gap * (len(lines) - 1)
    tempo = min(1.35, max(1.0, total / avail))
    t = lead if total / tempo <= avail else 0.1
    args = [ff, "-y", "-loglevel", "error"]
    parts = []
    for i, x in enumerate(lines):
        args += ["-i", x["path"]]
        d = x["raw_seconds"] / tempo
        x["start"], x["end"] = round(t, 3), round(min(duration, t + d), 3)
        ms = int(t * 1000)
        chain = f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo"
        if tempo > 1.001:
            chain += f",atempo={tempo:.3f}"
        parts.append(f"{chain},adelay={ms}|{ms}[d{i}]")
        t += d + gap
    mix = "".join(f"[d{i}]" for i in range(len(lines)))
    parts.append(f"{mix}amix=inputs={len(lines)}:duration=longest:dropout_transition=0:normalize=0,"
                 f"apad,atrim=0:{duration:.3f}[out]")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    _run(args + ["-filter_complex", ";".join(parts), "-map", "[out]", "-c:a", "pcm_s16le", out_path])
    return {"path": out_path, "tempo": round(tempo, 3), "overflow": t - gap > duration + 0.05,
            "lines": [{k: x[k] for k in ("speaker", "voice", "text", "start", "end") if k in x} for x in lines]}


async def dub_track(lines: list[dict], out_path: str, duration: float) -> dict:
    return await asyncio.to_thread(_dub_track_sync, lines, out_path, duration)

async def poster(src: str, dst: str, at: float = 0.5) -> str:
    """抽一帧做封面。"""
    ff = ffmpeg_bin()
    if not ff:
        raise MediaError("没有 ffmpeg")
    await asyncio.to_thread(_run, [ff, "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", src,
                                   "-frames:v", "1", "-q:v", "4", dst], None, 60)
    return dst


def last_frame(src: str, dst: str) -> str:
    """取视频最后一帧（接下一镜的首帧用）。"""
    ff = ffmpeg_bin()
    if not ff:
        raise MediaError("没有 ffmpeg，取不了尾帧")
    _run([ff, "-y", "-loglevel", "error", "-sseof", "-0.3", "-i", src, "-update", "1", "-frames:v", "1",
          "-q:v", "3", dst], None, 60)
    if not os.path.exists(dst):
        raise MediaError("尾帧提取失败")
    return dst


async def extract_audio(src: str, dst: str, start: float, seconds: float) -> str:
    """从视频里截一段音频（m4a/aac），用作角色音色参考。"""
    ff = ffmpeg_bin()
    if not ff:
        raise MediaError("没有找到 ffmpeg，无法截取音色")
    info = probe(src)
    if info and not info.get("has_audio"):
        raise MediaError("这个视频没有音轨，截不出音色")
    await asyncio.to_thread(_run, [ff, "-y", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}", "-t",
                                   f"{seconds:.3f}", "-i", src, "-vn", "-c:a", "aac", "-b:a", "128k", dst],
                            None, 120)
    return dst


# ---------------- 字幕 ----------------

def _ts(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    hh, ms = divmod(ms, 3_600_000)
    mm, ms = divmod(ms, 60_000)
    ss, ms = divmod(ms, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def build_srt(items: list[tuple[float, float, str]]) -> str:
    out, n = [], 0
    for start, end, text in items:
        text = (text or "").strip()
        if not text or end <= start:
            continue
        n += 1
        out.append(f"{n}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
    return "\n".join(out)


# ---------------- 剪辑时间线（EDL / FCPXML） ----------------

FPS = 24


def _tc(seconds: float, fps: int = FPS) -> str:
    f = int(round(seconds * fps))
    hh, f = divmod(f, 3600 * fps)
    mm, f = divmod(f, 60 * fps)
    ss, ff = divmod(f, fps)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def build_edl(title: str, clips: list[dict], fps: int = FPS) -> str:
    """CMX3600 EDL：每个片段一条事件，视频+音频，源从 0 开始。"""
    out = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    t = 0.0
    for i, c in enumerate(clips, start=1):
        d = c["seconds"]
        out.append(f"{i:03d}  AX       AA/V  C        {_tc(0, fps)} {_tc(d, fps)} {_tc(t, fps)} {_tc(t + d, fps)}")
        out.append(f"* FROM CLIP NAME: {c['name']}")
        out.append("")
        t += d
    return "\n".join(out)


def build_fcpxml(title: str, clips: list[dict], w: int, h: int, folder: str, fps: int = FPS) -> str:
    from xml.sax.saxutils import quoteattr

    def r(seconds: float) -> str:
        return f"{int(round(seconds * fps))}/{fps}s"

    assets, spine, t = [], [], 0.0
    for i, c in enumerate(clips, start=1):
        stem = os.path.splitext(c["name"])[0]
        src = quoteattr(f"./{folder}/{c['name']}")
        dur = r(c["seconds"])
        assets.append(f'    <asset id="a{i}" name={quoteattr(stem)} start="0s" duration="{dur}" hasVideo="1" '
                      f'hasAudio="1" format="r1"><media-rep kind="original-media" src={src}/></asset>')
        spine.append(f'          <asset-clip ref="a{i}" offset="{r(t)}" name={quoteattr(stem)} duration="{dur}" start="0s"/>')
        t += c["seconds"]
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE fcpxml>", '<fcpxml version="1.10">', "  <resources>",
        f'    <format id="r1" name="FFVideoFormat{w}x{h}p{fps}" frameDuration="1/{fps}s" width="{w}" height="{h}"/>',
        *assets, "  </resources>", "  <library>", f"    <event name={quoteattr(title)}>",
        f"      <project name={quoteattr(title)}>",
        f'        <sequence format="r1" duration="{r(t)}" tcStart="0s" tcFormat="NDF">', "          <spine>",
        *spine, "          </spine>", "        </sequence>", "      </project>", "    </event>", "  </library>", "</fcpxml>"])


# ---------------- 拼接成片 ----------------

def _mix_bgm(ff: str, video: str, bgm: str, volume: float, out: str) -> None:
    """配乐：循环铺满全片、末尾 2 秒淡出，压在镜头原声下面；字幕轨原样保留。"""
    dur = probe(video).get("duration") or 0
    fade = f",afade=t=out:st={max(0.0, dur - 2):.2f}:d=2" if dur > 3 else ""
    _run([ff, "-y", "-loglevel", "error", "-i", video, "-stream_loop", "-1", "-i", bgm, "-filter_complex",
          f"[1:a]volume={volume:.2f}{fade}[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
          "-map", "0:v", "-map", "[a]", "-map", "0:s?", "-c:v", "copy", "-c:s", "copy", "-c:a", "aac", "-b:a", "192k",
          "-t", f"{dur:.3f}", out])


def _assemble_sync(clips: list[dict], out_path: str, srt_path: str, aspect: str, burn: bool,
                   bgm: str = "", bgm_volume: float = 0.25, dub_mode: str = "off", orig_volume: float = 0.35) -> dict:
    ff = ffmpeg_bin()
    if not ff:
        raise MediaError("没有找到 ffmpeg：请安装 ffmpeg 或设置环境变量 FFMPEG")
    if not clips:
        raise MediaError("没有可拼接的镜头（先把视频跑完并下载到本地）")
    w, h = aspect_size(aspect, 720)
    work = tempfile.mkdtemp(prefix="assemble_")
    try:
        normed, subs, t = [], [], 0.0
        for i, c in enumerate(clips):
            info = probe(c["path"])
            dur = info.get("duration") or float(c.get("seconds") or 0) or 5.0
            norm = os.path.join(work, f"n{i:03d}.mp4")
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p")
            args = [ff, "-y", "-loglevel", "error", "-i", c["path"]]
            dub = c.get("dub") if dub_mode in ("mix", "replace") else ""
            if dub and os.path.exists(dub):
                orig = "0:a"
                if not info.get("has_audio", False):
                    args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
                    orig, di = "1:a", 2
                else:
                    di = 1
                args += ["-i", dub]
                fmt = "aresample=44100,aformat=channel_layouts=stereo"
                if dub_mode == "replace":
                    fc = f"[{di}:a]{fmt},apad[a]"
                else:   # 配音为主，原声（音效/环境）压低垫底
                    fc = (f"[{orig}]{fmt},volume={orig_volume:.2f}[o];[{di}:a]{fmt},apad[d];"
                          f"[o][d]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
                args += ["-filter_complex", fc, "-map", "0:v:0", "-map", "[a]"]
            elif not info.get("has_audio", False):
                args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "0:v:0", "-map", "1:a:0"]
            else:
                args += ["-map", "0:v:0", "-map", "0:a:0"]
            args += ["-t", f"{dur:.3f}", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                     "-c:a", "aac", "-ar", "44100", "-ac", "2", norm]
            _run(args)
            normed.append(norm)
            if c.get("subs"):        # 有配音：字幕按每句配音的真实起止时间
                for s0, s1, txt in c["subs"]:
                    if s0 < dur and txt:
                        subs.append((t + s0, t + min(s1, dur - 0.05), txt))
            elif c.get("dialogue"):
                subs.append((t + 0.1, t + dur - 0.1, c["dialogue"]))
            t += dur
        listing = os.path.join(work, "list.txt")
        with open(listing, "w", encoding="utf-8") as f:
            for n in normed:
                f.write(f"file '{os.path.basename(n)}'\n")
        joined = os.path.join(work, "joined.mp4")
        _run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
              "-c", "copy", "joined.mp4"], cwd=work)
        srt = build_srt(subs)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if srt and burn:
            shutil.copy(srt_path, os.path.join(work, "subs.srt"))
            _run([ff, "-y", "-loglevel", "error", "-i", "joined.mp4", "-vf",
                  "subtitles=subs.srt:force_style='Fontsize=16,Outline=1,MarginV=40'",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy", "final.mp4"], cwd=work)
            shutil.move(os.path.join(work, "final.mp4"), out_path)
        elif srt:
            shutil.copy(srt_path, os.path.join(work, "subs.srt"))
            _run([ff, "-y", "-loglevel", "error", "-i", "joined.mp4", "-i", "subs.srt",
                  "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text",
                  "-metadata:s:s:0", "language=chi", "final.mp4"], cwd=work)
            shutil.move(os.path.join(work, "final.mp4"), out_path)
        else:
            shutil.move(joined, out_path)
        if bgm and os.path.exists(bgm):
            mixed = os.path.join(work, "with_bgm.mp4")
            _mix_bgm(ff, out_path, bgm, bgm_volume, mixed)
            shutil.move(mixed, out_path)
        return {"path": out_path, "srt_path": srt_path, "seconds": round(t, 3), "clips": len(clips),
                "subtitles": len(subs), "burned": bool(srt and burn), "bgm": bool(bgm),
                "dubbed": sum(1 for c in clips if c.get("dub")) if dub_mode in ("mix", "replace") else 0}
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def assemble(clips: list[dict], out_path: str, srt_path: str, aspect: str = "9:16",
                   burn: bool = False, bgm: str = "", bgm_volume: float = 0.25,
                   dub_mode: str = "off", orig_volume: float = 0.35) -> dict:
    return await asyncio.to_thread(_assemble_sync, clips, out_path, srt_path, aspect, burn, bgm, bgm_volume,
                                   dub_mode, orig_volume)
