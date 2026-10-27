#!/usr/bin/env python3
"""Build separately installable, Personal Team-signed Xianyu IPA clones."""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import timezone
from pathlib import Path
from typing import Iterable

import lief
from PIL import Image, ImageDraw, ImageFont


ORIGINAL_BUNDLE_ID = "com.taobao.fleamarket"
DEFAULT_CLONE_BUNDLE_PREFIX = "com.efrenwu.goofish"
DEFAULT_TEAM_ID = os.environ.get("GOOFISH_TEAM_ID")
DEFAULT_IDENTITY_HINT = os.environ.get("GOOFISH_SIGNING_IDENTITY")
DEFAULT_DEVICE = os.environ.get("GOOFISH_DEVICE_UDID")
DEFAULT_IPA = Path(os.environ.get("GOOFISH_IPA", "闲鱼iOS.ipa"))
SHIM_NAME = "BundleIdentityShim.dylib"
SCRIPT_DIR = Path(__file__).resolve().parent


class CloneError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[闲鱼多开] {message}", flush=True)


def run(
    command: Iterable[os.PathLike[str] | str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    argv = [os.fspath(item) for item in command]
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        details = "\n".join(part.strip() for part in (exc.stdout, exc.stderr) if part and part.strip())
        suffix = f"\n{details}" if details else ""
        raise CloneError(f"命令失败（退出码 {exc.returncode}）：{' '.join(argv)}{suffix}") from exc


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise CloneError(f"找不到命令 {name}，请先安装或启用对应的 Xcode 命令行工具。")


def resolve_identity(hint: str) -> str:
    result = run(["security", "find-identity", "-v", "-p", "codesigning"], capture=True)
    candidates: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        match = re.search(r'\)\s+([0-9A-F]{40})\s+"([^"]+)"', line)
        if match and hint.lower() in (match.group(1) + " " + match.group(2)).lower():
            candidates.append((match.group(1), match.group(2)))
    if not candidates:
        raise CloneError(f"没有找到与 {hint!r} 匹配的有效签名证书。")
    if len(candidates) != 1:
        names = "、".join(name for _, name in candidates)
        raise CloneError(f"{hint!r} 匹配到多个证书：{names}；请用 --identity 指定 SHA-1。")
    digest, name = candidates[0]
    log(f"签名证书：{name}（{digest}）")
    return digest


def write_provision_project(project_root: Path) -> Path:
    source_dir = project_root / "ProvisionProbe"
    project_dir = project_root / "ProvisionProbe.xcodeproj"
    source_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    (source_dir / "main.m").write_text(
        """#import <UIKit/UIKit.h>
@interface AppDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@end
@implementation AppDelegate
- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    self.window = [[UIWindow alloc] initWithFrame:UIScreen.mainScreen.bounds];
    self.window.rootViewController = [UIViewController new];
    [self.window makeKeyAndVisible];
    return YES;
}
@end
int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil, NSStringFromClass(AppDelegate.class));
    }
}
""",
        encoding="utf-8",
    )
    (project_dir / "project.pbxproj").write_text(
        """// !$*UTF8*$!
{
 archiveVersion = 1;
 classes = {};
 objectVersion = 56;
 objects = {
  100000000000000000000001 = {isa = PBXBuildFile; fileRef = 100000000000000000000002; };
  100000000000000000000002 = {isa = PBXFileReference; lastKnownFileType = sourcecode.c.objc; path = main.m; sourceTree = "<group>"; };
  100000000000000000000003 = {isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = ProvisionProbe.app; sourceTree = BUILT_PRODUCTS_DIR; };
  100000000000000000000004 = {isa = PBXGroup; children = (100000000000000000000005, 100000000000000000000006,); sourceTree = "<group>"; };
  100000000000000000000005 = {isa = PBXGroup; children = (100000000000000000000002,); path = ProvisionProbe; sourceTree = "<group>"; };
  100000000000000000000006 = {isa = PBXGroup; children = (100000000000000000000003,); name = Products; sourceTree = "<group>"; };
  100000000000000000000007 = {isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = (100000000000000000000001,); runOnlyForDeploymentPostprocessing = 0; };
  100000000000000000000008 = {isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0; };
  100000000000000000000009 = {isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0; };
  10000000000000000000000A = {isa = PBXNativeTarget; buildConfigurationList = 10000000000000000000000B; buildPhases = (100000000000000000000007, 100000000000000000000008, 100000000000000000000009,); buildRules = (); dependencies = (); name = ProvisionProbe; productName = ProvisionProbe; productReference = 100000000000000000000003; productType = "com.apple.product-type.application"; };
  10000000000000000000000C = {isa = PBXProject; attributes = {BuildIndependentTargetsInParallel = 1; LastUpgradeCheck = 2700; TargetAttributes = {10000000000000000000000A = {CreatedOnToolsVersion = 27.0; ProvisioningStyle = Automatic;};};}; buildConfigurationList = 10000000000000000000000D; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base,); mainGroup = 100000000000000000000004; productRefGroup = 100000000000000000000006; projectDirPath = ""; projectRoot = ""; targets = (10000000000000000000000A,); };
  10000000000000000000000B = {isa = XCConfigurationList; buildConfigurations = (10000000000000000000000E, 10000000000000000000000F,); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;};
  10000000000000000000000D = {isa = XCConfigurationList; buildConfigurations = (100000000000000000000010, 100000000000000000000011,); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;};
  10000000000000000000000E = {isa = XCBuildConfiguration; buildSettings = {CODE_SIGN_STYLE = Automatic; DEVELOPMENT_TEAM = TEAM_PLACEHOLDER; GENERATE_INFOPLIST_FILE = YES; INFOPLIST_KEY_CFBundleDisplayName = ProvisionProbe; IPHONEOS_DEPLOYMENT_TARGET = 16.0; PRODUCT_BUNDLE_IDENTIFIER = com.example.ProvisionProbe; PRODUCT_NAME = "$(TARGET_NAME)"; SDKROOT = iphoneos; SUPPORTED_PLATFORMS = iphoneos; TARGETED_DEVICE_FAMILY = 1;}; name = Debug;};
  10000000000000000000000F = {isa = XCBuildConfiguration; buildSettings = {CODE_SIGN_STYLE = Automatic; DEVELOPMENT_TEAM = TEAM_PLACEHOLDER; GENERATE_INFOPLIST_FILE = YES; INFOPLIST_KEY_CFBundleDisplayName = ProvisionProbe; IPHONEOS_DEPLOYMENT_TARGET = 16.0; PRODUCT_BUNDLE_IDENTIFIER = com.example.ProvisionProbe; PRODUCT_NAME = "$(TARGET_NAME)"; SDKROOT = iphoneos; SUPPORTED_PLATFORMS = iphoneos; TARGETED_DEVICE_FAMILY = 1;}; name = Release;};
  100000000000000000000010 = {isa = XCBuildConfiguration; buildSettings = {CLANG_ENABLE_MODULES = YES;}; name = Debug;};
  100000000000000000000011 = {isa = XCBuildConfiguration; buildSettings = {CLANG_ENABLE_MODULES = YES;}; name = Release;};
 };
 rootObject = 10000000000000000000000C;
}
""",
        encoding="utf-8",
    )
    return project_dir


def create_profile(bundle_id: str, team_id: str, work_dir: Path) -> tuple[Path, dict]:
    project_root = work_dir / "provision-project"
    project_path = write_provision_project(project_root)
    derived = work_dir / "derived-data"
    log(f"为 {bundle_id} 申请/刷新个人开发描述文件")
    run(
        [
            "xcodebuild",
            "-project",
            project_path,
            "-scheme",
            "ProvisionProbe",
            "-configuration",
            "Debug",
            "-sdk",
            "iphoneos",
            "-destination",
            "generic/platform=iOS",
            "-derivedDataPath",
            derived,
            "-allowProvisioningUpdates",
            f"DEVELOPMENT_TEAM={team_id}",
            f"PRODUCT_BUNDLE_IDENTIFIER={bundle_id}",
            "CODE_SIGN_STYLE=Automatic",
            "build",
        ],
        capture=True,
    )
    app_path = derived / "Build/Products/Debug-iphoneos/ProvisionProbe.app"
    profile_path = app_path / "embedded.mobileprovision"
    if not profile_path.is_file():
        raise CloneError(f"Xcode 构建成功，但没有找到描述文件：{profile_path}")
    decoded = run(["security", "cms", "-D", "-i", profile_path], capture=True)
    profile = plistlib.loads(decoded.stdout.encode("utf-8"))
    application_identifier = profile.get("Entitlements", {}).get("application-identifier")
    expected = f"{team_id}.{bundle_id}"
    if application_identifier != expected:
        raise CloneError(f"描述文件 App ID 不匹配：实际 {application_identifier!r}，预期 {expected!r}")
    return profile_path, profile


def update_icon_declarations(info: dict) -> None:
    phone_icons = info.setdefault("CFBundleIcons", {})
    phone_icons.pop("CFBundleAlternateIcons", None)
    phone_icons["CFBundlePrimaryIcon"] = {"CFBundleIconFiles": ["GoofishCloneIcon60x60"]}

    ipad_icons = info.setdefault("CFBundleIcons~ipad", {})
    ipad_icons.pop("CFBundleAlternateIcons", None)
    ipad_icons["CFBundlePrimaryIcon"] = {
        "CFBundleIconFiles": ["GoofishCloneIcon60x60", "GoofishCloneIcon76x76"]
    }
    info["CFBundleIconFiles"] = ["GoofishCloneIcon60x60"]


def load_badge_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("/System/Library/Fonts/SFNSRounded.ttf"),
        Path("/System/Library/Fonts/SFNS.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def add_number_badge(source: Path, destination: Path, number: int) -> None:
    decoded = destination.with_name(f".{destination.name}.decoded.png")
    try:
        run(["xcrun", "pngcrush", "-revert-iphone-optimizations", "-q", source, decoded])
        image = Image.open(decoded).convert("RGBA")
        image.load()
    finally:
        decoded.unlink(missing_ok=True)
    width, height = image.size
    shortest = min(width, height)
    label = str(number)
    font_size = max(16, int(shortest * (0.31 if len(label) == 1 else 0.25)))
    font = load_badge_font(font_size)
    draw = ImageDraw.Draw(image)
    text_box = draw.textbbox((0, 0), label, font=font, stroke_width=0)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    padding_x = int(shortest * 0.085)
    padding_y = int(shortest * 0.055)
    badge_width = max(int(shortest * 0.37), text_width + 2 * padding_x)
    badge_height = max(int(shortest * 0.37), text_height + 2 * padding_y)
    edge = int(shortest * 0.045)
    right = width - edge
    top = edge
    left = right - badge_width
    bottom = top + badge_height
    radius = badge_height // 2
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        fill=(255, 59, 48, 255),
        outline=(255, 255, 255, 255),
        width=max(2, int(shortest * 0.025)),
    )
    text_x = left + (badge_width - text_width) / 2
    text_y = top + (badge_height - text_height) / 2 - text_box[1]
    draw.text((text_x, text_y), label, font=font, fill=(255, 255, 255, 255))
    image.save(destination, format="PNG", optimize=True)


def customize_app(app_path: Path, bundle_id: str, number: int) -> Path:
    info_path = app_path / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    executable_name = info.get("CFBundleExecutable")
    if not executable_name:
        raise CloneError("Info.plist 缺少 CFBundleExecutable。")
    executable = app_path / executable_name
    if not executable.is_file():
        raise CloneError(f"找不到主程序：{executable}")

    info["CFBundleIdentifier"] = bundle_id
    info["CFBundleDisplayName"] = "闲鱼"
    info["CFBundleName"] = "闲鱼"
    update_icon_declarations(info)
    info_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY, sort_keys=False))

    phone_icon = app_path / "AppIcon60x60@2x.png"
    ipad_icon = app_path / "AppIcon76x76@2x~ipad.png"
    if not phone_icon.is_file() or not ipad_icon.is_file():
        raise CloneError("原 IPA 缺少预期的主图标 AppIcon60x60@2x.png 或 AppIcon76x76@2x~ipad.png。")
    add_number_badge(phone_icon, app_path / "GoofishCloneIcon60x60@2x.png", number)
    add_number_badge(ipad_icon, app_path / "GoofishCloneIcon76x76@2x~ipad.png", number)

    plugins = app_path / "PlugIns"
    if plugins.exists():
        shutil.rmtree(plugins)
    return executable


