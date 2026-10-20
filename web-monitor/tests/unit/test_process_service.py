import asyncio
import sys
from types import SimpleNamespace

from src.services.process_service import ProcessService


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, returncode: int = 0):
        self.returncode = returncode
        self._done.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


def test_process_service_marks_task_stopped_when_process_exits(monkeypatch, tmp_path):
    fake_process = FakeProcess(pid=4321)
    events = []

    async def run_scenario():
        service = ProcessService()
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            consecutive_failures=0,
            paused_until=None,
        )

        stopped = asyncio.Event()

        async def on_started(task_id: int):
            events.append(("started", task_id))

        async def on_stopped(task_id: int):
            events.append(("stopped", task_id))
            stopped.set()

        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        result = await service.start_task(0, "task-a")
        assert result.success is True
        assert events == [("started", 0)]
        assert service.is_running(0) is True

        fake_process.finish(0)
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert ("stopped", 0) in events
        assert service.is_running(0) is False

    asyncio.run(run_scenario())


def test_process_service_reindexes_runtime_maps_after_delete():
    service = ProcessService()
    proc_a = object()
    proc_c = object()
    watcher_a = object()
    watcher_c = object()

    service.processes = {0: proc_a, 2: proc_c}
    service.log_paths = {0: "a.log", 2: "c.log"}
    service.task_names = {0: "A", 2: "C"}
    service.exit_watchers = {0: watcher_a, 2: watcher_c}

    service.reindex_after_delete(1)

    assert service.processes == {0: proc_a, 1: proc_c}
    assert service.log_paths == {0: "a.log", 1: "c.log"}
    assert service.task_names == {0: "A", 1: "C"}
    assert service.exit_watchers == {0: watcher_a, 1: watcher_c}


def test_process_service_adds_debug_limit_arg_when_env_enabled(monkeypatch):
    monkeypatch.setenv("SPIDER_DEBUG_LIMIT", "1")
    service = ProcessService()

    command = service._build_spawn_command("task-a")

    assert command == [
        sys.executable,
        "-u",
        "spider_v2.py",
        "--task-name",
        "task-a",
        "--debug-limit",
        "1",
    ]


def test_process_service_returns_detail_when_failure_guard_skips_start():
    async def run_scenario():
        service = ProcessService()
        disabled = []
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=True,
            should_notify=False,
            reason="登录态失效",
            consecutive_failures=3,
            paused_until=None,
        )

        async def on_paused(task_id: int, detail: str):
            disabled.append((task_id, detail))

        service.set_lifecycle_hooks(on_paused=on_paused)

        result = await service.start_task(0, "task-a")

        assert result.success is False
        assert "失败保护暂停" in result.detail
        assert "自动禁用" in result.detail
        assert "登录态失效" in result.detail
        assert disabled == [(0, result.detail)]

    asyncio.run(run_scenario())


def test_process_service_disables_task_when_process_exit_opens_failure_guard(monkeypatch, tmp_path):
    fake_process = FakeProcess(pid=4322)
    disabled = []

    async def run_scenario():
        service = ProcessService()
        service._resolve_cookie_path = lambda _task_name: None
        decisions = [
            SimpleNamespace(
                skip=False,
                should_notify=False,
                reason="not_paused",
                consecutive_failures=2,
                paused_until=None,
            ),
            SimpleNamespace(
                skip=True,
                should_notify=False,
                reason="TimeoutError: Timeout 30000ms exceeded",
                consecutive_failures=3,
                paused_until=None,
            ),
        ]

        def should_skip_start(*_args, **_kwargs):
            return decisions.pop(0)

        service.failure_guard.should_skip_start = should_skip_start
        paused = asyncio.Event()

        async def on_paused(task_id: int, detail: str):
            disabled.append((task_id, detail))
            paused.set()

        service.set_lifecycle_hooks(on_paused=on_paused)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        result = await service.start_task(5, "task-a")
        assert result.success is True

        fake_process.finish(0)
        await asyncio.wait_for(paused.wait(), timeout=1)

        assert len(disabled) == 1
        assert disabled[0][0] == 5
        assert "自动禁用" in disabled[0][1]
        assert "TimeoutError" in disabled[0][1]

    asyncio.run(run_scenario())
