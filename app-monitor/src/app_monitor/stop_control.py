"""Cooperative run cancellation shared by async handlers and phone workers."""
from contextvars import ContextVar
from contextlib import contextmanager
from threading import Event

STOP_REASON = "用户终止监控"
_event: ContextVar[Event | None] = ContextVar("monitor_stop_event", default=None)


class MonitorStopped(BaseException):
    """Bypass collector recovery handlers while still running cleanup blocks."""


@contextmanager
def stop_scope(event: Event):
    token = _event.set(event)
    try:
        yield
    finally:
        _event.reset(token)


def stop_requested() -> bool:
    event = _event.get()
    return event is not None and event.is_set()


def checkpoint() -> None:
    if stop_requested():
        raise MonitorStopped(STOP_REASON)


def guard_driver(driver):
    if _event.get() is None:
        return driver
    execute = driver.execute

    def guarded(command, params=None):
        if command != "quit":
            checkpoint()
        result = execute(command, params)
        if command != "quit":
            checkpoint()
        return result

    driver.execute = guarded
    try:
        checkpoint()
    except MonitorStopped:
        try:
            driver.quit()
        except Exception:
            pass
        raise
    return driver
