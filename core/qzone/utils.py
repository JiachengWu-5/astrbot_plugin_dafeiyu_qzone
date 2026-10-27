import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Union
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot.api import logger

BytesOrStr = Union[str, bytes]  # noqa: UP007


async def download_file(source: str) -> bytes | None:
    """读取本地图片或下载网络图片。

    Args:
        source: 本地绝对/相对路径、file:// URI 或 HTTP(S) URL。

    Returns:
        图片字节；读取失败时返回 None。
    """
    source = source.strip()
    if not source:
        return None

    try:
        # Windows drive paths like C:\... are misread by urlparse as scheme "c".
        win_abs = len(source) >= 3 and source[1] == ":" and source[2] in "\\/"
        unc = source.startswith("\\")
        if win_abs or unc:
            local_path = Path(source)
            if not local_path.is_file():
                logger.error(f"本地图片不存在或不是文件: {local_path}")
                return None
            return await asyncio.to_thread(local_path.read_bytes)

        parsed = urlparse(source)

        if parsed.scheme in ("http", "https"):
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(source) as response:
                    response.raise_for_status()
                    return await response.read()

        if parsed.scheme == "file":
            local_path = Path(unquote(parsed.path))
            # file:///C:/Users/... on Windows
            if local_path.as_posix().startswith("/") and len(local_path.parts) > 1 and len(local_path.parts[0]) == 0 and len(local_path.parts[1]) == 2 and local_path.parts[1][1:] == ":":
                local_path = Path(local_path.parts[1] + "\\" + "\\".join(local_path.parts[2:]))
            elif str(local_path).startswith("/") and len(str(local_path)) > 2 and str(local_path)[2] == ":":
                local_path = Path(str(local_path)[1:])
        elif parsed.scheme == "":
            local_path = Path(source)
        else:
            logger.error(f"不支持的图片地址协议: {parsed.scheme}")
            return None

        if not local_path.is_file():
            logger.error(f"本地图片不存在或不是文件: {local_path}")
            return None

        return await asyncio.to_thread(local_path.read_bytes)
    except Exception as e:
        logger.error(f"图片读取失败: {source}: {e}", exc_info=True)
        return None


async def normalize_images(images: Sequence[BytesOrStr] | None) -> list[bytes]:
    """
    将 str/bytes 混合列表统一转成 bytes 列表：
    - str -> 下载后转 bytes（下载失败则忽略）
    - bytes -> 原样保留
    - None -> 空列表
    """
    if images is None:
        return []

    cleaned: list[bytes] = []
    for item in images:
        if isinstance(item, bytes):
            cleaned.append(item)
        elif isinstance(item, str):
            file = await download_file(item)
            if file is not None:
                cleaned.append(file)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned
