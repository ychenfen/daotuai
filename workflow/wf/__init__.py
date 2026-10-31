"""工作流包。导入时先读工作区根目录的 .env（已存在的环境变量优先，不覆盖）。"""
from __future__ import annotations

import os


def _load_env_file() -> None:
    path = os.environ.get("DAOTU_ENV_FILE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file()
