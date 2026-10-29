import asyncio
from threading import Event

import pytest

from app_monitor.stop_control import MonitorStopped, checkpoint, guard_driver, stop_scope


def test_stop_after_inflight_command_blocks_followups_but_allows_quit():
    event, calls = Event(), []

    class Driver:
        def execute(self, command, params=None):
            calls.append(command)
            if command != "quit":
                event.set()
            return {}

        def quit(self):
            self.execute("quit")

    with stop_scope(event):
        driver = guard_driver(Driver())
        with pytest.raises(MonitorStopped):
            driver.execute("read")
        with pytest.raises(MonitorStopped):
            driver.execute("click")
        driver.quit()
    assert calls == ["read", "quit"]
    checkpoint()  # Scope reset must not leak stop into unrelated work.


async def test_stop_interrupts_proxy_wait_in_worker_thread(tmp_path):
    from dataclasses import replace
    from app_monitor.config import settings
    from app_monitor.proxy_capture import ProxyCapture

    proxy = ProxyCapture(replace(settings, proxy_runtime=tmp_path))
    event, ready = Event(), Event()

    def worker():
        ready.set()
        return proxy.wait("192.168.1.2", 0, timeout=60)

    with stop_scope(event):
        task = asyncio.create_task(asyncio.to_thread(worker))
        assert await asyncio.to_thread(ready.wait, 2)
        event.set()
        with pytest.raises(MonitorStopped):
            await asyncio.wait_for(task, 1)
