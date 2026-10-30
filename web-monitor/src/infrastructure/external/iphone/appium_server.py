"""复用已运行的 Appium 服务；配置了 APPIUM_BIN 时可在本机自动拉起。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from src.infrastructure.external.iphone.settings import IPhoneSettings

APPIUM_LOG_PATH = Path("logs") / "appium.log"


def appium_ready(url: str) -> bool:
    try:
        with urlopen(f"{url}/status", timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def ensure_appium_server(settings: IPhoneSettings) -> None:
    url = settings.appium_server_url
    if appium_ready(url):
        return
    parsed = urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or settings.appium_bin is None:
        raise RuntimeError(
            f"Appium 服务未就绪：{url}。请先启动 Appium，或在 .env 配置 APPIUM_BIN / APPIUM_HOME 让任务自动拉起。"
        )
    if not settings.appium_bin.exists():
        raise RuntimeError(f"APPIUM_BIN 不存在：{settings.appium_bin}")
    APPIUM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if settings.appium_home is not None:
        env["APPIUM_HOME"] = str(settings.appium_home)
    with APPIUM_LOG_PATH.open("a", encoding="utf-8") as log_file:
        # 独立会话运行，任务进程结束后服务保留，下次任务直接复用。
        process = subprocess.Popen(
            [str(settings.appium_bin), "--port", str(parsed.port or 4723), "--log-level", "warn"],
            env=env, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True,
        )
    for _ in range(40):
        if appium_ready(url):
            print(f"LOG: 已启动 Appium pid={process.pid}，日志 {APPIUM_LOG_PATH}")
            return
        if process.poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError(f"Appium 启动失败，请查看 {APPIUM_LOG_PATH}")
