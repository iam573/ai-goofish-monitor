"""Reuse a running local Appium server or launch the repository's pinned install."""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from .config import ROOT, Settings
from .stop_control import checkpoint

WDA_PORT = 8101

logger = logging.getLogger(__name__)
_owned_process: subprocess.Popen | None = None
_server_lock = threading.Lock()
_local_ports: dict[str, int] = {}
_ports_lock = threading.Lock()


def wda_local_port(udid: str) -> int:
    """Concurrent sessions need distinct forwarded Mac ports: Appium frees a busy port by
    dropping whichever device holds it. The phone-side WDA port stays WDA_PORT."""
    with _ports_lock:
        return _local_ports.setdefault(udid, WDA_PORT + len(_local_ports))


def wda_port_capabilities(udid: str) -> dict[str, int]:
    return {"appium:wdaLocalPort": wda_local_port(udid), "appium:wdaRemotePort": WDA_PORT}


def _ready(url: str) -> bool:
    try:
        with urlopen(f"{url}/status", timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def ensure_appium_server(settings: Settings) -> None:
    checkpoint()
    if _ready(settings.appium_server_url):
        return
    with _server_lock:
        _start_server(settings)


def _start_server(settings: Settings) -> None:
    global _owned_process
    url = settings.appium_server_url
    if _ready(url):
        return
    parsed = urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"Appium 服务未就绪：{url}")
    binary = ROOT / "node_modules" / ".bin" / "appium"
    driver = ROOT / ".appium" / "node_modules" / "appium-xcuitest-driver"
    if not binary.exists() or not driver.exists():
        raise RuntimeError("iPhone 依赖未安装：在 app-monitor 下运行 npm ci 和 APPIUM_HOME=$PWD/.appium npx appium driver install xcuitest@12.13.3")
    if _owned_process is None or _owned_process.poll() is not None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        log_path = settings.data_dir / "appium.log"
        log_file = log_path.open("a", encoding="utf-8")
        try:
            _owned_process = subprocess.Popen(
                [str(binary), "--port", str(parsed.port or 4723), "--log-level", "warn"],
                cwd=ROOT, env={**os.environ, "APPIUM_HOME": str(ROOT / ".appium")},
                stdout=log_file, stderr=subprocess.STDOUT,
            )
        finally:
            log_file.close()
        logger.info("已启动 Appium pid=%s log=%s", _owned_process.pid, log_path)
    for _ in range(40):
        checkpoint()
        if _ready(url):
            return
        if _owned_process.poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError(f"Appium 启动失败，请查看 {settings.data_dir / 'appium.log'}")


def _stop_owned_server() -> None:
    if _owned_process is not None and _owned_process.poll() is None:
        _owned_process.terminate()


atexit.register(_stop_owned_server)