def compile_and_inject_shim(app_path: Path, executable: Path, work_dir: Path) -> None:
    frameworks = app_path / "Frameworks"
    frameworks.mkdir(exist_ok=True)
    shim_path = frameworks / SHIM_NAME
    sdk = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], capture=True).stdout.strip()
    run(
        [
            "xcrun",
            "clang",
            "-dynamiclib",
            "-fobjc-arc",
            "-fblocks",
            "-arch",
            "arm64",
            "-isysroot",
            sdk,
            "-miphoneos-version-min=16.0",
            "-framework",
            "Foundation",
            "-install_name",
            f"@rpath/{SHIM_NAME}",
            SCRIPT_DIR / "BundleIdentityShim.m",
            "-o",
            shim_path,
        ]
    )

    binary = lief.parse(str(executable))
    if binary is None:
        raise CloneError(f"LIEF 无法解析主程序：{executable}")
    load_path = f"@rpath/{SHIM_NAME}"
    if load_path not in [library.name for library in binary.libraries]:
        binary.add_library(load_path)
        rewritten = work_dir / f"{executable.name}.injected"
        binary.write(str(rewritten))
        shutil.copymode(executable, rewritten)
        os.replace(rewritten, executable)


def make_entitlements(bundle_id: str, team_id: str, destination: Path) -> None:
    app_identifier = f"{team_id}.{bundle_id}"
    entitlements = {
        "application-identifier": app_identifier,
        "com.apple.developer.team-identifier": team_id,
        "get-task-allow": True,
        "keychain-access-groups": [app_identifier],
    }
    destination.write_bytes(plistlib.dumps(entitlements, fmt=plistlib.FMT_XML, sort_keys=False))


