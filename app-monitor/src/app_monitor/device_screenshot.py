"""Read a phone's current screen without creating or replacing an Appium session."""
from __future__ import annotations

import io
import shutil
import subprocess
import threading
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .appium_server import WDA_PORT
from .config import ROOT

_capture_lock = threading.Lock()


class ScreenshotBusy(RuntimeError):
    pass


def capture_screenshot(udid: str) -> bytes:
    if not _capture_lock.acquire(blocking=False):
        raise ScreenshotBusy('正在获取截图，请稍候再试')
    try:
        node = shutil.which('node')
        if not node:
            raise RuntimeError('截图组件未就绪，请检查 Node.js 安装')
        try:
            result = subprocess.run(
                [node, str(Path(__file__).with_suffix('.cjs')), str(ROOT), udid, str(WDA_PORT)],
                capture_output=True, timeout=18, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError('截图超时，请确认手机连接后重试') from None
        except OSError:
            raise RuntimeError('截图组件启动失败，请稍后重试') from None
        if result.returncode or not result.stdout:
            raise RuntimeError('截图通道未就绪，请确认手机已连接，并先运行一次该设备的搜索')
        # Only verified image bytes leave this boundary; never return subprocess logs.
        try:
            if len(result.stdout) > 18 * 1024 * 1024:
                raise ValueError('Screenshot too large')
            with Image.open(io.BytesIO(result.stdout)) as image:
                if image.format != 'PNG':
                    raise ValueError('Expected PNG')
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError):
            raise RuntimeError('未取得有效截图，请刷新重试') from None
        return result.stdout
    finally:
        _capture_lock.release()
