# -*- coding: utf-8 -*-
"""Resolve QZone auto-post images: cover -> related search -> local meme.

Downloaded files live under the configured cache tmp_images dir and MUST be
cleaned up after publish (success or fail). Local meme pack files are never deleted.

Policy:
- Web materials (B站 / 小红书 / 网易云 / mix): prefer that item's own cover/attached media.
- If cover missing or download fails: search with distinctive title/source keywords.
- Never fall back to unrelated local memes when a source cover URL existed.
- Local meme only for chat / no-material posts (or theme with no cover+no title).
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from astrbot.api import logger
from .cache_paths import TMP_IMAGES_DIR, ensure_cache_dirs

from .meme_picker import DEFAULT_MAX_BYTES, pick_meme_image_paths

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_WEB_SOURCES = frozenset(
    {
        "bilibili",
        "xiaohongshu",
        "xhs",
        "netease",
        "netease_music",
        "music163",
    }
)
_WEB_MODES = frozenset({"bilibili", "xiaohongshu", "netease", "mix"})


def tmp_image_dir() -> Path:
    ensure_cache_dirs()
    TMP_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_IMAGES_DIR


def cleanup_temp_images(paths: list[str] | None) -> None:
    """Delete only temp downloads; never touch meme packs."""
    if not paths:
        return
    tmp_root = tmp_image_dir().resolve()
    for p in paths:
        try:
            path = Path(p)
            resolved = path.resolve()
            # Only delete files under our temp dir
            if tmp_root not in resolved.parents and resolved.parent != tmp_root:
                continue
            if resolved.is_file():
                resolved.unlink(missing_ok=True)
                logger.info(f"[qzone-image] cleaned temp: {resolved}")
        except Exception as e:
            logger.debug(f"[qzone-image] cleanup skip {p}: {e}")


def _guess_ext(url: str, content_type: str = "") -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "gif" in ct:
        return ".gif"
    if "webp" in ct:
        return ".webp"
    path = urlparse(url).path.lower()
    for ext in IMAGE_EXTS:
        if path.endswith(ext):
            return ext
    return ".jpg"


def _referer_for_url(url: str, material: dict[str, Any] | None = None) -> str:
    u = (url or "").lower()
    src = str((material or {}).get("source") or "").lower()
    page = str((material or {}).get("url") or "").strip()
    if "hdslb.com" in u or "bilibili.com" in u or src == "bilibili":
        return page if "bilibili.com" in page.lower() else "https://www.bilibili.com/"
    if "music.126.net" in u or "music.163.com" in u or src in (
        "netease",
        "netease_music",
        "music163",
    ):
        return "https://music.163.com/"
    if "xiaohongshu" in u or "xhscdn" in u or "sns-webpic" in u or src in (
        "xiaohongshu",
        "xhs",
    ):
        return "https://www.xiaohongshu.com/"
    if page.startswith("http"):
        return page
    return "https://www.bing.com/"


def _cover_url_candidates(url: str) -> list[str]:
    """For bilibili CDN covers, also try resized variants under size cap."""
    u = (url or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    out = [u]
    if "hdslb.com" in u or "bilibili.com" in u:
        base = u.split("@", 1)[0]
        for suf in ("@400w_400h.jpg", "@320w_320h.jpg", "@240w_240h.jpg"):
            cand = base + suf
            if cand not in out:
                out.append(cand)
    # NetEase album covers often accept ?param=WxH
    if "music.126.net" in u or "music.163.com" in u:
        base = u.split("?", 1)[0]
        for suf in ("?param=400y400", "?param=300y300", "?param=200y200"):
            cand = base + suf
            if cand not in out:
                out.append(cand)
    return out


def _to_qzone_jpeg(data: bytes, max_bytes: int) -> bytes | None:
    """Normalize any decodable image to JPEG under max_bytes for QZone upload."""
    try:
        from io import BytesIO
        from PIL import Image
    except Exception as e:
        logger.debug(f"[qzone-image] PIL unavailable: {e}")
        # fallback: accept already-small jpeg/png/gif/bmp
        if len(data) <= max_bytes and (
            data.startswith(b"\xff\xd8")
            or data.startswith(b"\x89PNG")
            or data.startswith(b"GIF8")
            or data.startswith(b"BM")
        ):
            return data
        return None
    try:
        img = Image.open(BytesIO(data))
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            alpha = img.split()[-1] if img.mode in ("RGBA", "LA") else None
            bg.paste(img, mask=alpha)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # shrink until under max_bytes
        w, h = img.size
        for quality in (85, 75, 65, 55, 45):
            scale = 1.0
            while True:
                tw, th = max(1, int(w * scale)), max(1, int(h * scale))
                frame = img if scale == 1.0 else img.resize((tw, th), Image.Resampling.LANCZOS)
                buf = BytesIO()
                frame.save(buf, format="JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if len(out) <= max_bytes:
                    return out
                if tw <= 160 or th <= 160:
                    break
                scale *= 0.75
        return None
    except Exception as e:
        logger.warning(f"[qzone-image] jpeg convert fail: {e}")
        return None


def _download_to_temp(
    url: str,
    max_bytes: int,
    *,
    material: dict[str, Any] | None = None,
) -> str | None:
    """Download image URL into tmp dir, convert to JPEG <= max_bytes when needed."""
    referer = _referer_for_url(url, material)
    for u in _cover_url_candidates(url):
        try:
            req = Request(
                u,
                headers={
                    "User-Agent": UA,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer": referer,
                },
            )
            with urlopen(req, timeout=15) as resp:
                # Allow reading a bit over max so we can recompress with PIL
                data = resp.read(max(max_bytes * 8, 2_000_000))
                ctype = resp.headers.get("Content-Type") or ""
            if not data:
                continue
            if data.lstrip().startswith((b"<!DOCTYPE", b"<html", b"{", b"[")):
                logger.info(f"[qzone-image] not an image (html/json) url={u[:80]}")
                continue
            jpeg = _to_qzone_jpeg(data, max_bytes)
            if not jpeg:
                logger.info(f"[qzone-image] cannot fit under {max_bytes} url={u[:80]}")
                continue
            name = f"dl_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
            dest = tmp_image_dir() / name
            dest.write_bytes(jpeg)
            logger.info(f"[qzone-image] saved {len(jpeg)} bytes -> {dest} from {u[:80]}")
            return str(dest.resolve())
        except Exception as e:
            logger.debug(f"[qzone-image] download try fail {u[:60]}: {e}")
            continue
    return None


def _bing_image_urls(query: str, limit: int = 8) -> list[str]:
    q = (query or "").strip()
    if not q:
        return []
    url = "https://www.bing.com/images/async?" + urlencode(
        {"q": q, "first": "0", "count": str(max(limit, 10)), "mmasync": "1"}
    )
    req = Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "replace")
    urls: list[str] = []
    # murl is original image URL in Bing async HTML
    for m in re.finditer(r"murl&quot;:&quot;(https?://[^&\"']+)", raw):
        u = html.unescape(m.group(1)).replace("\\u0026", "&").replace("\\/", "/")
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    if not urls:
        for m in re.finditer(r'"murl"\s*:\s*"(https?://[^"]+)"', raw):
            u = m.group(1).encode("utf-8").decode("unicode_escape")
            if u not in urls:
                urls.append(u)
            if len(urls) >= limit:
                break
    return urls


def _ddg_image_urls(query: str, limit: int = 8) -> list[str]:
    """Best-effort DuckDuckGo image endpoint (may be blocked)."""
    q = (query or "").strip()
    if not q:
        return []
    # vqd bootstrap
    page_url = "https://duckduckgo.com/?" + urlencode({"q": q, "iax": "images", "ia": "images"})
    req = Request(page_url, headers={"User-Agent": UA})
    with urlopen(req, timeout=12) as resp:
        page = resp.read().decode("utf-8", "replace")
    m = re.search(r"vqd=['\"]([^'\"]+)['\"]", page)
    if not m:
        return []
    vqd = m.group(1)
    api = "https://duckduckgo.com/i.js?" + urlencode(
        {"l": "us-en", "o": "json", "q": q, "vqd": vqd, "f": ",,,", "p": "1"}
    )
    req2 = Request(api, headers={"User-Agent": UA, "Referer": "https://duckduckgo.com/"})
    with urlopen(req2, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    urls = []
    for row in data.get("results") or []:
        u = str(row.get("image") or "").strip()
        if u.startswith("http") and u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def search_related_image_urls(query: str, limit: int = 8) -> list[str]:
    q = re.sub(r"[\s\|\[\]【】]+", " ", (query or "")).strip()
    if len(q) > 48:
        q = q[:48]
    for fn in (_bing_image_urls, _ddg_image_urls):
        try:
            urls = fn(q, limit=limit)
            if urls:
                logger.info(f"[qzone-image] search via {fn.__name__}: {len(urls)} urls for {q!r}")
                return urls
        except Exception as e:
            logger.debug(f"[qzone-image] {fn.__name__} fail: {e}")
    return []


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _cover_from_material(material: dict[str, Any] | None) -> str:
    if not material:
        return ""
    for key in ("cover", "image_url", "pic", "img"):
        v = str(material.get(key) or "").strip()
        if v.startswith("//"):
            v = "https:" + v
        if v.startswith("http"):
            return v
    return ""


def should_try_cover(mode: str, material: dict[str, Any] | None) -> bool:
    """True when this post should prefer the source item's own cover/media."""
    material = material or {}
    if mode == "chat":
        return False
    cover = _cover_from_material(material)
    if not cover:
        return False
    src = str(material.get("source") or "").strip().lower()
    if mode in _WEB_MODES or src in _WEB_SOURCES:
        return True
    # Any non-chat material that already carries a cover URL
    return bool(src and src not in ("", "chat", "none"))


