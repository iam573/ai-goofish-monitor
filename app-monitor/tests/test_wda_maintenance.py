from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import plistlib
import time

import pytest

from app_monitor.config import settings
from app_monitor.wda_maintenance import APP, WdaMaintenance, WdaNeedsAttention, WdaRetryLater


@pytest.fixture
def maintenance(database, tmp_path):
    return WdaMaintenance(replace(settings, ios_wda_auto_renew=True, ios_wda_bundle_id="test.WDA",
                                  ios_wda_team_id="", ios_wda_derived_data_path=tmp_path / "derived"), database)


def profile(*, expires=None, devices=None):
    return {"team": "TEAM", "identifier": "TEAM.test.WDA.xctrunner",
            "devices": ["phone"] if devices is None else devices,
            "expires": time.time() + 86400 if expires is None else expires}


def test_valid_profile_does_not_rebuild(maintenance, monkeypatch):
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: profile())
    monkeypatch.setattr(maintenance, "_build", lambda *a: pytest.fail("unnecessary build"))
    maintenance.ensure("phone")


@pytest.mark.parametrize("old", [profile(expires=0), profile(devices=["other"]),
                                  {**profile(), "identifier": "TEAM.wrong.xctrunner"}])
def test_invalid_profile_is_rebuilt_and_verified(maintenance, monkeypatch, old):
    current = [old]
    calls = []
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: current[0])

    def build(udid, team):
        calls.append((udid, team))
        current[0] = profile()

    monkeypatch.setattr(maintenance, "_build", build)
    maintenance.ensure("phone")
    maintenance.ensure("phone")
    assert calls == [("phone", "TEAM")]


def test_failed_renewal_is_bounded_and_retains_static_attention(maintenance, monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: profile(expires=0))

    def fail(*args):
        calls.append(args)
        raise RuntimeError("Your session has expired: secret account log")

    monkeypatch.setattr(maintenance, "_build", fail)
    for _ in range(2):
        with pytest.raises(WdaNeedsAttention, match="Apple ID"):
            maintenance.ensure("phone")
    assert len(calls) == 1
    state = maintenance.database.state_get(maintenance._key("phone"), {})
    assert state["code"] == "xcode_login"
    assert "secret" not in str(state)


def test_early_renewal_failure_keeps_still_valid_package_usable(maintenance, monkeypatch):
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: profile(expires=time.time() + 3600))
    monkeypatch.setattr(maintenance, "_build", lambda *a: (_ for _ in ()).throw(RuntimeError("No accounts")))
    maintenance.ensure("phone")
    maintenance.ensure("phone")
    assert maintenance.connect("phone", lambda: object())
    assert maintenance.database.state_get(maintenance._key("phone"))["pending"]


def test_missing_team_requires_configuration_without_build(maintenance, monkeypatch):
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: {})
    monkeypatch.setattr(maintenance, "_build", lambda *a: pytest.fail("no team available"))
    with pytest.raises(WdaNeedsAttention, match="开发团队"):
        maintenance.ensure("phone")


def test_locked_keychain_requires_local_action_without_starting_build(maintenance, monkeypatch):
    import app_monitor.wda_maintenance as module
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: profile(expires=0))
    monkeypatch.setattr(module, "signing_keychain_unlocked", lambda: False)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: pytest.fail("keychain locked"))
    with pytest.raises(WdaNeedsAttention, match="钥匙串访问"):
        maintenance.ensure("phone")
    assert maintenance.database.state_get(maintenance._key("phone"))["code"] == "keychain"


def test_transient_renewal_failure_retries_without_operator_alert(maintenance, monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: profile(expires=0))

    def fail(*args):
        calls.append(args)
        raise RuntimeError("The network connection was lost")

    monkeypatch.setattr(maintenance, "_build", fail)
    for _ in range(2):
        with pytest.raises(WdaRetryLater):
            maintenance.ensure("phone")
    assert len(calls) == 1
    assert maintenance.database.state_get(maintenance._key("phone"), {}) == {}


