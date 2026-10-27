# -*- coding: utf-8 -*-
"""Local cache paths for the QZone plugin.

Default keeps the D: layout used by 大肥鱼 deployments. Override with:
- env DAFEIYU_QZONE_CACHE
- plugin config key cache_root (when passed into ensure_cache_dirs)
See README.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT = Path(r"D:\AstrBot\qzone_cache")


def resolve_cache_root(configured: str | None = None) -> Path:
    if configured and str(configured).strip():
        return Path(str(configured).strip())
    env = os.environ.get("DAFEIYU_QZONE_CACHE", "").strip()
    if env:
        return Path(env)
    return _DEFAULT


CACHE_ROOT = resolve_cache_root()
TMP_IMAGES_DIR = CACHE_ROOT / "tmp_images"
FEEDBACK_DIR = CACHE_ROOT / "feedback"


def ensure_cache_dirs(configured: str | None = None) -> Path:
    """Create cache dirs; optionally re-bind module-level path aliases."""
    global CACHE_ROOT, TMP_IMAGES_DIR, FEEDBACK_DIR
    root = resolve_cache_root(configured)
    TMP_IMAGES_DIR = root / "tmp_images"
    FEEDBACK_DIR = root / "feedback"
    CACHE_ROOT = root
    TMP_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return root
