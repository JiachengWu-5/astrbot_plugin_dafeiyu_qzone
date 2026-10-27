# -*- coding: utf-8 -*-
"""Hybrid comment feedback: event/trigger + cheap change watch + rare safety scan.

NapCat/OneBot does NOT emit QZone comment notices (confirmed against NapCat event
compat docs and local logs). Zhalslar's original qzone plugin also had no comment
listener. So primary detection is a cheap signature watch (HTTP only, no LLM);
full scan (with LLM only for unreplied non-score comments) runs on:
  - request_scan() from inbound QQ messages / admin command / page API
  - comment signature delta from the cheap watcher
  - rare safety IntervalTrigger (default 45 min)
"""
from __future__ import annotations

import asyncio
import zoneinfo
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from astrbot.api import logger

from . import feedback as fb
from .config import PluginConfig
from .model import Comment, Post
from .sender import Sender
from .service import PostService


class AutoFeedbackScan:
    """Hybrid auto feedback scheduler (event + cheap watch + safety net)."""

    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender | None = None,
        *,
        watch_interval_seconds: int = 120,
        safety_interval_minutes: int = 45,
        first_delay_seconds: int = 90,
        debounce_seconds: float = 12.0,
        ensure_client: Callable[[], Any] | None = None,
        # legacy kw (ignored / mapped)
        interval_minutes: int | None = None,
    ):
        self.cfg = config
        self.service = service
        self.sender = sender
        self.job_name = "AutoFeedbackScan"
        self.timezone = config.timezone or zoneinfo.ZoneInfo("Asia/Shanghai")
        if interval_minutes is not None and safety_interval_minutes == 45:
            # Allow old callers to set safety via interval_minutes
            safety_interval_minutes = max(15, int(interval_minutes))
        self.watch_interval_seconds = max(60, int(watch_interval_seconds or 120))
        self.safety_interval_minutes = max(15, int(safety_interval_minutes or 45))
        self.interval_minutes = self.safety_interval_minutes  # status compat
        self.first_delay_seconds = max(15, int(first_delay_seconds or 90))
        self.debounce_seconds = max(3.0, float(debounce_seconds or 12.0))
        self.ensure_client = ensure_client
        self._terminated = False
        self.last_run_at: str | None = None
        self.last_watch_at: str | None = None
        self.last_summary: dict | None = None
        self.next_run_at: str | None = None
        self.run_count: int = 0
        self.watch_count: int = 0
        self.last_error: str | None = None
        self.last_trigger_reason: str | None = None
        self.last_signature: dict[str, tuple[int, ...]] = {}
        self._scan_lock = asyncio.Lock()
        self._debounce_task: asyncio.Task | None = None
        self._pending_reason: str | None = None

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self.scheduler = AsyncIOScheduler(timezone=self.timezone, event_loop=loop)
        else:
            self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        now = datetime.now(self.timezone)
        first_at = now + timedelta(seconds=self.first_delay_seconds)
        try:
            self.scheduler.add_job(
                func=self._first_kick,
                trigger=DateTrigger(run_date=first_at, timezone=self.timezone),
                id=f"{self.job_name}_first",
                name=f"{self.job_name}_first",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
                replace_existing=True,
            )
            self.scheduler.add_job(
                func=self._watch_wrapper,
                trigger=IntervalTrigger(
                    seconds=self.watch_interval_seconds,
                    timezone=self.timezone,
                    start_date=first_at + timedelta(seconds=5),
                ),
                id=f"{self.job_name}_watch",
                name=f"{self.job_name}_watch",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
                replace_existing=True,
            )
            self.scheduler.add_job(
                func=self._safety_wrapper,
                trigger=IntervalTrigger(
                    minutes=self.safety_interval_minutes,
                    timezone=self.timezone,
                    start_date=first_at + timedelta(minutes=1),
                ),
                id=f"{self.job_name}_safety",
                name=f"{self.job_name}_safety",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=600,
                replace_existing=True,
            )
            self.next_run_at = first_at.isoformat()
            logger.info(
                f"[{self.job_name}] hybrid registered: first={first_at.isoformat()} "
                f"watch={self.watch_interval_seconds}s safety={self.safety_interval_minutes}m"
            )
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"[{self.job_name}] schedule register failed: {e}")

    def status(self) -> dict:
        next_run = self.next_run_at
        try:
            job = self.scheduler.get_job(f"{self.job_name}_safety")
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()
                self.next_run_at = next_run
        except Exception:
            pass
        return {
            "enabled": not self._terminated,
            "mode": "hybrid",
            "trigger": "event+cheap_watch+safety",
            "watch_interval_seconds": self.watch_interval_seconds,
            "safety_interval_minutes": self.safety_interval_minutes,
            "interval_minutes": self.safety_interval_minutes,
            "first_delay_seconds": self.first_delay_seconds,
            "debounce_seconds": self.debounce_seconds,
            "run_count": self.run_count,
            "watch_count": self.watch_count,
            "last_run_at": self.last_run_at,
            "last_watch_at": self.last_watch_at,
            "next_run_at": next_run,
            "last_summary": self.last_summary,
            "last_error": self.last_error,
            "last_trigger_reason": self.last_trigger_reason,
            "tracked_posts": len(self.last_signature),
            "scheduler_running": bool(getattr(self.scheduler, "running", False)),
            "napcat_qzone_comment_notice": False,
            "note": (
                "NapCat has no QZone comment notice; "
                "replies fire on comment-count change / inbound trigger / 45m safety"
            ),
        }

    def request_scan(self, reason: str = "manual") -> None:
        """Debounced request for a full feedback scan (no-op if terminated)."""
        if self._terminated:
            return
        self._pending_reason = reason or "manual"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Scheduler thread may call us; schedule via APScheduler date job
            try:
                run_at = datetime.now(self.timezone) + timedelta(seconds=self.debounce_seconds)
                self.scheduler.add_job(
                    func=self._run_pending_scan,
                    trigger=DateTrigger(run_date=run_at, timezone=self.timezone),
                    id=f"{self.job_name}_debounce",
                    name=f"{self.job_name}_debounce",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=120,
                    replace_existing=True,
                )
            except Exception as e:
                logger.warning(f"[{self.job_name}] request_scan schedule fail: {e}")
            return

        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        async def _debounced():
            try:
                await asyncio.sleep(self.debounce_seconds)
                await self._run_pending_scan()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[{self.job_name}] debounce fail: {e}")

        self._debounce_task = loop.create_task(_debounced())

    async def _run_pending_scan(self):
        reason = self._pending_reason or "debounced"
        self._pending_reason = None
        await self._run_scan(reason)

    async def _first_kick(self):
        await self._run_scan("first_kick")

    async def _safety_wrapper(self):
        await self._run_scan("safety_interval")

    async def _watch_wrapper(self):
        if self._terminated:
            return
        try:
            changed = await self.cheap_watch()
            if changed:
                # immediate-ish scan (still uses request debounce lightly)
                self._pending_reason = "comment_delta"
                await self._run_scan("comment_delta")
        except Exception as e:
            self.last_error = str(e)
            logger.exception(f"[{self.job_name}] watch fail: {e}")

    def _prepare_client(self) -> bool:
        if self.ensure_client:
            try:
                self.ensure_client()
            except Exception as e:
                logger.debug(f"[{self.job_name}] ensure_client: {e}")
        return bool(getattr(self.cfg, "client", None))

    async def cheap_watch(self) -> bool:
        """HTTP-only: compare comment tid signatures. Returns True if changed."""
        self.watch_count += 1
        self.last_watch_at = datetime.now(self.timezone).isoformat()
        if not self._prepare_client():
            logger.debug(f"[{self.job_name}] watch skip: no CQHttp client yet")
            return False
        try:
            sig = await self._collect_signature()
        except Exception as e:
            self.last_error = f"watch:{e}"
            logger.warning(f"[{self.job_name}] signature collect fail: {e}")
            return False
        if not self.last_signature:
            self.last_signature = sig
            logger.info(
                f"[{self.job_name}] watch baseline set posts={len(sig)}"
            )
            return False
        changed_tids = [
            tid
            for tid, ids in sig.items()
            if self.last_signature.get(tid) != ids
        ]
        # also treat brand-new tracked posts with comments as change
        for tid, ids in sig.items():
            if tid not in self.last_signature and ids:
                if tid not in changed_tids:
                    changed_tids.append(tid)
        self.last_signature = sig
        if changed_tids:
            logger.info(
                f"[{self.job_name}] comment signature changed tids={changed_tids[:8]}"
            )
            return True
        return False

    async def _collect_signature(self) -> dict[str, tuple[int, ...]]:
        records = fb.list_auto_posts(limit=15)
        if not records:
            return {}
        self_uin = await self.service.session.get_uin()
        wanted = {str(r.get("tid")) for r in records if r.get("tid")}
        posts = await self.service.query_feeds(
            target_id=str(self_uin),
            pos=0,
            num=min(20, max(len(wanted), 10)),
            with_detail=False,
            no_self=False,
            no_commented=False,
        )
        by_tid: dict[str, Post] = {}
        for p in posts:
            if p.tid:
                by_tid[str(p.tid)] = p
        sig: dict[str, tuple[int, ...]] = {}
        for tid in wanted:
            post = by_tid.get(tid) or Post(uin=self_uin, tid=tid, text="")
            if not post.comments:
                try:
                    filled = await self.service._fill_post_detail([post])
                    if filled:
                        post = filled[0]
                except Exception:
                    continue
            ids = sorted(
                {
                    int(c.tid)
                    for c in (post.comments or [])
                    if c.parent_tid is None and c.uin != self_uin and c.tid
                }
            )
            sig[tid] = tuple(ids)
        return sig

    async def _run_scan(self, reason: str) -> dict | None:
        if self._terminated:
            return None
        if self._scan_lock.locked():
            logger.info(f"[{self.job_name}] scan busy, skip reason={reason}")
            return None
        async with self._scan_lock:
            if not self._prepare_client():
                self.last_error = "CQHttp 实例不存在"
                logger.warning(
                    f"[{self.job_name}] scan skip reason={reason}: no CQHttp client"
                )
                return None
            self.last_trigger_reason = reason
            try:
                summary = await scan_feedback_once(self.service)
                # refresh signature after successful scan
                try:
                    self.last_signature = await self._collect_signature()
                except Exception:
                    pass
                self.last_summary = summary
                self.last_error = None
                logger.info(f"[{self.job_name}] scan done reason={reason}: {summary}")
                return summary
            except Exception as e:
                self.last_error = str(e)
                logger.exception(f"[{self.job_name}] scan fail reason={reason}: {e}")
                return None
            finally:
                self.run_count += 1
                self.last_run_at = datetime.now(self.timezone).isoformat()
                try:
                    job = self.scheduler.get_job(f"{self.job_name}_safety")
                    if job and job.next_run_time:
                        self.next_run_at = job.next_run_time.isoformat()
                except Exception:
                    pass

    async def do_task(self) -> dict:
        summary = await self._run_scan("do_task")
        return summary or {}

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        try:
            self.scheduler.remove_all_jobs()
        except Exception:
            pass
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info(f"[{self.job_name}] stopped")


