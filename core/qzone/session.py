import asyncio
from http.cookies import SimpleCookie
from time import monotonic

from astrbot.api import logger

from ..config import PluginConfig
from .model import QzoneContext


class QzoneSession:
    """QQ 登录上下文"""

    DOMAIN = "user.qzone.qq.com"

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._ctx: QzoneContext | None = None
        self._last_refresh_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_ctx(self) -> QzoneContext:
        async with self._lock:
            if not self._ctx or self._is_cookie_expired():
                self._ctx = await self._refresh_ctx_locked()
            return self._ctx

    async def get_uin(self) -> int:
        ctx = await self.get_ctx()
        return ctx.uin

    async def get_nickname(self) -> str:
        ctx = await self.get_ctx()
        uin = str(ctx.uin)
        if not self.cfg.client:
            return uin
        try:
            info = await self.cfg.client.get_login_info()
            return info.get("nickname") or uin
        except Exception:
            return uin

    async def invalidate(self) -> None:
        async with self._lock:
            self._ctx = None
            self._last_refresh_at = 0.0

    async def login(self) -> QzoneContext:
        logger.info("正在登录 QQ 空间")
        async with self._lock:
            self._ctx = await self._refresh_ctx_locked()
            logger.info(f"登录成功，uin={self._ctx.uin}")
            return self._ctx

    async def _refresh_ctx_locked(self) -> QzoneContext:
        if not self.cfg.client:
            raise RuntimeError("CQHttp 实例不存在")

        # NapCat 有时对 user.qzone.qq.com 返回空串，依次尝试常见域
        domains = [
            self.DOMAIN,
            "qzone.qq.com",
            "qq.com",
            ""
        ]
        cookies_str = ""
        last_payload = None
        for domain in domains:
            try:
                kwargs = {"domain": domain} if domain else {}
                payload = await self.cfg.client.get_cookies(**kwargs)
            except TypeError:
                payload = await self.cfg.client.get_cookies(domain=domain or self.DOMAIN)
            except Exception as e:
                logger.warning(f"get_cookies domain={domain!r} fail: {e}")
                continue
            last_payload = payload
            if isinstance(payload, dict):
                cookies_str = str(payload.get("cookies") or "").strip()
            if cookies_str:
                logger.info(f"get_cookies ok domain={domain!r} len={len(cookies_str)}")
                break
        if not cookies_str:
            raise RuntimeError(f"get_cookies 未返回可用 Cookie (last={last_payload!r})")

        c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
        uin_text = c.get("uin", "0")
        uin_raw = uin_text[1:] if uin_text[:1].lower() == "o" else uin_text
        uin = int(uin_raw) if uin_raw.isdigit() else 0
        if not uin:
            raise RuntimeError("Cookie 中缺少合法 uin")

        self._last_refresh_at = monotonic()
        return QzoneContext(
            uin=uin,
            skey=c.get("skey", ""),
            p_skey=c.get("p_skey", "") or c.get("skey", ""),
        )

    def _is_cookie_expired(self) -> bool:
        ttl = max(int(self.cfg.cookie_ttl), 0)
        if ttl <= 0:
            return False
        if self._last_refresh_at <= 0:
            return True
        return monotonic() - self._last_refresh_at >= ttl
