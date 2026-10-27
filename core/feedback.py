# -*- coding: utf-8 -*-
"""Topic-weight feedback store for QZone auto posts (D: drive).

Scores from comments that are exactly 0 / 1 / 2:
  0 = like (+2), 1 = ok (+0.5), 2 = dislike (-2)
Same uin+post counted once. Theme weights floored at 0.1.
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .cache_paths import FEEDBACK_DIR, ensure_cache_dirs

SCORE_DELTAS = {"0": 2.0, "1": 0.5, "2": -2.0}
MIN_WEIGHT = 0.1
DEFAULT_WEIGHT = 1.0
MAX_POSTS = 80

# theme keyword heuristics (title / source / text)
THEME_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("音乐", ("音乐", "歌曲", "歌单", "网易云", "听歌", "单曲", "playlist", "mv", "演唱会", "乐队", "rap", "说唱", "流行", "摇滚")),
    ("影视", ("电影", "电视剧", "综艺", "动漫", "番剧", "纪录片", "影评", "追剧", "院线")),
    ("游戏", ("游戏", "电竞", "直播", "steam", "原神", "王者", "英雄联盟", "lol", "csgo")),
    ("数码", ("数码", "手机", "耳机", "电脑", "笔记本", "相机", "平板", "充电", "科技")),
    ("穿搭", ("穿搭", "衣服", "鞋子", "包包", "时尚", "ootd", "搭配")),
    ("美食", ("美食", "吃", "探店", "咖啡", "奶茶", "做饭", "减脂", "食谱", "餐厅")),
    ("护肤", ("护肤", "美妆", "化妆", "面膜", "化妆", "口红")),
    ("健身", ("健身", "运动", "跑步", "瑜伽", "减肥", "打卡", "撸铁")),
    ("旅行", ("旅行", "旅游", "出行", "景点", "酒店", "机票", "城市漫步")),
    ("学习", ("学习", "考试", "考研", "图书馆", "课程", "作业", "高效")),
    ("宠物", ("宠物", "猫", "狗", "喵", "汪", "萌宠")),
    ("情绪", ("情绪", "心情", "焦虑", "治愈", "碎碎念", "emo", "难过", "开心")),
    ("吐槽", ("吐槽", "离谱", "无语", "搞笑", "沙雕", "迷惑")),
    ("宿舍生活", ("宿舍", "改造", "租房", "家居")),
    ("群聊日常", ("群聊", "闲聊", "日常")),
]

WEIGHTS_FILE = "weights.json"
POSTS_FILE = "posts.json"
SCORED_FILE = "scored.json"
REPLIED_FILE = "replied_comments.json"


def _path(name: str) -> Path:
    ensure_cache_dirs()
    return FEEDBACK_DIR / name


def _load_json(name: str, default: Any) -> Any:
    p = _path(name)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[qzone-feedback] load {name} fail: {e}")
        return default


def _save_json(name: str, data: Any) -> None:
    p = _path(name)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def derive_themes(
    *,
    title: str = "",
    source: str = "",
    text: str = "",
    mode: str = "",
) -> list[str]:
    blob = f"{title} {source} {text} {mode}".lower()
    src = (source or "").lower()
    found: list[str] = []
    if src in ("netease", "netease_music") or "网易云" in blob or "music.163" in blob:
        found.append("音乐")
    if src in ("bilibili",) and "影视" not in found:
        pass
    if mode == "chat" or src in ("chat", ""):
        # soft hint only if nothing else matches
        pass
    for theme, kws in THEME_KEYWORDS:
        if any(k.lower() in blob for k in kws):
            if theme not in found:
                found.append(theme)
    if not found:
        if src in ("xiaohongshu", "xhs"):
            found.append("情绪")
        elif src == "bilibili":
            found.append("影视")
        elif mode == "chat":
            found.append("群聊日常")
        else:
            found.append("情绪")
    return found[:4]


def get_weights() -> dict[str, float]:
    data = _load_json(WEIGHTS_FILE, {"themes": {}})
    themes = data.get("themes") if isinstance(data, dict) else {}
    if not isinstance(themes, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in themes.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def save_weights(weights: dict[str, float]) -> None:
    _save_json(
        WEIGHTS_FILE,
        {"themes": weights, "updated_at": int(time.time())},
    )


def ensure_theme_weight(theme: str, weights: dict[str, float] | None = None) -> dict[str, float]:
    w = dict(weights or get_weights())
    if theme not in w:
        w[theme] = DEFAULT_WEIGHT
    return w


def apply_score_to_themes(themes: list[str], score: str) -> dict[str, float]:
    delta = SCORE_DELTAS.get(str(score).strip())
    if delta is None or not themes:
        return get_weights()
    weights = get_weights()
    for t in themes:
        cur = float(weights.get(t, DEFAULT_WEIGHT))
        nxt = max(MIN_WEIGHT, cur + delta)
        weights[t] = round(nxt, 3)
    save_weights(weights)
    logger.info(f"[qzone-feedback] score={score} delta={delta} themes={themes} -> { {t: weights.get(t) for t in themes} }")
    return weights


def record_auto_post(
    *,
    tid: str,
    themes: list[str],
    source: str,
    title: str,
    text: str,
    mode: str = "",
    url: str = "",
) -> None:
    if not tid:
        return
    posts: list[dict] = _load_json(POSTS_FILE, [])
    if not isinstance(posts, list):
        posts = []
    posts = [p for p in posts if str(p.get("tid")) != str(tid)]
    posts.insert(
        0,
        {
            "tid": str(tid),
            "themes": themes or derive_themes(title=title, source=source, text=text, mode=mode),
            "source": source or "",
            "title": (title or "")[:120],
            "text_snippet": (text or "")[:200],
            "mode": mode or "",
            "url": url or "",
            "created_at": int(time.time()),
        },
    )
    _save_json(POSTS_FILE, posts[:MAX_POSTS])
    # seed missing theme weights
    w = get_weights()
    for t in themes:
        if t not in w:
            w[t] = DEFAULT_WEIGHT
    save_weights(w)


def list_auto_posts(limit: int = 20) -> list[dict]:
    posts = _load_json(POSTS_FILE, [])
    if not isinstance(posts, list):
        return []
    return posts[:limit]


def get_post_record(tid: str) -> dict | None:
    for p in list_auto_posts(limit=MAX_POSTS):
        if str(p.get("tid")) == str(tid):
            return p
    return None


def _score_key(post_tid: str, uin: int) -> str:
    return f"{post_tid}:{uin}"


def already_scored(post_tid: str, uin: int) -> bool:
    scored = _load_json(SCORED_FILE, [])
    if not isinstance(scored, list):
        return False
    key = _score_key(post_tid, uin)
    return any(_score_key(str(r.get("post_tid")), int(r.get("uin") or 0)) == key for r in scored)


def record_score(
    *,
    post_tid: str,
    comment_tid: int,
    uin: int,
    score: str,
    themes: list[str],
) -> bool:
    """Return True if newly applied."""
    if already_scored(post_tid, uin):
        return False
    if str(score).strip() not in SCORE_DELTAS:
        return False
    scored = _load_json(SCORED_FILE, [])
    if not isinstance(scored, list):
        scored = []
    scored.insert(
        0,
        {
            "post_tid": str(post_tid),
            "comment_tid": int(comment_tid or 0),
            "uin": int(uin),
            "score": str(score).strip(),
            "themes": themes,
            "applied_at": int(time.time()),
        },
    )
    _save_json(SCORED_FILE, scored[:500])
    apply_score_to_themes(themes, score)
    return True


def parse_rating(content: str) -> str | None:
    s = re.sub(r"[\s\u3000]+", "", (content or "")).strip()
    if s in ("0", "1", "2"):
        return s
    return None


def reply_key(post_tid: str, comment_tid: int) -> str:
    return f"{post_tid}:{int(comment_tid)}"


def already_replied(post_tid: str, comment_tid: int) -> bool:
    data = _load_json(REPLIED_FILE, [])
    if not isinstance(data, list):
        return False
    return reply_key(post_tid, comment_tid) in data


def mark_replied(post_tid: str, comment_tid: int) -> None:
    data = _load_json(REPLIED_FILE, [])
    if not isinstance(data, list):
        data = []
    key = reply_key(post_tid, comment_tid)
    if key not in data:
        data.insert(0, key)
    _save_json(REPLIED_FILE, data[:1000])


def sample_themes(k: int = 2) -> list[str]:
    weights = get_weights()
    # include known themes even if unseen
    for theme, _ in THEME_KEYWORDS:
        weights.setdefault(theme, DEFAULT_WEIGHT)
    items = list(weights.items())
    if not items:
        return ["情绪"]
    names = [n for n, _ in items]
    ws = [max(MIN_WEIGHT, float(w)) for _, w in items]
    total = sum(ws)
    picked: list[str] = []
    for _ in range(min(k, len(names))):
        r = random.random() * total
        acc = 0.0
        choice = names[0]
        for n, w in zip(names, ws):
            acc += w
            if r <= acc:
                choice = n
                break
        if choice not in picked:
            picked.append(choice)
        # reduce chance of re-picking same in this draw
        idx = names.index(choice)
        total -= ws[idx]
        ws[idx] = 0.0
        if total <= 0:
            break
    return picked or ["情绪"]


def preference_hint() -> str:
    weights = get_weights()
    if not weights:
        return ""
    ranked = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    high = [t for t, w in ranked if w >= 1.5][:4]
    low = [t for t, w in ranked if w <= 0.6][:4]
    parts = []
    if high:
        parts.append("更偏向这些题材：" + "、".join(high))
    if low:
        parts.append("尽量少碰这些题材：" + "、".join(low))
    if not parts:
        mid = [t for t, _ in ranked[:3]]
        if mid:
            parts.append("可参考近期偏好题材：" + "、".join(mid))
    return "；".join(parts)


def bias_mode_by_themes(themes: list[str]) -> str | None:
    """Map preferred themes to a material mode hint."""
    tset = set(themes or [])
    if "音乐" in tset:
        return "netease"
    if tset & {"穿搭", "护肤", "美食", "宿舍生活", "旅行", "健身"}:
        return "xiaohongshu"
    if tset & {"影视", "游戏", "数码", "吐槽"}:
        return "bilibili"
    if "群聊日常" in tset:
        return "chat"
    return None


def score_title_against_themes(title: str, preferred: list[str]) -> float:
    if not preferred:
        return 1.0
    derived = derive_themes(title=title)
    hit = len(set(derived) & set(preferred))
    return 1.0 + hit * 2.0
