"""小工具：ID、短哈希、图片尺寸。"""
from __future__ import annotations

import hashlib
import uuid

SIZES = {
    "16:9": {"1K": (1280, 720), "2K": (2560, 1440), "4K": (3840, 2160)},
    "9:16": {"1K": (720, 1280), "2K": (1440, 2560), "4K": (2160, 3840)},
    "1:1": {"1K": (1024, 1024), "2K": (2048, 2048), "4K": (4096, 4096)},
    "4:3": {"1K": (1280, 960), "2K": (2560, 1920), "4K": (4096, 3072)},
    "3:4": {"1K": (960, 1280), "2K": (1920, 2560), "4K": (3072, 4096)},
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def short_hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


def image_size(aspect: str, res: str) -> str:
    w, h = SIZES.get(aspect, SIZES["16:9"]).get(res.upper(), SIZES.get(aspect, SIZES["16:9"])["2K"])
    return f"{w}x{h}"
