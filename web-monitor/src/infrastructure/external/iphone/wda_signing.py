"""预编译 WebDriverAgent 的签名检查与自动续签。

免费开发团队的描述文件只有 7 天有效期，构建目录也可能被系统清理。配置 IOS_WDA_TEAM_ID 后，
连接手机前发现产物缺失、签名将过期、bundle ID 不符或描述文件未包含当前手机时，
用 Xcode 自动签名重新 build-for-testing，之后仍以 usePrebuiltWDA 方式连接。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from src.infrastructure.external.iphone.settings import IPhoneSettings

RENEW_BEFORE = timedelta(hours=24)
BUILD_TIMEOUT_SECONDS = 900
WDA_BUILD_LOG_PATH = Path("logs") / "wda-build.log"
_BUNDLED_WDA_PROJECT = Path(
    "node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"
)
# 宿主机的头文件/库搜索路径会被带进 iOS 交叉编译导致失败。
_HOST_BUILD_ENV = ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "LIBRARY_PATH")


@dataclass(frozen=True)
class WdaSigning:
    bundle_id: str
    expires_at: datetime | None
    # None 表示描述文件不限定设备（如企业签名）
    provisioned_udids: frozenset[str] | None


def runner_app_path(derived_data_path: Path) -> Path:
    return derived_data_path / "Build" / "Products" / "Debug-iphoneos" / "WebDriverAgentRunner-Runner.app"


def _decode_profile(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["security", "cms", "-D", "-i", str(path)], capture_output=True, timeout=15, check=False,
        )
        return plistlib.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return {}


def read_wda_signing(derived_data_path: Path | None) -> WdaSigning | None:
    if derived_data_path is None:
        return None
    app = runner_app_path(derived_data_path)
    info_path, profile_path = app / "Info.plist", app / "embedded.mobileprovision"
    if not info_path.is_file() or not profile_path.is_file():
        return None
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    profile = _decode_profile(profile_path)
    expires_at = profile.get("ExpirationDate")
    if isinstance(expires_at, datetime):
        expires_at = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = None
    devices = profile.get("ProvisionedDevices")
    return WdaSigning(
        bundle_id=str(info.get("CFBundleIdentifier") or ""),
        expires_at=expires_at,
        provisioned_udids=frozenset(str(udid) for udid in devices) if isinstance(devices, list) else None,
    )


def renewal_reason(
    signing: WdaSigning | None,
    *,
    bundle_id: str,
    udid: str = "",
    now: datetime | None = None,
) -> str | None:
    if signing is None:
        return "WDA 构建产物缺失或不完整"
    expected_bundle_id = f"{bundle_id}.xctrunner"
    if signing.bundle_id != expected_bundle_id:
        return f"WDA bundle ID 为 {signing.bundle_id or '空'}，与配置的 {expected_bundle_id} 不一致"
    if signing.expires_at is None:
        return "无法读取 WDA 描述文件有效期"
    now = now or datetime.now(timezone.utc)
    if signing.expires_at - now <= RENEW_BEFORE:
        verb = "已" if signing.expires_at <= now else "将"
        return f"WDA 签名{verb}于 {signing.expires_at.astimezone():%m-%d %H:%M} 过期"
    if udid and signing.provisioned_udids is not None and udid not in signing.provisioned_udids:
        return "WDA 描述文件未包含当前手机"
    return None


def wda_project_path(settings: IPhoneSettings) -> Path | None:
    candidates = [settings.wda_project_path]
    if settings.appium_home is not None:
        candidates.append(settings.appium_home / _BUNDLED_WDA_PROJECT)
    return next((path for path in candidates if path is not None and path.exists()), None)


def wda_build_command(settings: IPhoneSettings, udid: str, project: Path) -> list[str]:
    return [
        "xcodebuild", "build-for-testing",
        "-project", str(project),
        "-scheme", "WebDriverAgentRunner",
        "-destination", f"id={udid}",
        "-derivedDataPath", str(settings.wda_derived_data_path),
        "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration",
        "CODE_SIGN_STYLE=Automatic",
        f"DEVELOPMENT_TEAM={settings.wda_team_id}",
        f"PRODUCT_BUNDLE_IDENTIFIER={settings.wda_bundle_id}",
    ]


@contextmanager
def _build_lock() -> Iterator[None]:
    import fcntl

    with (Path(tempfile.gettempdir()) / "goofish-wda-build.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _last_error_line(log_path: Path) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    errors = [line.strip() for line in lines if "error:" in line.lower()]
    return errors[-1][:300] if errors else ""


def _run_build(settings: IPhoneSettings, udid: str, project: Path) -> None:
    WDA_BUILD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in _HOST_BUILD_ENV}
    with WDA_BUILD_LOG_PATH.open("w", encoding="utf-8") as log_file:
        try:
            result = subprocess.run(
                wda_build_command(settings, udid, project),
                stdout=log_file, stderr=subprocess.STDOUT, env=env,
                timeout=BUILD_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"WDA 自动签名构建超时（{BUILD_TIMEOUT_SECONDS} 秒），详见 {WDA_BUILD_LOG_PATH}") from exc
    if result.returncode != 0:
        detail = _last_error_line(WDA_BUILD_LOG_PATH)
        raise RuntimeError(
            f"WDA 自动签名构建失败（xcodebuild 退出码 {result.returncode}）"
            f"{'：' + detail if detail else ''}。详见 {WDA_BUILD_LOG_PATH}；"
            f"常见原因：Xcode 未登录团队 {settings.wda_team_id} 的 Apple 账号、登录钥匙串已锁定或手机未解锁"
        )


def ensure_wda_signed(settings: IPhoneSettings, udid: str, log: Callable[[str], None] = print) -> None:
    """未配置 IOS_WDA_TEAM_ID 时保持原行为，直接使用现有预编译 WDA。"""
    if not settings.wda_team_id or settings.wda_derived_data_path is None:
        return

    def current_reason() -> str | None:
        signing = read_wda_signing(settings.wda_derived_data_path)
        return renewal_reason(signing, bundle_id=settings.wda_bundle_id, udid=udid)

    if current_reason() is None:
        return
    project = wda_project_path(settings)
    if project is None:
        raise RuntimeError("找不到 WebDriverAgent 工程，无法自动签名；请配置 APPIUM_HOME 或 IOS_WDA_PROJECT_PATH")
    with _build_lock():
        # 排队期间其他任务可能已完成续签
        reason = current_reason()
        if reason is None:
            return
        log(f"LOG: {reason}，正在用 Xcode 自动签名重新构建 WebDriverAgent（约 1-2 分钟）...")
        _run_build(settings, udid, project)
        signing = read_wda_signing(settings.wda_derived_data_path)
        reason = renewal_reason(signing, bundle_id=settings.wda_bundle_id, udid=udid)
        if reason is not None:
            raise RuntimeError(f"WDA 重新构建后仍不可用：{reason}；详见 {WDA_BUILD_LOG_PATH}")
        log(f"LOG: WebDriverAgent 已重新签名，有效期至 {signing.expires_at.astimezone():%m-%d %H:%M}")
