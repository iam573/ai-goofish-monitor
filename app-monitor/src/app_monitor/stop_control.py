"""Cooperative run cancellation shared by async handlers and phone workers."""
from contextvars import ContextVar
from contextlib import contextmanager
from threading import Event
import os
import signal
import subprocess
import time

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


def run_process(command, *, timeout, env=None):
    """Capture a short-lived command and cancel its whole process group on stop."""
    checkpoint()
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            checkpoint()
            try:
                output, error = process.communicate(timeout=.2)
                checkpoint()
                return subprocess.CompletedProcess(command, process.returncode, output or '', error or '')
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout) from None
    except BaseException:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        raise
