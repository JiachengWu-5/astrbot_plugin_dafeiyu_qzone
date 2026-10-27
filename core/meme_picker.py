"""Pick small meme / collected images for QZone auto publish."""
from __future__ import annotations

import random
from pathlib import Path

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Prefer small sticker packs; large GIF packs last / size-filtered.
PREFERRED_PACKS = (
    "arknights-2nd-stickers",
    "arknights-wechat-all",
    "arknights-anecdote",
    "deepseek-fanart-meme-pack",
    "blue-fat-fish-meme-pack",
)

# QZone cgi_upload_image is brittle with multi-MB base64 payloads.
DEFAULT_MAX_BYTES = 300 * 1024


def meme_manager_root() -> Path:
    return Path(get_astrbot_plugin_data_path()) / "meme_manager"


def _iter_image_files(root: Path):
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            yield path


def _collect_candidates(
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    prefer_packs: tuple[str, ...] = PREFERRED_PACKS,
) -> list[Path]:
    root = meme_manager_root()
    packs_dir = root / "packs"
    inbox_dir = root / "auto_collect_inbox"

    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= 0 or size > max_bytes:
            return
        key = str(path.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    # 1) Preferred packs first (still collected into one pool for random pick,
    #    but we weight by scanning preferred packs before others).
    preferred_files: list[Path] = []
    other_files: list[Path] = []

    if packs_dir.is_dir():
        preferred_set = {p.lower() for p in prefer_packs}
        for pack in packs_dir.iterdir():
            if not pack.is_dir():
                continue
            bucket = preferred_files if pack.name.lower() in preferred_set else other_files
            for path in _iter_image_files(pack):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if 0 < size <= max_bytes:
                    bucket.append(path)

    for path in preferred_files + other_files:
        add(path)

    # 2) Auto-collected inbox (approved / pending images on disk)
    for path in _iter_image_files(inbox_dir):
        add(path)

    return candidates


def pick_meme_image_paths(
    count: int = 1,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    prefer_packs: tuple[str, ...] | None = None,
) -> list[str]:
    """Return absolute local paths to small meme images (may be empty)."""
    if count <= 0:
        return []

    candidates = _collect_candidates(
        max_bytes=max_bytes,
        prefer_packs=prefer_packs or PREFERRED_PACKS,
    )
    if not candidates:
        logger.warning("[qzone-meme] no eligible meme images under meme_manager")
        return []

    # Prefer preferred-pack files when present: filter/reorder by pack name.
    preferred = []
    others = []
    prefer_set = {p.lower() for p in (prefer_packs or PREFERRED_PACKS)}
    for path in candidates:
        pack_name = ""
        parts = path.parts
        if "packs" in parts:
            i = parts.index("packs")
            if i + 1 < len(parts):
                pack_name = parts[i + 1].lower()
        (preferred if pack_name in prefer_set else others).append(path)

    pool = preferred or others
    k = min(count, len(pool))
    chosen = random.sample(pool, k=k)
    paths = [str(p.resolve()) for p in chosen]
    logger.info(f"[qzone-meme] picked {len(paths)} image(s): {paths}")
    return paths


def pick_meme_images_as_bytes(
    count: int = 1,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[bytes]:
    """Read picked meme files into bytes (skips unreadable files)."""
    out: list[bytes] = []
    for path_str in pick_meme_image_paths(count, max_bytes=max_bytes):
        try:
            data = Path(path_str).read_bytes()
        except OSError as e:
            logger.warning(f"[qzone-meme] failed to read {path_str}: {e}")
            continue
        if data:
            out.append(data)
    return out