async def scan_feedback_once(service: PostService, *, limit_posts: int = 15) -> dict:
    """Scan recent auto posts: apply ratings, auto-reply non-ratings.

    LLM is ONLY called for new non-score comments that have not been replied yet.
    Score-only comments (0/1/2) never burn chat tokens.
    """
    summary = {
        "posts_checked": 0,
        "ratings_applied": [],
        "replies_sent": 0,
        "skipped": 0,
        "errors": [],
        "comments_seen": [],
        "llm_calls": 0,
    }
    records = fb.list_auto_posts(limit=limit_posts)
    if not records:
        logger.info("[qzone-feedback] no auto-post records yet")
        return summary

    try:
        self_uin = await service.session.get_uin()
    except Exception as e:
        summary["errors"].append(f"uin:{e}")
        return summary

    wanted = {str(r.get("tid")): r for r in records if r.get("tid")}

    try:
        posts = await service.query_feeds(
            target_id=str(self_uin),
            pos=0,
            num=min(20, max(limit_posts, 10)),
            with_detail=False,
            no_self=False,
            no_commented=False,
        )
    except Exception as e:
        summary["errors"].append(f"feeds:{e}")
        posts = []

    by_tid: dict[str, Post] = {}
    for p in posts:
        if p.tid:
            by_tid[str(p.tid)] = p

    for tid, rec in wanted.items():
        post = by_tid.get(tid)
        if not post:
            post = Post(uin=self_uin, tid=tid, text=str(rec.get("text_snippet") or ""))
        if not post.comments:
            try:
                filled = await service._fill_post_detail([post])
                if filled:
                    post = filled[0]
            except Exception as e:
                summary["errors"].append(f"detail {tid}:{e}")
                continue
        summary["posts_checked"] += 1
        themes = list(rec.get("themes") or []) or fb.derive_themes(
            title=str(rec.get("title") or ""),
            source=str(rec.get("source") or ""),
            text=str(rec.get("text_snippet") or post.text or ""),
            mode=str(rec.get("mode") or ""),
        )

        for comment in list(post.comments or []):
            if comment.parent_tid is not None:
                continue
            if comment.uin == self_uin:
                continue
            summary["comments_seen"].append(
                {
                    "tid": tid,
                    "comment_tid": comment.tid,
                    "uin": comment.uin,
                    "content": (comment.content or "")[:80],
                }
            )
            rating = fb.parse_rating(comment.content)
            if rating is not None:
                # Score-only: no LLM
                applied = fb.record_score(
                    post_tid=tid,
                    comment_tid=int(comment.tid or 0),
                    uin=int(comment.uin),
                    score=rating,
                    themes=themes,
                )
                if applied:
                    summary["ratings_applied"].append(
                        {
                            "tid": tid,
                            "uin": comment.uin,
                            "score": rating,
                            "themes": themes,
                            "content": (comment.content or "")[:40],
                        }
                    )
                continue

            if not comment.tid:
                continue
            if fb.already_replied(tid, int(comment.tid)):
                summary["skipped"] += 1
                continue
            try:
                summary["llm_calls"] += 1
                reply_text = await service.reply_to_comment(post, comment)
                fb.mark_replied(tid, int(comment.tid))
                summary["replies_sent"] += 1
                summary.setdefault("replies", []).append(
                    {
                        "tid": tid,
                        "comment_tid": comment.tid,
                        "uin": comment.uin,
                        "comment": (comment.content or "")[:60],
                        "reply": (reply_text or "")[:120],
                    }
                )
                logger.info(
                    f"[qzone-feedback] auto-replied tid={tid} comment={comment.tid} uin={comment.uin}"
                )
            except Exception as e:
                summary["errors"].append(f"reply {tid}/{comment.tid}:{e}")
                logger.warning(f"[qzone-feedback] reply fail: {e}")

    logger.info(f"[qzone-feedback] scan done: {summary}")
    return summary
