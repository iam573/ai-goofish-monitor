from dataclasses import replace
from datetime import datetime, timezone
import json
import time
import plistlib

import pytest

from app_monitor.config import settings
from app_monitor.remote_wda import RemoteWdaMaintenance
from app_monitor.wda_maintenance import APP, WdaMaintenance


@pytest.fixture
def remote(database, tmp_path):
    cfg = replace(settings, ios_wda_auto_renew=True, ios_wda_remote_host='test-builder',
                  ios_wda_remote_team_id='TESTTEAM',
                  ios_wda_preinstalled_devices=json.dumps({'phone': 'test.WDA.xctrunner'}))
    instance = RemoteWdaMaintenance(cfg, database, 'phone')
    instance.settings = replace(instance.settings, ios_wda_derived_data_path=tmp_path)
    return instance


def valid_profile(identifier='TESTTEAM.test.WDA.xctrunner'):
    return {'team': 'TESTTEAM', 'identifier': identifier, 'devices': ['phone'],
            'expires': time.time() + 86400 * 30}


@pytest.mark.parametrize('identifier,valid', [
    ('TESTTEAM.test.WDA.xctrunner', True), ('TESTTEAM.*', True),
    ('TESTTEAM.test.*', True), ('TESTTEAM.other.*', False),
    ('OTHER.*', False), ('TESTTEAM.test.WDA2.xctrunner', False),
])
def test_profile_scope_matches_expected_team_and_bundle(remote, identifier, valid):
    assert remote._valid(valid_profile(identifier), 'phone') is valid
    assert remote._valid(valid_profile(identifier), 'unregistered-phone') is False


def test_usable_expiry_is_limited_by_signing_certificate(remote, monkeypatch):
    profile = valid_profile('TESTTEAM.*')
    profile['expires'] = datetime(2027, 11, 4, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(WdaMaintenance, '_read_profile', lambda *a: profile)
    def run(command, **kw):
        return 'notAfter=Jul 10 02:33:38 2027 GMT\n' if command[0] == 'openssl' else ''
    monkeypatch.setattr('app_monitor.remote_wda.run', run)
    assert remote._read_profile(remote.settings.ios_wda_derived_data_path / APP)['expires'] == datetime(
        2027, 7, 10, 2, 33, 38, tzinfo=timezone.utc).timestamp()


def test_valid_cached_wda_installs_once_and_runs_without_ssh(remote, monkeypatch):
    app = remote.settings.ios_wda_derived_data_path / APP
    (app / '_CodeSignature').mkdir(parents=True)
    (app / 'embedded.mobileprovision').write_bytes(b'profile')
    (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'runner'}))
    (app / 'runner').write_bytes(b'executable v1')
    resources = app / '_CodeSignature/CodeResources'
    resources.write_bytes(b'signed app v1')
    monkeypatch.setattr(remote, '_read_profile', lambda *a: valid_profile('TESTTEAM.*'))
    monkeypatch.setattr(remote, '_build', lambda *a: pytest.fail('valid cache must not require remote Mac'))
    commands = []
    monkeypatch.setattr('app_monitor.remote_wda.run', lambda cmd, **kw: commands.append(cmd) or '')
    assert remote.ensure('phone') is True
    assert remote.ensure('phone') is True
    assert len(commands) == 1
    assert commands[0][:6] == ['xcrun', 'devicectl', 'device', 'install', 'app', '--device']
    # A new signed app using the same provisioning profile still requires install.
    resources.write_bytes(b'signed app v2')
    remote.ensure('phone')
    assert len(commands) == 2
    (app / 'runner').write_bytes(b'executable v2')
    remote.ensure('phone')
    assert len(commands) == 3
    # A forced repair reinstalls the still-valid package without contacting SSH.
    remote.ensure('phone', force=True)
    assert len(commands) == 4


def test_remote_renewal_selected_only_for_mapped_device(remote, monkeypatch):
    cfg = replace(remote.settings, ios_wda_preinstalled_devices=json.dumps({'phone': 'test.WDA.xctrunner'}))
    maintenance = WdaMaintenance(cfg, remote.database)
    calls = []
    monkeypatch.setattr(RemoteWdaMaintenance, 'ensure', lambda self, udid, **kw: calls.append(udid) or True)
    monkeypatch.setattr(maintenance, '_read_profile', lambda *a: valid_profile())
    monkeypatch.setattr(maintenance, '_build', lambda *a: pytest.fail('unrelated device must retain valid local WDA'))
    assert maintenance.ensure('phone') is True
    assert calls == ['phone']
    # Disabling renewal retains externally installed WDA without reaching SSH.
    maintenance.settings = replace(cfg, ios_wda_auto_renew=False)
    assert maintenance.ensure('phone') is True
    assert calls == ['phone']


def test_remote_keychain_issue_remains_automatically_retryable(remote):
    action = remote.attention('phone', 'Remote WDA errSecInternalComponent')
    assert '远端' in action
    assert remote.blocking_attention('phone') is None
    assert remote.database.state_get(remote._key('phone'))['code'] == 'keychain'
