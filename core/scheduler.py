import random
import zoneinfo
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger

from .config import PluginConfig
from .sender import Sender
from .service import PostService


class AutoRandomCronTask:
    """
    Schedule one task per cron cycle around the cron anchor time.
    Subclasses only need to implement async do_task().
    """

    def __init__(
        self,
        job_name: str,
        cron_expr: str,
        timezone: zoneinfo.ZoneInfo,
        offset_seconds: int,
    ):
        self.timezone = timezone
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        self.cron_expr = cron_expr
        self.job_name = job_name
        self.offset_seconds = offset_seconds
        self._last_base_time: datetime | None = None
        self._terminated = False

        self._register_task()

        logger.info(
            f"[{self.job_name}] 已启动，任务周期：{self.cron_expr}，偏移范围：±{self.offset_seconds} 分钟"
        )

    def _register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(
                self.cron_expr, timezone=self.timezone
            )
            self._schedule_next_job()
        except Exception as e:
            logger.error(f"[{self.job_name}] Cron 格式错误：{e}")

    def _schedule_next_job(self):
        if self._terminated:
            logger.debug(f"[{self.job_name}] 调度器已终止，跳过后续调度")
            return
        if not hasattr(self, "trigger"):
            return

        now = datetime.now(self.timezone)
        base_time = self.trigger.get_next_fire_time(self._last_base_time, now)
        if not base_time:
            logger.error(f"[{self.job_name}] 无法计算下一次基准时间")
            return

        self._last_base_time = base_time

        delay_seconds = (
            random.randint(-self.offset_seconds, self.offset_seconds)
            if self.offset_seconds
            else 0
        )
        target_time = base_time + timedelta(seconds=delay_seconds)

        if target_time <= now:
            target_time = now + timedelta(seconds=1)
            logger.warning(
                f"[{self.job_name}] 偏移后时间已过，改为立即补偿执行：{target_time}"
            )

        logger.info(
            f"[{self.job_name}] 基准时间：{base_time}，偏移：{delay_seconds} 秒，执行时间：{target_time}"
        )

        try:
            self.scheduler.add_job(
                func=self._run_task_wrapper,
                trigger=DateTrigger(run_date=target_time, timezone=self.timezone),
                name=f"{self.job_name}_once_{int(base_time.timestamp())}",
                max_instances=1,
            )
        except Exception as e:
            if self._terminated:
                logger.debug(
                    f"[{self.job_name}] 调度器终止后跳过 add_job：{type(e).__name__}: {e}"
                )
                return
            logger.error(f"[{self.job_name}] 添加调度任务失败：{e}")

    async def _run_task_wrapper(self):
        logger.info(f"[{self.job_name}] 开始执行任务")
        try:
            await self.do_task()
        except Exception as e:
            logger.exception(f"[{self.job_name}] 任务执行失败: {e}")
        finally:
            if not self._terminated:
                self._schedule_next_job()
            logger.info(f"[{self.job_name}] 本轮任务完成")

    async def do_task(self):
        raise NotImplementedError

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        self.scheduler.remove_all_jobs()
        try:
            self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"[{self.job_name}] 关闭调度器时忽略异常：{e}")
        logger.info(f"[{self.job_name}] 已停止")


