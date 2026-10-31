"""LibTV 连接层测试：对着进程内的假 LibTV 服务走完 登录授权 → MCP 会话 → 上传参考图 → 出片 → 轮询 → 续期 → 退出。
不联网、不花钱。  python tests/libtv_test.py"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="libtv_test_")
os.environ["AGG_DB"] = os.path.join(TMP, "agg.db")
os.environ["LIBTV_MCP_URL"] = "http://libtv.test/mcp"
os.environ["LIBTV_ISSUER"] = "http://libtv.test/"
os.environ["LIBTV_REDIRECT_URI"] = "http://127.0.0.1:8010/libtv/callback"
os.environ.pop("AGG_FORCE_MOCK", None)
os.environ["DAOTU_ENV_FILE"] = os.path.join(TMP, "none.env")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging  # noqa: E402

import httpx  # noqa: E402

for _n in ("httpx", "agg"):
    logging.getLogger(_n).setLevel(logging.WARNING)

import fake_libtv  # noqa: E402
from app import libtv  # noqa: E402
from app.main import app  # noqa: E402

libtv._transport = httpx.ASGITransport(app=fake_libtv.app)

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))


PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake reference").decode()


async def main() -> int:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agg") as c, \
            httpx.AsyncClient(transport=libtv._transport, base_url="http://libtv.test") as lt:
        print("\n[A] 未连接")
        st = (await c.get("/libtv/status")).json()
        check("未授权", st["authorized"] is False, json.dumps(st))
        ms = {m["id"]: m for m in (await c.get("/v1/models")).json()["data"]}
        check("LibTV 模型在目录里、未连接时不是真实线路", "libtv-wan3.0" in ms and not ms["libtv-wan3.0"]["live"], "")
        r = await c.get("/libtv/doctor")
        check("未连接时 doctor 报 401 并提示去连接", r.status_code == 401 and "连接" in r.json()["error"], r.text[:200])

        print("\n[B] 浏览器授权（注册 → PKCE → 回调换令牌）")
        r = await c.get("/libtv/login")
        loc = r.headers.get("location", "")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
        check("跳转到 LibTV 授权页（PKCE S256、带 resource 与 scope）", r.status_code == 307 and loc.startswith("http://libtv.test/authorize")
              and q["code_challenge_method"] == ["S256"] and q["resource"] == ["http://libtv.test/mcp"]
              and "libtv:task:write" in q["scope"][0], loc[:200])
        r2 = await lt.get(loc.replace("http://libtv.test", ""))      # 用户在浏览器里点“授权”
        cb = r2.headers["location"]
        bad = await c.get("/libtv/callback?code=x&state=wrong")
        check("state 不对拒绝", "失败" in bad.text, bad.text[:120])
        r3 = await c.get(cb.replace("http://127.0.0.1:8010", ""))
        check("回调页显示已连接", r3.status_code == 200 and "已连接" in r3.text, r3.text[:200])
        st = (await c.get("/libtv/status")).json()
        check("状态：已授权、令牌有效", st["authorized"] and st["expires_in_s"] > 3000, json.dumps(st))
        auth = json.load(open(os.path.join(TMP, "libtv_auth.json"), encoding="utf-8"))
        check("令牌只存在本机数据目录、没有残留登录中间态", auth.get("refresh_token") and "pending" not in auth, list(auth))
        ms = {m["id"]: m for m in (await c.get("/v1/models")).json()["data"]}
        check("连接后 LibTV 模型变成真实线路", ms["libtv-wan3.0"]["live"] and ms["libtv-seedance-2.5"]["live"], "")

        print("\n[C] MCP 会话：工具列表 + doctor")
        d = (await c.get("/libtv/doctor")).json()
        check("列出工具并通过准入检查", d["ok"] and "generate_video" in d["tools"]
              and d["doctor"]["executionAdmission"]["allowed"], json.dumps(d, ensure_ascii=False)[:200])
        tools = (await c.get("/libtv/tools")).json()["tools"]
        check("工具 schema 可查（用于校准）", any(t["name"] == "generate_video" and t["inputSchema"]["required"] for t in tools), "")

        print("\n[D] 出片：建画布 → 上传参考图 → 选模型 → generate_video → 轮询")
        await c.post("/v1/wallet/topup", json={"amount_rmb": 10}, headers={"X-User-Id": "studio"})
        body = {"model": "libtv-wan3.0", "prompt": "【参考】图1：孔宣\n图1（孔宣）抬头看红绿灯", "resolution": "480P",
                "duration": 5, "aspect_ratio": "9:16", "project": "妖王入世·股神",
                "images": [{"url": PNG, "role": "reference_image"}]}
        v = (await c.post("/v1/videos/generations", json=body, headers={"X-User-Id": "studio"})).json()
        check("提交返回任务号（后台跑 MCP 流程）", v.get("status") == "submitted" and v["routing"]["provider"] == "libtv",
              json.dumps(v, ensure_ascii=False)[:200])
        done = None
        for _ in range(40):
            done = (await c.get(f"/v1/videos/{v['id']}", headers={"X-User-Id": "studio"})).json()
            if done["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.05)
        check("轮询到成片 URL", done and done["status"] == "succeeded" and done["url"].endswith(".mp4"), json.dumps(done)[:300])
        calls = {n: a for n, a in fake_libtv.S["calls"]}
        gv = calls.get("generate_video") or {}
        node = (gv.get("referenceImageNodes") or [None])[0]
        check("画布按项目名创建（带稳定幂等键）", calls.get("create_project", {}).get("name") == "妖王入世·股神"
              and calls["create_project"]["idempotencyKey"].startswith("canvas-"), json.dumps(calls.get("create_project")))
        check("参考图真的上传了字节并成了节点", node and node in fake_libtv.S["nodes"]
              and any(fake_libtv.S["uploads"].values()), json.dumps(gv, ensure_ascii=False)[:200])
        check("提示词里的 图1 换成 {{Node id}}", f"{{{{Node {node}}}}}（孔宣）" in gv.get("prompt", ""), gv.get("prompt", ""))
        check("用实时契约里的精确模型与参数（只填契约里有的）", gv.get("model") == "wan-3.0-video"
              and gv.get("params") == {"duration": 5, "resolution": "480p", "aspectRatio": "9:16"}, json.dumps(gv.get("params")))
        check("run=true 且有幂等键", gv.get("run") is True and gv.get("idempotencyKey", "").startswith("vid-lt_"), "")

        print("\n[E] 令牌过期自动续期")
        a = json.load(open(os.path.join(TMP, "libtv_auth.json"), encoding="utf-8"))
        a["expires_at"] = time.time() - 10
        json.dump(a, open(os.path.join(TMP, "libtv_auth.json"), "w", encoding="utf-8"))
        n0 = sum(1 for n, _ in fake_libtv.S["calls"] if n == "refresh")
        d = (await c.get("/libtv/doctor")).json()
        n1 = sum(1 for n, _ in fake_libtv.S["calls"] if n == "refresh")
        check("过期后自动用刷新令牌续期", d["ok"] and n1 == n0 + 1, f"{n0}->{n1}")

        print("\n[F] 参数校准保护")
        m = libtv.MCP()
        await m.list_tools()
        try:
            m.build_args("generate_video", {"prompt": "x"})
            ok = False
        except libtv.LibTVError as e:
            ok = "缺少必填字段" in str(e) and e.detail and "schema" in e.detail
        check("缺必填字段时报错并带上真实 schema（不瞎猜）", ok, "")

        print("\n[G] 退出")
        r = (await c.post("/libtv/logout")).json()
        st = (await c.get("/libtv/status")).json()
        check("退出后未授权、LibTV 模型回到非真实", not r["authorized"] and not st["authorized"]
              and not {m["id"]: m for m in (await c.get("/v1/models")).json()["data"]}["libtv-wan3.0"]["live"], "")

    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except Exception:
        traceback.print_exc()
        code = 2
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