def test_connection_repairs_once_then_requires_attention(maintenance, monkeypatch):
    attempts = []
    monkeypatch.setattr(maintenance, "ensure", lambda udid, **kw: attempts.append(kw))
    calls = []

    def factory():
        calls.append(1)
        raise RuntimeError("WebDriverAgent xcodebuild code 65")

    with pytest.raises(WdaNeedsAttention):
        maintenance.connect("phone", factory)
    assert attempts == [{}, {"force": True}] and len(calls) == 2
    assert maintenance.database.state_get(maintenance._key("phone"))["pending"]


def test_success_is_quiet_and_ends_attention_incident(maintenance, monkeypatch):
    maintenance.attention("phone", "No accounts")
    monkeypatch.setattr(maintenance, "ensure", lambda *a, **kw: None)
    driver = object()
    assert maintenance.connect("phone", lambda: driver) is driver
    assert maintenance.database.state_get(maintenance._key("phone")) == {}


@pytest.mark.parametrize("proxy", [False, True])
def test_both_collectors_use_shared_maintenance_session_hook(maintenance, monkeypatch, proxy):
    from app_monitor.iphone_collector import IPhoneCollector
    from app_monitor.iphone_proxy_collector import IPhoneProxyCollector

    collector = IPhoneProxyCollector(maintenance.settings, None) if proxy else IPhoneCollector(maintenance.settings)
    collector.wda_maintenance = maintenance
    attempts = []
    monkeypatch.setattr(maintenance, "ensure", lambda udid, **kw: attempts.append(udid))
    driver = object()
    assert collector._open_session("phone", lambda: driver) is driver
    assert attempts == ["phone"]


def test_profile_reader_only_returns_required_metadata(monkeypatch, tmp_path):
    from datetime import datetime
    from app_monitor.wda_maintenance import profile_info
    import app_monitor.wda_maintenance as module

    payload = {"ExpirationDate": datetime(2030, 1, 1), "TeamIdentifier": ["TEAM"],
               "ProvisionedDevices": ["phone"], "Entitlements": {"application-identifier": "TEAM.test.WDA.xctrunner"},
               "DeveloperCertificates": [b"private metadata"]}
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=plistlib.dumps(payload)))
    info = profile_info(tmp_path)
    assert info["team"] == "TEAM" and info["expires"] > time.time()
    assert "private" not in str(info)


@pytest.mark.parametrize("valid", [True, False])
def test_build_validates_staging_before_replacing_existing_products(maintenance, tmp_path, monkeypatch, valid):
    import app_monitor.wda_maintenance as module

    project = tmp_path / ".appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"
    project.mkdir(parents=True)
    original = "PRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;"
    (project / "project.pbxproj").write_text(original)
    old = maintenance.settings.ios_wda_derived_data_path / APP
    old.mkdir(parents=True)
    (old / "old").write_text("preserve me")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "signing_keychain_unlocked", lambda: True)
    monkeypatch.setenv("SDKROOT", "bad-sdk")
    monkeypatch.setattr(maintenance, "_read_profile", lambda path: profile() if valid else {})

    def popen(command, **kwargs):
        assert "-allowProvisioningUpdates" in command
        assert "-allowProvisioningDeviceRegistration" in command
        assert kwargs["start_new_session"] and "SDKROOT" not in kwargs["env"]
        copied = Path(command[command.index("-project") + 1]) / "project.pbxproj"
        assert "test.WDA;" in copied.read_text()
        target = Path(command[command.index("-derivedDataPath") + 1]) / APP
        target.mkdir(parents=True)
        (target / "new").write_text("signed app")
        (target / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "test.WDA.xctrunner"}))
        return SimpleNamespace(returncode=0, communicate=lambda **kw: ("build output", None))

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: SimpleNamespace(returncode=0))
    if valid:
        maintenance._build("phone", "TEAM")
        assert (old / "new").exists() and not (old / "old").exists()
    else:
        with pytest.raises(RuntimeError, match="未替换"):
            maintenance._build("phone", "TEAM")
        assert (old / "old").read_text() == "preserve me"
    assert (project / "project.pbxproj").read_text() == original
