from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app_monitor.config import settings
from app_monitor.iphone_collector import IPhoneCollector


def configured():
    return replace(settings, ios_wda_remote_host='', ios_wda_preinstalled_devices=json.dumps({'phone': 'test.external.xctrunner'}))


def test_external_device_never_builds_or_reads_local_profile(database, monkeypatch):
    from app_monitor.wda_maintenance import WdaMaintenance
    maintenance = WdaMaintenance(configured(), database)
    monkeypatch.setattr(maintenance, '_read_profile', lambda *a: pytest.fail('read unrelated local profile'))
    monkeypatch.setattr(maintenance, '_build', lambda *a: pytest.fail('re-signed external WDA'))
    assert maintenance.ensure('phone') is True
    assert maintenance.ensure('phone', force=True) is True


def test_only_mapped_device_uses_external_connection(monkeypatch):
    collector = IPhoneCollector(configured())
    calls = []
    def external(cfg, udid, caps, factory):
        calls.append(udid)
        caps['appium:webDriverAgentUrl'] = 'http://test'
        return factory(caps)
    monkeypatch.setattr('app_monitor.iphone_collector.connect_preinstalled_wda', external)
    caps = {}
    assert collector._open_session('phone', caps, lambda options: options.copy()) == {'appium:webDriverAgentUrl': 'http://test'}
    assert collector._open_session('other', {}, lambda options: 'original') == 'original'
    assert calls == ['phone']


def test_failed_external_start_cleans_up_forwarder(monkeypatch):
    import app_monitor.preinstalled_wda as module
    process = SimpleNamespace(stdout=None)
    closed = []
    monkeypatch.setattr(module.shutil, 'which', lambda _: 'node')
    monkeypatch.setattr(module, 'run_process', lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: process)
    monkeypatch.setattr(module, '_close_forwarder', lambda p: closed.append(p))
    class Selector:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def register(self, *a): pass
        def select(self, **kw): return []
    monkeypatch.setattr(module.selectors, 'DefaultSelector', Selector)
    with pytest.raises(RuntimeError, match='USB'):
        module.connect_preinstalled_wda(configured(), 'phone', {}, lambda options: pytest.fail('opened session'))
    assert closed == [process]


def test_success_and_factory_failure_close_owned_forwarder(monkeypatch):
    import io
    import app_monitor.preinstalled_wda as module
    process = SimpleNamespace(stdout=io.StringIO('{"port":12345}\n'), poll=lambda: None)
    closed = []
    launches = []
    monkeypatch.setattr(module.shutil, 'which', lambda _: 'node')
    def launch(args, **kwargs):
        launches.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module, 'run_process', launch)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: process)
    monkeypatch.setattr(module, '_close_forwarder', lambda p: closed.append(p))
    class Selector:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def register(self, *a): pass
        def select(self, **kw): return [True]
    monkeypatch.setattr(module.selectors, 'DefaultSelector', Selector)
    def status(*a, **kw):
        return io.StringIO(json.dumps({'value': {'ready': True, 'build': {
            'productBundleIdentifier': 'test.external.xctrunner'}}}))
    monkeypatch.setattr(module, 'urlopen', status)
    driver = SimpleNamespace(quit=lambda: 'quit')
    caps = {'appium:usePrebuiltWDA': True, 'appium:derivedDataPath': '/unrelated'}
    result = module.connect_preinstalled_wda(configured(), 'phone', caps, lambda options: driver)
    assert caps['appium:webDriverAgentUrl'] == 'http://127.0.0.1:12345'
    assert 'appium:usePrebuiltWDA' not in caps
    assert 'appium:derivedDataPath' not in caps
    assert launches[0][-1] == 'test.external.xctrunner'
    assert launches[1][-1] == configured().bundle_id
    assert not closed
    assert result.quit() == 'quit'
    assert closed == [process]
    process.stdout = io.StringIO('{"port":12345}\n')
    def fail(options): raise RuntimeError('session failed')
    with pytest.raises(RuntimeError, match='session failed'):
        module.connect_preinstalled_wda(configured(), 'phone', {}, fail)
    assert closed == [process, process]
