"""Maintain the existing Xcode signing identity; never handle Apple ID credentials."""
from __future__ import annotations

import hashlib
import ctypes
import os
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import timezone
from pathlib import Path

from .config import ROOT
from .instance_lock import _exclusive
from .stop_control import checkpoint

APP = Path("Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app")
RENEW_BEFORE = 12 * 3600
REPAIR_INTERVAL = 10 * 60


class WdaNeedsAttention(RuntimeError):
    pass


class WdaRetryLater(RuntimeError):
    """A transient repair failure that does not require an operator notification."""


def transient_error(error: str) -> bool:
    return any(word in error.lower() for word in (
        "timed out", "network connection was lost", "could not connect to the server",
        "internet connection appears to be offline", "could not resolve host", "构建超时"))


def signing_keychain_unlocked() -> bool | None:
    """Inspect the default keychain status without reading keys or passwords."""
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        security.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        security.SecKeychainGetStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        core.CFRelease.argtypes = [ctypes.c_void_p]
        keychain, status = ctypes.c_void_p(), ctypes.c_uint32()
        if security.SecKeychainCopyDefault(ctypes.byref(keychain)) != 0:
            return None
        try:
            if security.SecKeychainGetStatus(keychain, ctypes.byref(status)) == 0:
                return bool(status.value & 1)
        finally:
            core.CFRelease(keychain)
    except (OSError, AttributeError):
        pass
    return None


def profile_info(app: Path) -> dict:
    completed = subprocess.run(
        ["security", "cms", "-D", "-i", str(app / "embedded.mobileprovision")],
        capture_output=True, timeout=15, check=True,
    )
    profile = plistlib.loads(completed.stdout)
    expires = profile["ExpirationDate"].replace(tzinfo=timezone.utc).timestamp()
    return {"expires": expires, "team": (profile.get("TeamIdentifier") or [""])[0],
            "devices": profile.get("ProvisionedDevices") or [],
            "identifier": profile.get("Entitlements", {}).get("application-identifier", "")}


def action_for_error(error: str) -> tuple[str, str]:
    text = error.lower()
    if "keychain locked" in text or "errsecinternalcomponent" in text or "user interaction is not allowed" in text:
        return "keychain", "请在 Mac「钥匙串访问」中解锁登录钥匙串，并允许 codesign 使用开发证书私钥；程序将在后续搜索自动重试。密码仅在系统窗口输入。"
    if any(word in text for word in ("no accounts", "session has expired", "authentication", "not logged in", "sign in", "credentials")):
        return "xcode_login", "请在 Xcode → Settings → Accounts 中重新登录 Apple ID，然后点击立即重试。"
    if any(word in text for word in ("developer mode", "developermode")):
        return "developer_mode", "请在手机「设置 → 隐私与安全性」开启开发者模式，重启确认后点击立即重试。"
    if any(word in text for word in ("locked", "passcode", "trust", "invalid code signature", "untrusted")):
        return "phone_confirmation", "请解锁手机，确认信任电脑及「设置 → 通用 → VPN与设备管理」中的开发者，再点击立即重试。"
    if any(word in text for word in ("provision", "signing", "certificate", "development team")):
        return "signing", "请在 Xcode 检查开发团队、签名证书和设备注册；修复后点击立即重试。"
    if any(word in text for word in ("platform", "disk image", "sdk", "not supported")):
        return "xcode_platform", "请在 Xcode 检查该 iOS 版本的设备支持组件和平台兼容性，然后点击立即重试。"
    return "wda_repair", "WDA 自动恢复未成功，请在 Xcode 的 Devices and Simulators 检查设备连接与 WDA 签名，然后点击立即重试。"