def build_image_search_query(mode: str, material: dict[str, Any] | None) -> str:
    """Build a distinctive search query tied to this post's material."""
    material = material or {}
    src = str(material.get("source") or mode or "").strip().lower()
    title = str(material.get("title") or "").strip()
    extra = str(material.get("extra") or "").strip()
    url = str(material.get("url") or "").strip()

    # strip very common punctuation noise but keep Chinese keywords
    title_clean = re.sub(r"[《》【】\[\]（）()|｜]+", " ", title)
    title_clean = re.sub(r"\s+", " ", title_clean).strip()

    parts: list[str] = []
    if src in ("bilibili",) or mode == "bilibili":
        parts.append("B站")
        m = re.search(r"(BV[\w]+)", url or title, re.I)
        # Prefer human title keywords over BV id for image search
        if title_clean:
            parts.append(title_clean)
        elif m:
            parts.append(m.group(1))
        parts.append("视频封面")
        if extra and extra not in title_clean and len(extra) <= 20:
            parts.append(extra)
    elif src in ("netease", "netease_music", "music163") or mode == "netease":
        parts.append("网易云")
        if title_clean:
            parts.append(title_clean)
        kind = str(material.get("kind") or "")
        parts.append("歌单封面" if kind == "playlist" else "专辑封面")
    elif src in ("xiaohongshu", "xhs") or mode == "xiaohongshu":
        parts.append("小红书")
        if title_clean:
            parts.append(title_clean)
        else:
            parts.append("生活氛围")
    elif mode == "chat":
        # chat: prefer gentle related stickers, not random pack
        if title_clean:
            parts.append(title_clean)
        else:
            parts.append("可爱表情包 日常")
    else:
        if title_clean:
            parts.append(title_clean)
        if src and src not in ("none", "chat", "mix"):
            parts.append(src)

    q = " ".join(p for p in parts if p).strip()
    q = re.sub(r"\s+", " ", q)
    if len(q) > 48:
        q = q[:48].rstrip()
    return q


