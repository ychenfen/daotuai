"""部署冒烟：用真正的启动器(run.py)拉起两个服务（临时库、临时素材目录、全部模拟渠道、备用端口），
走一遍“建项目 → 写剧本 → 一键成片”，检查出成片，然后把整棵进程树关掉。不花钱，不碰正式数据。

    py -3.12 tools/smoke.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGG_PORT, WF_PORT = 18010, 18011
BASE = f"http://127.0.0.1:{WF_PORT}"
SCRIPT = """第1集
1-1 日 外 金鸡岭
人物：萤姐、猪婆龙
△五只青鸾鸟俯视金鸡岭<风声>。
猪婆龙（指向彩蛋）：那里！
萤姐（冷笑）：你们仨是从哪座庙里蹦出来的？
"""
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def call(method: str, path: str, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)


def kill_tree(p: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        os.killpg(p.pid, signal.SIGTERM)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="daotu_smoke_")
    env = dict(os.environ, AGG_FORCE_MOCK="1", WORKFLOW_LLM="mock", AGG_DB=os.path.join(tmp, "agg.db"),
               WORKFLOW_DB=os.path.join(tmp, "wf.db"), WORKFLOW_MEDIA=os.path.join(tmp, "media"),
               WORKFLOW_POLL_INTERVAL_S="1", WORKFLOW_WAIT_INTERVAL_S="0.5", DAOTU_ENV_FILE=os.path.join(tmp, "x.env"))
    kw = {"creationflags": 0x00000200} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "run.py"), "--mock", "--no-browser",
                          "--agg-port", str(AGG_PORT), "--wf-port", str(WF_PORT)], cwd=ROOT, env=env, **kw)
    ok = True
    try:
        for _ in range(60):
            try:
                st, h = call("GET", "/healthz", timeout=3)
                if st == 200 and h.get("aggregator"):
                    break
            except Exception:
                time.sleep(1)
        else:
            print("[x] 服务没起来"); return 1
        print("[ok] healthz", json.dumps(h, ensure_ascii=False))
        st, ui = call("GET", "/ui/")
        print("[ok] 界面" if st == 200 and b"app.js" in ui else "[x] 界面"); ok &= st == 200
        _, proj = call("POST", "/projects", {"name": "冒烟测试", "settings": {"budget_rmb": 100}})
        pid = proj["id"]
        call("POST", f"/projects/{pid}/script", {"text": SCRIPT})
        _, t = call("POST", f"/projects/{pid}/run?wait=true", timeout=600)
        steps = [f"{s['name']}:{s['status']}" for s in t.get("steps", [])]
        print("[ok]" if t["status"] == "succeeded" else "[x]", "一键成片", t["status"], " / ".join(steps), t.get("error", ""))
        ok &= t["status"] == "succeeded"
        _, pj = call("GET", f"/projects/{pid}")
        finals = [e["final_url"] for e in pj["episodes"]]
        print("[ok]" if all(finals) else "[x]", "成片", finals, "状态", pj["status"])
        ok &= all(finals)
        if all(finals):
            st, _ = call("GET", finals[0])
            print("[ok] 成片可下载" if st == 200 else "[x] 成片下载失败"); ok &= st == 200
    except Exception as e:  # 冒烟脚本：任何异常都算失败并打印
        print("[x] 异常", repr(e)); ok = False
    finally:
        kill_tree(p)
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
