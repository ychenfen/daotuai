"""一键启动：聚合层(8010) + 工作流(8011)，就绪后打开浏览器。Ctrl+C 一起停。

    py -3.12 run.py            # 正常启动
    py -3.12 run.py --mock     # 全部走模拟渠道（不花钱，演示/培训用）
    py -3.12 run.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(ROOT, "data", "logs")


def _load_dotenv() -> None:
    env_path = os.path.join(ROOT, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                cur = os.environ.get(k, "")
                if not cur or "你的" in cur:
                    os.environ[k] = v


def wait_ok(url: str, timeout: float = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="所有渠道走 mock，不花钱")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--agg-port", type=int, default=8010)
    ap.add_argument("--wf-port", type=int, default=8011)
    a = ap.parse_args()

    _load_dotenv()
    os.makedirs(LOGS, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               AGG_BASE=f"http://127.0.0.1:{a.agg_port}")
    if a.mock:
        env["AGG_FORCE_MOCK"] = "1"
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

    procs = []
    for name, cwd, app, port in (("aggregator", "aggregator", "app.main:app", a.agg_port),
                                 ("workflow", "workflow", "wf.main:app", a.wf_port)):
        log = open(os.path.join(LOGS, f"{name}.log"), "a", encoding="utf-8")
        p = subprocess.Popen([sys.executable, "-m", "uvicorn", app, "--host", a.host, "--port", str(port)],
                             cwd=os.path.join(ROOT, cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                             creationflags=flags)
        procs.append((name, p, port))
        if not wait_ok(f"http://127.0.0.1:{port}/healthz"):
            print(f"[x] {name} 没起来，看日志：{os.path.join(LOGS, name + '.log')}")
            for _, q, _ in procs:
                q.terminate()
            return 1
        print(f"[ok] {name}  http://127.0.0.1:{port}")

    url = f"http://127.0.0.1:{a.wf_port}/"
    print(f"\n工作台：{url}\n日志：{LOGS}\n按 Ctrl+C 停止。")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        while all(p.poll() is None for _, p, _ in procs):
            time.sleep(1)
        for name, p, _ in procs:
            if p.poll() is not None:
                print(f"[x] {name} 退出了（{p.returncode}），看日志：{os.path.join(LOGS, name + '.log')}")
    except KeyboardInterrupt:
        pass
    finally:
        for _, p, _ in procs:
            if p.poll() is None:
                p.terminate()
        for _, p, _ in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