def remove_old_signatures(app_path: Path) -> None:
    for signature_dir in sorted(app_path.rglob("_CodeSignature"), reverse=True):
        if signature_dir.is_dir():
            shutil.rmtree(signature_dir)
    for receipt in app_path.rglob("SC_Info"):
        if receipt.is_dir():
            shutil.rmtree(receipt)


def sign_app(app_path: Path, identity: str, entitlements: Path) -> None:
    remove_old_signatures(app_path)
    dylibs = sorted(app_path.rglob("*.dylib"), key=lambda path: len(path.parts), reverse=True)
    frameworks = sorted(app_path.rglob("*.framework"), key=lambda path: len(path.parts), reverse=True)
    for code_path in [*dylibs, *frameworks]:
        run(["codesign", "--force", "--sign", identity, "--timestamp=none", code_path])
    run(
        [
            "codesign",
            "--force",
            "--sign",
            identity,
            "--timestamp=none",
            "--entitlements",
            entitlements,
            app_path,
        ]
    )
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app_path])


def package_ipa(payload_dir: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", payload_dir, destination])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_clone(
    ipa: Path,
    output_dir: Path,
    number: int,
    bundle_prefix: str,
    team_id: str,
    identity: str,
    install: bool,
    device: str,
    keep_work_dir: bool,
) -> Path:
    bundle_id = f"{bundle_prefix}{number}"
    temp_dir = Path(tempfile.mkdtemp(prefix=f"goofish-clone-{number}."))
    try:
        extracted = temp_dir / "extracted"
        extracted.mkdir()
        log(f"解包 {ipa.name}，准备闲鱼 {number}（{bundle_id}）")
        run(["ditto", "-x", "-k", ipa, extracted])
        apps = list((extracted / "Payload").glob("*.app"))
        if len(apps) != 1:
            raise CloneError(f"IPA 中应有且仅有一个主 App，实际找到 {len(apps)} 个。")
        app_path = apps[0]
        executable = customize_app(app_path, bundle_id, number)
        profile_path, profile = create_profile(bundle_id, team_id, temp_dir)
        shutil.copy2(profile_path, app_path / "embedded.mobileprovision")
        compile_and_inject_shim(app_path, executable, temp_dir)
        entitlements = temp_dir / "entitlements.plist"
        make_entitlements(bundle_id, team_id, entitlements)
        sign_app(app_path, identity, entitlements)

        output = output_dir / f"闲鱼-{number}-个人7天签名.ipa"
        package_ipa(extracted / "Payload", output)
        expiration = profile.get("ExpirationDate")
        log(f"已生成：{output}")
        log(f"SHA-256：{sha256(output)}")
        if expiration:
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            log(f"描述文件到期：{expiration.astimezone().isoformat(timespec='minutes')}")
        if install:
            log(f"安装到设备 {device}")
            run(["xcrun", "devicectl", "device", "install", "app", "--device", device, app_path])
            log(f"闲鱼 {number} 安装完成")
        return output
    finally:
        if keep_work_dir:
            log(f"保留工作目录：{temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把闲鱼 IPA 生成为多个独立安装的个人签名副本，桌面名均为“闲鱼”，图标右上角显示编号。"
    )
    parser.add_argument("numbers", nargs="+", type=int, help="副本编号，例如：2 3 4")
    parser.add_argument("--ipa", type=Path, default=DEFAULT_IPA, help=f"原始 IPA（默认：{DEFAULT_IPA}）")
    parser.add_argument("--output-dir", type=Path, help="输出目录（默认：IPA 同目录下的 clones）")
    parser.add_argument("--team-id", default=DEFAULT_TEAM_ID, help="Personal Team ID（也可由 GOOFISH_TEAM_ID 提供）")
    parser.add_argument(
        "--bundle-prefix",
        default=DEFAULT_CLONE_BUNDLE_PREFIX,
        help=f"副本包名前缀（默认：{DEFAULT_CLONE_BUNDLE_PREFIX}；末尾自动追加编号）",
    )
    parser.add_argument(
        "--identity",
        default=DEFAULT_IDENTITY_HINT,
        help="签名证书的邮箱、名称或 SHA-1 片段（也可由 GOOFISH_SIGNING_IDENTITY 提供）",
    )
    parser.add_argument("--install", action="store_true", help="生成后立即安装到设备")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="安装目标 UDID（也可由 GOOFISH_DEVICE_UDID 提供）")
    parser.add_argument("--keep-work-dir", action="store_true", help="保留临时工作目录，便于排查")
    args = parser.parse_args()
    if any(number < 2 for number in args.numbers):
        parser.error("副本编号必须从 2 开始。")
    if len(set(args.numbers)) != len(args.numbers):
        parser.error("副本编号不能重复。")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", args.bundle_prefix):
        parser.error("--bundle-prefix 不是有效的反向域名包名前缀。")
    return args


def main() -> int:
    args = parse_args()
    ipa = args.ipa.expanduser().resolve()
    if not ipa.is_file():
        raise CloneError(f"IPA 不存在：{ipa}")
    output_dir = (args.output_dir or (ipa.parent / "clones")).expanduser().resolve()
    if not args.team_id:
        raise CloneError("缺少 Personal Team ID；请配置 GOOFISH_TEAM_ID 或传 --team-id。")
    if not args.identity:
        raise CloneError("缺少签名证书；请配置 GOOFISH_SIGNING_IDENTITY 或传 --identity。")
    if args.install and not args.device:
        raise CloneError("使用 --install 时必须配置 GOOFISH_DEVICE_UDID 或传 --device。")
    for tool in ("codesign", "ditto", "plutil", "security", "xcodebuild", "xcrun"):
        require_tool(tool)
    identity = resolve_identity(args.identity)
    outputs = []
    for number in args.numbers:
        outputs.append(
            build_clone(
                ipa,
                output_dir,
                number,
                args.bundle_prefix,
                args.team_id,
                identity,
                args.install,
                args.device,
                args.keep_work_dir,
            )
        )
    log(f"完成，共生成 {len(outputs)} 个副本。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CloneError as exc:
        print(f"[闲鱼多开] 错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