class WdaMaintenance:
    def __init__(self, settings, database):
        self.settings, self.database = settings, database

    def _key(self, udid: str) -> str:
        return "wda_attention_" + hashlib.sha256(udid.encode()).hexdigest()

    def attention(self, udid: str, error: str) -> str:
        code, action = action_for_error(error)
        return self._attention_action(udid, code, action)

    def _attention_action(self, udid: str, code: str, action: str) -> str:
        key = self._key(udid)
        self.database.state_set(key, {"code": code, "action": action, "pending": True})
        return action

    def recovered(self, udid: str) -> None:
        self.database.state_set(self._key(udid), {})

    def _read_profile(self, app: Path) -> dict:
        try:
            return profile_info(app)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            return {}

    def _valid(self, profile: dict, udid: str, margin: float = 0) -> bool:
        bundle = self.settings.ios_wda_bundle_id.removesuffix(".xctrunner") + ".xctrunner"
        return (profile.get("expires", 0) > time.time() + margin
                and udid in profile.get("devices", [])
                and profile.get("identifier") == f"{profile.get('team')}.{bundle}")

    def ensure(self, udid: str, *, force: bool = False) -> bool:
        checkpoint()
        if not self.settings.ios_wda_auto_renew:
            return True
        derived = self.settings.ios_wda_derived_data_path
        profile = self._read_profile(derived / APP)
        if not force and self._valid(profile, udid, RENEW_BEFORE):
            return True
        digest = hashlib.sha256(str(derived.resolve()).encode()).hexdigest()
        lock = Path(tempfile.gettempdir()) / f"goofish-wda-{os.getuid()}-{digest}.lock"
        with _exclusive(lock, "另一项任务正在维护 WDA，请稍后重试"):
            profile = self._read_profile(derived / APP)
            if not force and self._valid(profile, udid, RENEW_BEFORE):
                return True
            key = "wda_build_" + digest
            state = self.database.state_get(key, {})
            if time.time() - state.get("attempt", 0) < REPAIR_INTERVAL:
                if not force and self._valid(profile, udid):
                    return not bool(state.get("action"))
                if force and not state.get("action") and self._valid(profile, udid):
                    return True  # A just-rebuilt package needs no second build.
                if state.get("transient"):
                    raise WdaRetryLater("WDA 自动维护暂未完成，将在后续搜索重试")
                if state.get("code"):
                    self._attention_action(udid, state["code"], state["action"])
                if not state.get("action"):
                    raise WdaRetryLater("WDA 自动维护暂未完成，将在后续搜索重试")
                raise WdaNeedsAttention(state.get("action") or "WDA 正在等待恢复，请处理已有提示后稍后重试")
            self.database.state_set(key, {"attempt": time.time()})
            team = self.settings.ios_wda_team_id or profile.get("team", "")
            try:
                if not team:
                    raise RuntimeError("未配置 development team；请设置 IOS_WDA_TEAM_ID")
                self._build(udid, team)
                fresh = self._read_profile(derived / APP)
                if not self._valid(fresh, udid):
                    raise RuntimeError("更新后 provisioning profile 仍无效")
                self.database.state_set(key, {"attempt": time.time()})
                return True
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                if transient_error(str(exc)) or isinstance(exc, subprocess.TimeoutExpired):
                    self.database.state_set(key, {"attempt": time.time(), "transient": True})
                    if not force and self._valid(profile, udid):
                        return False
                    raise WdaRetryLater("WDA 自动维护暂未完成，将在后续搜索重试") from None
                action = self.attention(udid, str(exc))
                code, _ = action_for_error(str(exc))
                self.database.state_set(key, {"attempt": time.time(), "action": action, "code": code})
                # An unsuccessful early renewal must not stop a still-valid WDA.
                if not force and self._valid(profile, udid):
                    return False
                raise WdaNeedsAttention(action) from None

    def _build(self, udid: str, team: str) -> None:
        checkpoint()
        if signing_keychain_unlocked() is False:
            raise RuntimeError("signing keychain locked")
        derived = self.settings.ios_wda_derived_data_path
        derived.mkdir(parents=True, exist_ok=True)
        project = ROOT / ".appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"
        if not project.is_dir():
            raise RuntimeError("WDA 工程缺失，请安装项目固定版本的 Appium XCUITest 驱动")
        with tempfile.TemporaryDirectory(prefix=".wda-renew-", dir=derived.parent) as directory:
            staging = Path(directory)
            # Work on a private project copy; Appium and other devices may use the installed one.
            shutil.copytree(project.parent, staging / "source", ignore=shutil.ignore_patterns("node_modules", ".git"))
            copied_project = staging / "source" / project.name
            project_file = copied_project / "project.pbxproj"
            text = project_file.read_text()
            bundle = self.settings.ios_wda_bundle_id.removesuffix(".xctrunner")
            if not re.fullmatch(r"[A-Za-z0-9.-]+", bundle):
                raise RuntimeError("WDA bundle ID 配置无效")
            text = re.sub(r"PRODUCT_BUNDLE_IDENTIFIER = [^;]*WebDriverAgentRunner[^;]*;",
                          f"PRODUCT_BUNDLE_IDENTIFIER = {bundle};", text)
            project_file.write_text(text)
            candidate = staging / "derived"
            env = {key: value for key, value in os.environ.items() if key not in {
                "SDKROOT", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "LIBRARY_PATH", "CFLAGS", "CPPFLAGS"}}
            command = ["xcodebuild", "-quiet", "-project", str(copied_project), "-scheme", "WebDriverAgentRunner",
                       "-configuration", "Debug", "-sdk", "iphoneos", "-destination", f"id={udid}",
                       "-derivedDataPath", str(candidate), "-allowProvisioningUpdates",
                       "-allowProvisioningDeviceRegistration", "build-for-testing",
                       f"DEVELOPMENT_TEAM={team}", "CODE_SIGN_STYLE=Automatic", "CODE_SIGN_IDENTITY=Apple Development"]
            checkpoint()
            process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 600
                while True:
                    checkpoint()
                    try:
                        output, _ = process.communicate(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("WDA 构建超时，请检查 Xcode 和网络") from None
            except BaseException:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate()
                raise
            checkpoint()
            if process.returncode:
                # Raw output is used locally for classification only.
                errors = [line for line in output.splitlines() if re.search(
                    r"(?i)(?:^|\s)(?:fatal )?error:|error domain=|session has expired|no accounts|errSecInternalComponent", line)]
                raise RuntimeError("\n".join(errors)[:16000] if errors else output[-4000:])
            if not self._valid(self._read_profile(candidate / APP), udid):
                raise RuntimeError("新构建 provisioning profile 仍无效，未替换现有 WDA")
            info = plistlib.loads((candidate / APP / "Info.plist").read_bytes())
            bundle_id = self.settings.ios_wda_bundle_id.removesuffix(".xctrunner") + ".xctrunner"
            if info.get("CFBundleIdentifier") != bundle_id:
                raise RuntimeError("新构建 WDA bundle ID 不匹配，未替换现有 WDA")
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(candidate / APP)],
                           capture_output=True, timeout=30, check=True)
            products = derived / "Build/Products"
            checkpoint()
            products.parent.mkdir(parents=True, exist_ok=True)
            backup = staging / "previous-products"
            if products.exists():
                products.rename(backup)
            try:
                (candidate / "Build/Products").rename(products)
            except OSError:
                if backup.exists():
                    backup.rename(products)
                raise

    def connect(self, udid: str, factory):
        checkpoint()
        can_clear = self.ensure(udid) is not False
        try:
            driver = factory()
        except Exception as exc:
            text = str(exc).lower()
            if not any(word in text for word in ("webdriveragent", "xcodebuild", "code signature", "provision")):
                raise
            try:
                can_clear = self.ensure(udid, force=True) is not False
                driver = factory()
            except (WdaRetryLater, WdaNeedsAttention):
                raise
            except Exception as retry_error:
                action = self.attention(udid, str(retry_error))
                raise WdaNeedsAttention(action) from None
        if can_clear:
            self.recovered(udid)
        return driver
