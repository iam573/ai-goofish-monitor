"""同一台 iPhone 同一时间只允许一个任务进程操作。"""

from __future__ import annotations

import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator


def device_lock_path(udid: str) -> Path:
    return Path(tempfile.gettempdir()) / f"goofish-iphone-{udid}.lock"


def iphone_device_busy(udid: str) -> bool:
    import fcntl

    with device_lock_path(udid).open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


@contextmanager
def any_iphone_device_lock(
    udids: list[str],
    timeout_seconds: int,
    stop_event: threading.Event | None = None,
    on_wait=None,
) -> Iterator[str]:
    """按顺序锁住第一台空闲的手机并返回其 UDID；全部被占用时排队等待。"""
    import fcntl

    if not udids:
        raise RuntimeError("没有可用的 iPhone")
    with ExitStack() as stack:
        handles = [stack.enter_context(device_lock_path(udid).open("a+")) for udid in udids]
        deadline = time.monotonic() + timeout_seconds
        waited = False
        while True:
            for udid, handle in zip(udids, handles):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    yield udid
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return
            if not waited and on_wait is not None:
                on_wait()
            waited = True
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("等待 iPhone 时任务被停止")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待 iPhone 空闲超时（{timeout_seconds} 秒），其他任务仍在使用手机")
            time.sleep(1)


@contextmanager
def iphone_device_lock(
    udid: str,
    timeout_seconds: int,
    stop_event: threading.Event | None = None,
    on_wait=None,
) -> Iterator[None]:
    with any_iphone_device_lock([udid], timeout_seconds, stop_event, on_wait):
        yield