class AutoComment(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.comment_cron
        timezone = config.timezone
        offset = config.trigger.comment_offset
        super().__init__("AutoComment", cron, timezone, offset)
        self.cfg = config
        self.service = service
        self.sender = sender

    async def do_task(self):
        posts = await self.service.query_feeds(
            pos=0,
            num=20,
            no_self=True,
            no_commented=True,
        )
        for post in posts:
            try:
                await self.service.comment_posts(post)
                if self.cfg.trigger.like_when_comment:
                    await self.service.like_posts(post)
                await self.sender.send_admin_post(post, message="定时读说说")
            except Exception as e:
                logger.exception(
                    f"[{self.job_name}] 跳过说说评论失败: tid={post.tid}, uin={post.uin}, name={post.name}, error={e}"
                )


class AutoPublish(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.publish_cron
        timezone = config.timezone
        offset = config.trigger.publish_offset
        super().__init__("AutoPublish", cron, timezone, offset)
        self.cfg = config
        self.service = service
        self.sender = sender

    def _should_attach_meme(self) -> bool:
        raw = self.cfg.trigger.raw_data()
        return bool(raw.get("attach_meme_image", True))

    def _meme_max_bytes(self) -> int:
        raw = self.cfg.trigger.raw_data()
        try:
            return int(raw.get("meme_max_bytes", 300 * 1024) or (300 * 1024))
        except (TypeError, ValueError):
            return 300 * 1024

    async def do_task(self):
        try:
            import random

            from .hot_topics import pick_hot_material

            # Random material: chat-only / bilibili / xiaohongshu / mix(topic+chat)
            from . import feedback as fb

            # theme-weight bias can nudge source mode
            preferred_themes = fb.sample_themes(2)
            bias = fb.bias_mode_by_themes(preferred_themes)
            modes = ["chat", "bilibili", "xiaohongshu", "netease", "mix"]
            if bias in modes and random.random() < 0.55:
                mode = bias
            else:
                mode = random.choice(modes)
            topic = None
            material = None
            if mode != "chat":
                preferred = mode if mode in ("bilibili", "xiaohongshu", "netease") else None
                material = await pick_hot_material(preferred=preferred)
                # lightly re-roll if title clashes with low-weight themes
                try:
                    score = fb.score_title_against_themes(
                        str((material or {}).get("title") or ""), preferred_themes
                    )
                    if score < 1.5 and random.random() < 0.45:
                        material = await pick_hot_material(preferred=preferred)
                except Exception:
                    pass
                topic = material.get("prompt") or material.get("title")
                logger.info(
                    f"[{self.job_name}] material mode={mode}, source={material.get('source')}, title={material.get('title')}"
                )
            else:
                logger.info(f"[{self.job_name}] material mode=chat")

            # inject sampled theme preference into topic for this post
            try:
                if preferred_themes:
                    th = "、".join(preferred_themes)
                    if topic:
                        topic = f"{topic}（偏好题材：{th}）"
                    else:
                        topic = f"围绕这些题材写一句吐槽或感慨：{th}"
            except Exception:
                pass

            text = await self.service.llm.generate_post(topic=topic)

        except Exception as e:
            logger.error(f"自动生成内容失败：{e}")
            return
        if not text or not str(text).strip():
            logger.warning(f"[{self.job_name}] 生成内容为空，跳过本次发布")
            return

        
        from .hot_topics import with_source_footer
        from .smart_image import cleanup_temp_images, resolve_post_images

        text = with_source_footer(str(text), mode, material)

        images: list[str] = []
        image_kind = "none"
        temp_images: list[str] = []
        try:
            if self._should_attach_meme():
                try:
                    images, image_kind, temp_images = await resolve_post_images(
                        mode=mode,
                        material=material,
                        max_bytes=self._meme_max_bytes(),
                        enabled=True,
                    )
                except Exception as e:
                    logger.warning(f"[{self.job_name}] 选取配图失败，将发纯文字: {e}")
                    images, image_kind, temp_images = [], "none", []
            logger.info(
                f"[{self.job_name}] image_kind={image_kind}, images={images}"
            )

            if images:
                try:
                    post = await self.service.publish_post(text=text, images=images)
                except Exception as e:
                    logger.warning(
                        f"[{self.job_name}] 带图发布失败，回退纯文字: {e}"
                    )
                    post = await self.service.publish_post(text=text)
            else:
                post = await self.service.publish_post(text=text)
            await self.sender.send_admin_post(post, message="定时发说说")
            try:
                from . import feedback as fb
                themes = fb.derive_themes(
                    title=str((material or {}).get("title") or ""),
                    source=str((material or {}).get("source") or mode),
                    text=str(text),
                    mode=str(mode),
                )
                fb.record_auto_post(
                    tid=str(getattr(post, "tid", "") or ""),
                    themes=themes,
                    title=str((material or {}).get("title") or ""),
                    source=str((material or {}).get("source") or mode),
                    mode=str(mode),
                    text=str(text)[:120],
                    url=str((material or {}).get("url") or ""),
                )
            except Exception as e:
                logger.warning(f"[{self.job_name}] record feedback post fail: {e}")
        finally:
            cleanup_temp_images(temp_images)
