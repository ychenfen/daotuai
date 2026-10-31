"""零成本核对“模型是否已开通”：对每个候选模型发一个参数故意写错的请求。
已开通 → 400 参数错误；没开通 → 404/403。请求不会生成任何内容，不扣费。

    py -3.12 tools/probe_access.py
"""
from __future__ import annotations

import os
import re
import sys

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ARK = "https://ark.cn-beijing.volces.com/api/v3"
DS = "https://dashscope.aliyuncs.com"


def verdict(r: httpx.Response) -> str:
    t = r.text[:220].replace("\n", " ")
    if r.status_code == 400 or "InvalidParameter" in t or "invalid" in t.lower() and r.status_code < 500:
        if "NotFound" not in t and "not exist" not in t and "AccessDenied" not in t:
            return f"可用（{r.status_code} 参数错误，符合预期）"
    return f"不可用 {r.status_code}：{t}"


def main() -> int:
    ark, ds = os.environ.get("ARK_API_KEY", ""), os.environ.get("DASHSCOPE_API_KEY", "")
    with httpx.Client(timeout=30, trust_env=False) as c:
        if ark:
            h = {"Authorization": f"Bearer {ark}"}
            ids = [m["id"] for m in c.get(f"{ARK}/models", headers=h).json().get("data", [])]
            print("== 火山方舟：视频")
            for m in [x for x in ids if "seedance" in x]:
                r = c.post(f"{ARK}/contents/generations/tasks", headers=h,
                           json={"model": m, "content": [{"type": "text", "text": "test"}], "duration": 999})
                print(f"  {m}: {verdict(r)}")
            print("== 火山方舟：图像")
            for m in [x for x in ids if re.search("seedream|seededit", x)]:
                r = c.post(f"{ARK}/images/generations", headers=h, json={"model": m, "prompt": "test", "size": "1x1"})
                print(f"  {m}: {verdict(r)}")
            print("== 火山方舟：文本（抽查）")
            for m in [x for x in ids if re.search(r"^(doubao-seed-1-6|doubao-1-5-pro-32k|deepseek-v3-2|deepseek-v4)", x)][:12]:
                r = c.post(f"{ARK}/chat/completions", headers=h,
                           json={"model": m, "messages": [{"role": "user", "content": "x"}], "temperature": 9})
                print(f"  {m}: {verdict(r)}")
        if ds:
            h = {"Authorization": f"Bearer {ds}", "X-DashScope-Async": "enable"}
            ids = [m["id"] for m in c.get(f"{DS}/compatible-mode/v1/models", headers={"Authorization": f"Bearer {ds}"}).json().get("data", [])]
            print("== 百炼：模型列表里的图像/视频")
            print("  ", ", ".join(x for x in ids if re.search(r"wan|image|wanx|kling|video", x)))
            print("== 百炼：视频（万相）")
            cands = ["wan3.0-video", "wan3.0-video-prime"] + [x for x in ids if re.search(r"^wan", x) and re.search(r"t2v|i2v|r2v|video", x)]
            for m in list(dict.fromkeys(cands))[:12]:
                r = c.post(f"{DS}/api/v1/services/aigc/video-generation/video-synthesis", headers=h,
                           json={"model": m, "input": {"prompt": "test"}, "parameters": {"duration": 999}})
                print(f"  {m}: {verdict(r)}")
            print("== 百炼：图像")
            for m in [x for x in ids if re.search(r"^(qwen-image|wan.*t2i|wanx)", x)][:12]:
                r = c.post(f"{DS}/api/v1/services/aigc/text2image/image-synthesis", headers=h,
                           json={"model": m, "input": {"prompt": "test"}, "parameters": {"size": "1*1"}})
                print(f"  {m}: {verdict(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
