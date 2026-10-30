import plistlib
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from src.infrastructure.external.iphone import wda_signing
from src.infrastructure.external.iphone.wda_signing import (
    WdaSigning,
    ensure_wda_signed,
    read_wda_signing,
    renewal_reason,
    runner_app_path,
    wda_build_command,
)
from tests.unit.test_iphone_search_client import make_settings

UDID = "00008130-AAAA"
NOW = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)


def signing(**overrides):
    values = dict(
        bundle_id="com.example.wda.xctrunner",
        expires_at=NOW + timedelta(days=6),
        provisioned_udids=frozenset({UDID}),
    )
    values.update(overrides)
    return WdaSigning(**values)


def write_runner(derived, *, bundle_id="com.example.wda.xctrunner"):
    app = runner_app_path(derived)
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    (app / "embedded.mobileprovision").write_bytes(b"signed-profile")


def fake_security(profile):
    def run(args, **kwargs):
        assert args[:3] == ["security", "cms", "-D"]
        return subprocess.CompletedProcess(args, 0, stdout=plistlib.dumps(profile), stderr=b"")

    return run


def test_renewal_reason_accepts_valid_signing():
    assert renewal_reason(signing(), bundle_id="com.example.wda", udid=UDID, now=NOW) is None


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (None, "缺失"),
        (signing(bundle_id="other.xctrunner"), "不一致"),
        (signing(expires_at=None), "有效期"),
        (signing(expires_at=NOW + timedelta(hours=3)), "将于"),
        (signing(expires_at=NOW - timedelta(days=1)), "已于"),
        (signing(provisioned_udids=frozenset({"other"})), "未包含当前手机"),
    ],
)
def test_renewal_reason_detects_unusable_wda(current, expected):
    assert expected in renewal_reason(current, bundle_id="com.example.wda", udid=UDID, now=NOW)


def test_unrestricted_profile_does_not_require_device():
    current = signing(provisioned_udids=None)
    assert renewal_reason(current, bundle_id="com.example.wda", udid=UDID, now=NOW) is None


def test_read_wda_signing_parses_runner_bundle_and_profile(tmp_path, monkeypatch):
    write_runner(tmp_path)
    expires = datetime(2026, 11, 6, 8, 1, 4)
    monkeypatch.setattr(wda_signing.subprocess, "run", fake_security(
        {"ExpirationDate": expires, "ProvisionedDevices": [UDID]},
    ))

    result = read_wda_signing(tmp_path)

    assert result == WdaSigning(
        bundle_id="com.example.wda.xctrunner",
        expires_at=expires.replace(tzinfo=timezone.utc),
        provisioned_udids=frozenset({UDID}),
    )


def test_read_wda_signing_treats_cleaned_build_as_missing(tmp_path):
    (runner_app_path(tmp_path) / "PlugIns").mkdir(parents=True)
    assert read_wda_signing(tmp_path) is None


def test_build_command_uses_automatic_signing(tmp_path):
    settings = make_settings(wda_team_id="TEAM123456", wda_derived_data_path=tmp_path)
    command = wda_build_command(settings, UDID, tmp_path / "WebDriverAgent.xcodeproj")

    assert command[:2] == ["xcodebuild", "build-for-testing"]
    assert f"id={UDID}" in command
    assert "-allowProvisioningUpdates" in command
    assert "-allowProvisioningDeviceRegistration" in command
    assert "CODE_SIGN_STYLE=Automatic" in command
    assert "DEVELOPMENT_TEAM=TEAM123456" in command
    assert "PRODUCT_BUNDLE_IDENTIFIER=com.example.wda" in command


def test_ensure_wda_signed_skips_without_team_id(tmp_path, monkeypatch):
    monkeypatch.setattr(wda_signing, "_run_build", lambda *args: pytest.fail("不应构建"))
    ensure_wda_signed(make_settings(wda_derived_data_path=tmp_path), UDID, log=lambda _: None)


def test_ensure_wda_signed_skips_valid_build(tmp_path, monkeypatch):
    monkeypatch.setattr(wda_signing, "read_wda_signing", lambda path: signing(expires_at=datetime.now(timezone.utc) + timedelta(days=5)))
    monkeypatch.setattr(wda_signing, "_run_build", lambda *args: pytest.fail("不应构建"))
    settings = make_settings(wda_team_id="TEAM123456", wda_derived_data_path=tmp_path)

    ensure_wda_signed(settings, UDID, log=lambda _: None)


def test_ensure_wda_signed_rebuilds_expired_build(tmp_path, monkeypatch):
    project = tmp_path / "WebDriverAgent.xcodeproj"
    project.mkdir()
    states = [
        signing(expires_at=datetime.now(timezone.utc) - timedelta(hours=1)),
        signing(expires_at=datetime.now(timezone.utc) - timedelta(hours=1)),
        signing(expires_at=datetime.now(timezone.utc) + timedelta(days=7)),
    ]
    builds, logs = [], []
    monkeypatch.setattr(wda_signing, "read_wda_signing", lambda path: states.pop(0))
    monkeypatch.setattr(wda_signing, "_run_build", lambda settings, udid, path: builds.append((udid, path)))
    monkeypatch.setattr(wda_signing.tempfile, "gettempdir", lambda: str(tmp_path))
    settings = make_settings(
        wda_team_id="TEAM123456", wda_derived_data_path=tmp_path / "derived", wda_project_path=project,
    )

    ensure_wda_signed(settings, UDID, log=logs.append)

    assert builds == [(UDID, project)]
    assert "已于" in logs[0] and "已重新签名" in logs[-1]


def test_ensure_wda_signed_requires_project(tmp_path, monkeypatch):
    monkeypatch.setattr(wda_signing, "read_wda_signing", lambda path: None)
    settings = make_settings(wda_team_id="TEAM123456", wda_derived_data_path=tmp_path)

    with pytest.raises(RuntimeError, match="IOS_WDA_PROJECT_PATH"):
        ensure_wda_signed(settings, UDID, log=lambda _: None)


def test_ensure_wda_signed_reports_build_failure(tmp_path, monkeypatch):
    project = tmp_path / "WebDriverAgent.xcodeproj"
    project.mkdir()
    log_path = tmp_path / "logs" / "wda-build.log"
    monkeypatch.setattr(wda_signing, "WDA_BUILD_LOG_PATH", log_path)
    monkeypatch.setattr(wda_signing, "read_wda_signing", lambda path: None)
    monkeypatch.setattr(wda_signing.tempfile, "gettempdir", lambda: str(tmp_path))

    def failed_build(args, **kwargs):
        kwargs["stdout"].write("error: No Account for Team \"TEAM123456\".\n")
        return subprocess.CompletedProcess(args, 65)

    monkeypatch.setattr(wda_signing.subprocess, "run", failed_build)
    settings = make_settings(
        wda_team_id="TEAM123456", wda_derived_data_path=tmp_path / "derived", wda_project_path=project,
    )

    with pytest.raises(RuntimeError, match="退出码 65.*No Account for Team"):
        ensure_wda_signed(settings, UDID, log=lambda _: None)
