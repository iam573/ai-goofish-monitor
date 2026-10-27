import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_monitor.instance_lock import monitor_instance


def test_duplicate_process_is_rejected_and_lock_survives_holder_crash(tmp_path):
    path=tmp_path/'monitor.sqlite3'
    code='''
import sys
from pathlib import Path
from app_monitor.instance_lock import monitor_instance
with monitor_instance(Path(sys.argv[1])):
    print('locked',flush=True)
    sys.stdin.read()
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')}
    child=subprocess.Popen([sys.executable,'-c',code,str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,text=True,env=env)
    try:
        assert child.stdout.readline().strip()=='locked'
        with pytest.raises(RuntimeError,match='已有监控服务'):
            with monitor_instance(path):
                pytest.fail('Second scheduler acquired an owned database')
    finally:
        child.kill();child.communicate(timeout=5)
    with monitor_instance(path):
        pass  # The OS releases ownership on process exit, regardless of stale PID text.


def test_alias_paths_share_owner_and_independent_databases_do_not(tmp_path):
    path=tmp_path/'monitor.sqlite3';path.touch()
    alias=tmp_path/'alias.sqlite3';alias.symlink_to(path)
    with monitor_instance(path):
        with pytest.raises(RuntimeError):
            with monitor_instance(alias):pass
        with monitor_instance(tmp_path/'other.sqlite3'):pass
    with monitor_instance(alias):pass


async def test_lifespan_rejects_duplicate_before_touching_active_runs(tmp_path,monkeypatch):
    from app_monitor import api
    path=tmp_path/'monitor.sqlite3'
    def forbidden():raise AssertionError('Duplicate startup must not initialize DB or close active runs')
    monkeypatch.setattr(api,'database',SimpleNamespace(path=path,initialize=forbidden))
    monkeypatch.setattr(api,'monitor_service',SimpleNamespace(close_interrupted_runs=forbidden))
    with monitor_instance(path):
        with pytest.raises(RuntimeError,match='已有监控服务'):
            async with api.lifespan(api.app):pass


@pytest.mark.parametrize('channel',['ui','proxy'])
def test_both_channels_refuse_to_replace_an_owned_device_session(tmp_path,monkeypatch,channel):
    from app_monitor import instance_lock
    from app_monitor.config import Settings
    from app_monitor.iphone_collector import IPhoneCollector
    from app_monitor.iphone_proxy_collector import IPhoneProxyCollector
    monkeypatch.setattr(instance_lock.tempfile,'gettempdir',lambda:str(tmp_path))
    # Holding the device lease simulates another backend or a still-running worker.
    with instance_lock.device_session('test-phone'):
        with pytest.raises(RuntimeError,match='另一个采集任务'):
            if channel=='ui':
                IPhoneCollector(Settings())._collect('test-phone','','iPhone 15','厦门',[],['256'],0,lambda *a:True)
            else:
                IPhoneProxyCollector(Settings(),None)._collect_proxy('test-phone','','iPhone 15','厦门',[],['256'],0,lambda *a:True,None,None,'192.168.1.20')
        # A different phone is independent.
        with instance_lock.device_session('other-phone'):pass
    with instance_lock.device_session('test-phone'):pass


async def test_cancelled_async_caller_keeps_phone_owned_until_worker_exits(tmp_path,monkeypatch):
    import asyncio
    import threading
    from app_monitor import instance_lock
    monkeypatch.setattr(instance_lock.tempfile,'gettempdir',lambda:str(tmp_path))
    started=threading.Event();finish=threading.Event();done=threading.Event()
    class Worker:
        @instance_lock.exclusive_device
        def collect(self,udid):
            started.set()
            assert finish.wait(3)
    def run():
        try:Worker().collect('test-phone')
        finally:done.set()
    caller=asyncio.create_task(asyncio.to_thread(run))
    try:
        assert await asyncio.to_thread(started.wait,2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):await caller
        with pytest.raises(RuntimeError,match='另一个采集任务'):
            with instance_lock.device_session('test-phone'):pass
    finally:
        finish.set()
        assert await asyncio.to_thread(done.wait,2)
    with instance_lock.device_session('test-phone'):pass