def _allow_local_meme(mode: str, material: dict[str, Any] | None, *, had_cover: bool) -> bool:
    """Local meme is only for chat / theme fluff — never when a source cover existed."""
    if had_cover:
        return False
    material = material or {}
    src = str(material.get("source") or "").strip().lower()
    if mode in _WEB_MODES or src in _WEB_SOURCES:
        # Web post without cover (e.g. XHS theme-fallback): search only; no random meme.
        # Exception: chat-like empty material.
        title = str(material.get("title") or "").strip()
        if title:
            return False
    if mode == "chat" or src in ("", "chat", "none"):
        return True
    return False


async def resolve_post_images(
    *,
    mode: str,
    material: dict[str, Any] | None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    enabled: bool = True,
) -> tuple[list[str], str, list[str]]:
    """Pick images for a post.

    Returns (paths_for_publish, kind, temp_paths_to_cleanup)
    kind in: cover | search | local | none
    """
    if not enabled:
        return [], "none", []

    material = material or {}
    src = str(material.get("source") or "").strip().lower()
    title = str(material.get("title") or "").strip()
    cover = _cover_from_material(material)
    had_cover = bool(cover)
    want_cover = should_try_cover(mode, material)

    temp_paths: list[str] = []

    # 1) cover / page image belonging to this item
    if want_cover and cover:
        path = await _to_thread(_download_to_temp, cover, max_bytes, material=material)
        if path:
            temp_paths.append(path)
            logger.info(
                f"[qzone-image] using cover src={src or mode} title={title[:40]!r} url={cover[:80]}"
            )
            return [path], "cover", temp_paths
        logger.warning(
            f"[qzone-image] cover download failed src={src or mode} url={cover[:80]}"
        )

    # 2) related image search tied to this material
    search_q = build_image_search_query(mode, material)
    if search_q:
        urls = await _to_thread(search_related_image_urls, search_q, 8)
        for u in urls:
            path = await _to_thread(_download_to_temp, u, max_bytes, material=material)
            if path:
                temp_paths.append(path)
                logger.info(
                    f"[qzone-image] using search src={src or mode} q={search_q!r} from={u[:80]}"
                )
                return [path], "search", temp_paths

    # 3) local meme — only when no source cover existed
    if _allow_local_meme(mode, material, had_cover=had_cover):
        try:
            locals_ = pick_meme_image_paths(count=1, max_bytes=max_bytes)
        except Exception as e:
            logger.warning(f"[qzone-image] local meme fail: {e}")
            locals_ = []
        if locals_:
            logger.info(f"[qzone-image] using local meme mode={mode} src={src}")
            return locals_, "local", temp_paths

    if had_cover:
        logger.info(
            "[qzone-image] skip unrelated local meme because source cover existed "
            f"(src={src or mode}, title={title[:40]!r})"
        )
    return [], "none", temp_paths
