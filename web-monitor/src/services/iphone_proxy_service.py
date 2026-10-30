"""按设备通道配置启停 iPhone 代理采集服务；代理随 Web 服务进程常驻，任务进程按手机 IP 订阅。"""

from __future__ import annotations

import sys
import threading

from src.infrastructure.external.iphone.proxy.capture import ProxyService
from src.infrastructure.external.iphone.settings import IPhoneSettings
from src.infrastructure.persistence.iphone_device_selection import (
    load_iphone_device_channels,
    proxy_client_ips,
)

_lock = threading.Lock()
_service: ProxyService | None = None


def get_proxy_service() -> ProxyService:
    global _service
    with _lock:
        if _service is None:
            _service = ProxyService(IPhoneSettings.from_env())
        return _service


def sync_proxy_service() -> ProxyService:
    """有手机使用代理采集时启动并更新白名单，全部切回原有采集时停止以释放端口。"""
    service = get_proxy_service()
    clients = proxy_client_ips(load_iphone_device_channels())
    service.configure(clients)
    if not clients:
        service.stop()
    elif sys.platform == "darwin":
        try:
            service.start()
        except (OSError, RuntimeError) as exc:
            print(f"iPhone 代理采集启动失败: {exc}")
    return service


def shutdown_proxy_service() -> None:
    with _lock:
        service = _service
    if service is not None:
        service.stop()
