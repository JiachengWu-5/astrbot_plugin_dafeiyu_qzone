# -*- coding: utf-8 -*-
"""Fetch casual hot-topic seeds for QZone auto-publish.

Sources:
- Bilibili public ranking/popular APIs (no login)
- Xiaohongshu: best-effort public aggregators; if unavailable, rotate
  lifestyle/hot-theme seeds in XHS tone (no account bypass)
- Caller may still mix with chat-message material
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from astrbot.api import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Soft fallback themes in 小红书 vibe when live hotlist is unreachable.
XHS_FALLBACK_THEMES = [
    "今日穿搭灵感：轻松出门也能很有氛围感",
    "周末探店：一家小众但出片的咖啡店",
    "护肤日常：换季烂脸自救小技巧",
    "宿舍改造：花小钱提升幸福感的细节",
    "健身打卡：坚持两周后真的有变化",
    "宅家美食：十分钟搞定的减脂晚饭",
    "旅行备忘：城市漫步必带的小物件",
    "学习搭子：图书馆高效学习的小习惯",
    "数码种草：学生党真正能用上的耳机",
    "情绪管理：感觉被掏空时怎么缓一缓",
    "宠物日记：家里小猫的迷惑行为大赏",
    "电影安利：适合一个人看的治愈片单",
]

_INVALID_URL_MARKERS = (
    "apps.apple.com",
    "play.google.com",
    "itunes.apple.com",
    "app.xiaohongshu.com/download",
    "xiaohongshu.com/download",
    "page.xiaohongshu.com/download",
    "market.xiaomi.com",
    "appgallery.huawei.com",
)


def xhs_search_url(keyword: str) -> str:
    """Legacy desktop search page. Do NOT use for QZone attribution (phone often cannot open)."""
    kw = (keyword or "").strip() or "生活热点"
    return "https://www.xiaohongshu.com/search_result?" + urlencode({"keyword": kw})


_XHS_NOTE_ID_RE = re.compile(
    r"(?:explore|discovery/item|item|note)/([0-9a-f]{16,32})",
    re.I,
)
_XHS_NOTE_ID_BARE_RE = re.compile(r"^[0-9a-f]{24}$", re.I)


def extract_xhs_note_id(*candidates: Any) -> str:
    """Pull a Xiaohongshu note id from urls / ids / nested dict values."""
    for raw in candidates:
        if raw is None:
            continue
        if isinstance(raw, dict):
            for key in (
                "note_id",
                "noteId",
                "id",
                "noteid",
                "item_id",
                "itemId",
                "url",
                "mobileUrl",
                "link",
                "href",
                "share_url",
                "shareUrl",
            ):
                got = extract_xhs_note_id(raw.get(key))
                if got:
                    return got
            continue
        s = str(raw).strip()
        if not s:
            continue
        if _XHS_NOTE_ID_BARE_RE.match(s):
            return s.lower()
        m = _XHS_NOTE_ID_RE.search(s.replace("\\", "/"))
        if m:
            return m.group(1).lower()
        qm = re.search(r"[?&](?:note_id|noteId|id)=([0-9a-f]{16,32})", s, re.I)
        if qm:
            return qm.group(1).lower()
    return ""


def xhs_note_url(note_id: str) -> str:
    nid = (note_id or "").strip()
    if not nid:
        return ""
    # explore/<id> opens in app / mobile web better than search_result
    return f"https://www.xiaohongshu.com/explore/{nid}"


def is_valid_content_url(url: str) -> bool:
    """Keep real content targets only (note / video / song). Reject search & store pages."""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return False
    low_full = u.lower()
    low = low_full.split("?", 1)[0].rstrip("/")
    if low in (
        "https://www.xiaohongshu.com/explore",
        "http://www.xiaohongshu.com/explore",
        "https://xiaohongshu.com/explore",
        "http://xiaohongshu.com/explore",
        "https://www.xiaohongshu.com",
        "https://xiaohongshu.com",
        "https://music.163.com",
        "http://music.163.com",
        "https://www.bilibili.com",
        "https://www.bilibili.com/v/popular/all",
        "http://www.bilibili.com/v/popular/all",
    ):
        return False
    if any(m in low for m in _INVALID_URL_MARKERS):
        return False
    if "xiaohongshu.com" in low or "xhslink.com" in low:
        # NEVER treat search_result as attribution-worthy
        if "/search_result" in low:
            return False
        if "/explore/" in low:  # /explore/{note_id}
            return True
        if "/discovery/item/" in low or "/item/" in low or "/note/" in low:
            return True
        if "xhslink.com" in low:
            return True
        return False
    if "bilibili.com" in low or "b23.tv" in low:
        return ("/video/" in low) or ("/bangumi/" in low) or ("b23.tv" in low)
    if "music.163.com" in low:
        return ("/song" in low) or ("/playlist" in low) or ("/album" in low)
    return True


def normalize_material_url(source: str, title: str, url: str) -> str:
    """Return a phone-openable content URL, or empty if none (omit rather than broken search)."""
    src = (source or "").strip().lower()
    u = (url or "").strip()
    if is_valid_content_url(u):
        if src in ("xiaohongshu", "xhs") or "xiaohongshu.com" in u.lower() or "xhslink.com" in u.lower():
            nid = extract_xhs_note_id(u)
            if nid:
                return xhs_note_url(nid)
        return u
    if src in ("xiaohongshu", "xhs"):
        nid = extract_xhs_note_id(u, title)
        if nid:
            return xhs_note_url(nid)
        # No note id: omit link (search_result often fails on mobile QQ)
        return ""
    if src == "bilibili":
        m = re.search(r"(BV[\w]+)", u or title or "", re.I)
        if m:
            return f"https://www.bilibili.com/video/{m.group(1)}"
        return ""
    if src in ("netease", "netease_music", "music163"):
        if re.search(r"(?:song|playlist|album)\?id=\d+", u):
            return u
        return ""
    return u if is_valid_content_url(u) else ""


def _http_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 12) -> Any:
    req = Request(url, headers={**(headers or {}), "User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def fetch_bilibili_hot(limit: int = 30) -> list[dict[str, str]]:
    """Public Bilibili hot/ranking titles (no login)."""

    def _fetch() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        headers = {
            "Referer": "https://www.bilibili.com/v/popular/rank/all",
            "Origin": "https://www.bilibili.com",
        }
        endpoints = [
            "https://api.bilibili.com/x/web-interface/popular?" + urlencode({"ps": "50", "pn": "1"}),
            "https://api.bilibili.com/x/web-interface/ranking/v2?" + urlencode({"rid": "0", "type": "all"}),
        ]
        for endpoint in endpoints:
            try:
                data = _http_get_json(endpoint, headers=headers)
                if data.get("code") != 0:
                    continue
                for row in (data.get("data") or {}).get("list") or []:
                    title = str(row.get("title") or "").strip()
                    if not title:
                        continue
                    owner = ""
                    if isinstance(row.get("owner"), dict):
                        owner = str(row["owner"].get("name") or "")
                    bvid = str(row.get("bvid") or "").strip()
                    aid = row.get("aid")
                    if bvid:
                        item_url = f"https://www.bilibili.com/video/{bvid}"
                    elif aid:
                        item_url = f"https://www.bilibili.com/video/av{aid}"
                    else:
                        item_url = "https://www.bilibili.com/v/popular/all"
                    cover = str(row.get("pic") or row.get("cover") or "").strip()
                    if cover.startswith("//"):
                        cover = "https:" + cover
                    item: dict[str, str] = {
                        "source": "bilibili",
                        "title": title,
                        "extra": owner,
                        "url": item_url,
                    }
                    if cover:
                        item["cover"] = cover
                        item["image_url"] = cover
                    out.append(item)
            except Exception as e:
                logger.debug(f"[hot_topics] bilibili fetch fail: {e}")
        seen = set()
        uniq = []
        for it in out:
            if it["title"] in seen:
                continue
            seen.add(it["title"])
            uniq.append(it)
        return uniq

    items = await _to_thread(_fetch)
    random.shuffle(items)
    return items[:limit]


async def fetch_xiaohongshu_hot(limit: int = 20) -> list[dict[str, str]]:
    """Best-effort XHS hot topics via public aggregators; soft fallback themes."""

    def _fetch_live() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        urls = [
            "https://api-hot.imsyy.top/xiaohongshu?cache=true",
            "https://dailyhot.zkeq.top/xiaohongshu",
            "https://uapis.cn/api/hotlist?type=xiaohongshu",
        ]
        for url in urls:
            try:
                data = _http_get_json(url)
                rows = data.get("data") or data.get("list") or data.get("word_list") or []
                if isinstance(data, list):
                    rows = data
                for row in rows:
                    if isinstance(row, str):
                        title = row.strip()
                        link = ""
                        cover = ""
                    elif isinstance(row, dict):
                        title = str(
                            row.get("title")
                            or row.get("name")
                            or row.get("word")
                            or row.get("hot_word")
                            or ""
                        ).strip()
                        note_id = extract_xhs_note_id(row)
                        link = str(
                            row.get("url")
                            or row.get("mobileUrl")
                            or row.get("link")
                            or row.get("href")
                            or row.get("share_url")
                            or row.get("shareUrl")
                            or ""
                        ).strip()
                        if note_id:
                            link = xhs_note_url(note_id)
                        elif link:
                            # may still be search / download; normalize later
                            pass
                        cover = str(
                            row.get("cover")
                            or row.get("pic")
                            or row.get("img")
                            or row.get("image")
                            or row.get("image_url")
                            or ""
                        ).strip()
                    else:
                        continue
                    if not title:
                        continue
                    link = normalize_material_url("xiaohongshu", title, link)
                    item: dict[str, str] = {
                        "source": "xiaohongshu",
                        "title": title,
                        "extra": "live",
                        "url": link,
                    }
                    if cover.startswith("//"):
                        cover = "https:" + cover
                    if cover.startswith("http"):
                        item["cover"] = cover
                        item["image_url"] = cover
                    out.append(item)
                if out:
                    break
            except Exception as e:
                logger.debug(f"[hot_topics] xhs aggregator fail {url}: {e}")
        return out

    live = await _to_thread(_fetch_live)
    if live:
        random.shuffle(live)
        return live[:limit]

    themes = random.sample(XHS_FALLBACK_THEMES, k=min(limit, len(XHS_FALLBACK_THEMES)))
    return [
        {
            "source": "xiaohongshu",
            "title": theme,
            "extra": "theme-fallback",
            # No note id available offline — omit URL rather than broken search_result
            "url": "",
        }
        for theme in themes
    ]



async def fetch_netease_hot(limit: int = 20) -> list[dict[str, str]]:
    """NetEase Cloud Music: hot toplist tracks + recommended playlists (public APIs)."""

    def _fetch() -> list[dict[str, str]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        }
        out: list[dict[str, str]] = []
        # Official hot song charts (热歌榜 / 飙升榜 / 新歌榜)
        playlist_ids = ("3778678", "19723756", "3779629")
        for pid in playlist_ids:
            if len(out) >= limit:
                break
            try:
                data = _http_get_json(
                    "https://music.163.com/api/playlist/detail?"
                    + urlencode({"id": pid}),
                    headers=headers,
                )
                playlist = (data or {}).get("result") or (data or {}).get("playlist") or {}
                tracks = playlist.get("tracks") or []
                pl_name = str(playlist.get("name") or "网易云热歌")
                for tr in tracks:
                    if len(out) >= limit:
                        break
                    if not isinstance(tr, dict):
                        continue
                    sid = tr.get("id")
                    name = str(tr.get("name") or "").strip()
                    if not sid or not name:
                        continue
                    artists = tr.get("artists") or tr.get("ar") or []
                    artist = ""
                    if isinstance(artists, list) and artists:
                        artist = str((artists[0] or {}).get("name") or "")
                    album = tr.get("album") or tr.get("al") or {}
                    cover = ""
                    if isinstance(album, dict):
                        cover = str(album.get("picUrl") or album.get("blurPicUrl") or "")
                    title = f"{name} - {artist}" if artist else name
                    out.append(
                        {
                            "source": "netease",
                            "title": title,
                            "url": f"https://music.163.com/song?id={sid}",
                            "cover": cover,
                            "image_url": cover,
                            "extra": pl_name,
                            "kind": "song",
                        }
                    )
            except Exception as e:
                logger.debug(f"[hot_topics] netease playlist {pid} fail: {e}")

        # Recommended playlists
        try:
            data = _http_get_json(
                "https://music.163.com/api/personalized/playlist?"
                + urlencode({"limit": str(max(10, min(limit, 30)))}),
                headers=headers,
            )
            for pl in (data or {}).get("result") or []:
                if len(out) >= limit * 2:
                    break
                if not isinstance(pl, dict):
                    continue
                pid = pl.get("id")
                name = str(pl.get("name") or "").strip()
                if not pid or not name:
                    continue
                cover = str(pl.get("picUrl") or pl.get("coverImgUrl") or "")
                out.append(
                    {
                        "source": "netease",
                        "title": name,
                        "url": f"https://music.163.com/playlist?id={pid}",
                        "cover": cover,
                        "image_url": cover,
                        "extra": "推荐歌单",
                        "kind": "playlist",
                    }
                )
        except Exception as e:
            logger.debug(f"[hot_topics] netease personalized fail: {e}")

        # de-dupe by url
        seen: set[str] = set()
        uniq: list[dict[str, str]] = []
        for item in out:
            u = item.get("url") or ""
            if u in seen:
                continue
            seen.add(u)
            uniq.append(item)
        return uniq[: max(limit, 10)]

    return await _to_thread(_fetch)


async def pick_hot_material(preferred: str | None = None) -> dict[str, str]:
    """Randomly pick one hot material from bilibili / xhs / netease (fallback ok)."""
    allowed = ("bilibili", "xiaohongshu", "netease")
    source = preferred if preferred in allowed else random.choice(
        ["bilibili", "xiaohongshu", "netease", "bilibili", "xiaohongshu", "netease"]
    )
    fetchers = {
        "bilibili": fetch_bilibili_hot,
        "xiaohongshu": fetch_xiaohongshu_hot,
        "netease": fetch_netease_hot,
    }
    order = [source] + [s for s in allowed if s != source]
    items: list[dict[str, str]] = []
    for src_try in order:
        items = await fetchers[src_try]()
        if items:
            source = src_try
            break
    if not items:
        return {
            "source": "none",
            "title": "随便聊聊今天的小心情",
            "prompt": "写一条轻松碎碎念的QQ空间说说，口语化，像朋友圈随手发",
            "url": "",
        }
    item = random.choice(items)
    src = item["source"]
    title = item["title"]
    item_url = normalize_material_url(src, title, item.get("url", ""))
    if src == "bilibili":
        prompt = (
            f"参考B站当前热门话题/视频标题：「{title}」。"
            "用中文写一条轻松、口语化的QQ空间说说，像在跟朋友分享观感，"
            "不要写成新闻稿，不要贴链接，不要剧透。避免标题党、禁止辱骂。"
        )
    elif src == "netease":
        kind = item.get("kind") or "song"
        if kind == "playlist":
            prompt = (
                f"参考网易云推荐歌单：「{title}」。"
                "用中文写一条适合边听边发的QQ空间说说，轻松、有点情绪或氛围，"
                "不要写成歌单简介，不要贴链接，不要广告腔。"
            )
        else:
            prompt = (
                f"参考网易云热门歌曲：「{title}」。"
                "用中文写一条听歌感很强的QQ空间说说，像随手分享心情，"
                "可以轻轻提到歌名氛围，不要贴链接，不要写成乐评长文。"
            )
    else:
        prompt = (
            f"参考小红书近期热点关键词：「{title}」。"
            "用中文写一条带点氛围感的QQ空间说说，可以碎碎念、像发朋友圈，"
            "带一点点生活感或情绪，不要硬广，不要贴链接。"
        )
    out: dict[str, str] = {
        "source": src,
        "title": title,
        "prompt": prompt,
        "extra": item.get("extra", ""),
        "url": item_url,
    }
    cover = item.get("cover") or item.get("image_url") or ""
    if cover:
        out["cover"] = cover
        out["image_url"] = cover
    return out


def format_source_footer(mode: str, material: dict[str, Any] | None = None) -> str:
    """Source attribution with clickable URL when available."""
    material = material or {}
    src = str(material.get("source") or "").strip().lower()
    title = str(material.get("title") or "").strip()
    url = str(material.get("url") or "").strip()
    if url:
        url = normalize_material_url(src or mode, title, url)

    label_map = {
        "bilibili": "B站热门",
        "netease": "网易云热门",
        "xiaohongshu": "小红书热点",
        "xhs": "小红书热点",
        "chat": "群聊闲聊",
        "mix": "热点 + 群聊",
        "none": "日常碎碎念",
    }

    if mode == "chat":
        label = "群聊闲聊"
        url = ""
    elif mode == "mix":
        base = label_map.get(src, "联网热点")
        label = base + " + 群聊"
    else:
        label = label_map.get(mode) or label_map.get(src) or "联网热点"

    if url and not is_valid_content_url(url):
        # e.g. leftover search_result from older materials
        nid = extract_xhs_note_id(url, title) if ("xiaohongshu" in (mode, src) or "xhs" in (mode, src) or "xiaohongshu" in url) else ""
        url = xhs_note_url(nid) if nid else ""
    # Do NOT invent search_result / site homepages — omit link if no real content URL
    lines = ["来源：" + label]
    if title and mode != "chat":
        short = title if len(title) <= 36 else title[:35] + "…"
        lines.append(short)
    if url:
        lines.append(url)
    return chr(10).join(lines)


def with_source_footer(text: str, mode: str, material: dict[str, Any] | None = None) -> str:
    body = (text or "").strip()
    footer = format_source_footer(mode, material)
    if not body:
        return footer
    if footer in body or (material or {}).get("url") and str((material or {}).get("url")) in body:
        return body
    return body + chr(10) + chr(10) + footer
