#!/usr/bin/env python3
"""Minimal Xianyu monitor using a real iPhone through Appium/XCUITest."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_SERVER = "http://127.0.0.1:4723"
PRICE_RE = re.compile(r"(?:[¥￥]\s*)?(\d{1,7}(?:\.\d{1,2})?)\s*元?")
IGNORED_TEXT = {
    "关闭",
    "发布",
    "我的",
    "搜索",
    "消息",
    "筛选",
    "综合",
    "返回",
    "闲鱼",
    "首页",
    "新发布",
}


@dataclass(frozen=True)
class Device:
    udid: str
    os_version: str


@dataclass(frozen=True)
class InstalledApp:
    name: str
    bundle_id: str


@dataclass(frozen=True)
class Item:
    title: str
    price: str
    raw_text: list[str]

    @property
    def key(self) -> str:
        return f"{self.title.strip().lower()}|{self.price.strip().lower()}"


def _run_json(command: list[str]) -> dict:
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return json.loads(completed.stdout)


def discover_connected_iphones() -> list[Device]:
    payload = _run_json(
        [
            "xcrun",
            "devicectl",
            "list",
            "devices",
            "--filter",
            "State = 'connected' AND Name CONTAINS 'iPhone'",
            "--json-output",
            "-",
        ]
    )
    devices: list[Device] = []
    for raw in payload.get("result", {}).get("devices", []):
        properties = raw.get("properties", {})
        os_version = (
            raw.get("operatingSystemVersion")
            or properties.get("device", {}).get("osVersionNumber")
            or raw.get("deviceProperties", {}).get("osVersionNumber")
            or "unknown"
        )
        identifier = str(
            properties.get("hardware", {}).get("udid")
            or raw.get("hardwareProperties", {}).get("udid")
            or raw.get("identifier")
            or ""
        ).strip()
        if identifier:
            devices.append(Device(identifier, str(os_version)))
    return devices


def discover_xianyu_apps(udid: str) -> list[InstalledApp]:
    payload = _run_json(
        [
            "xcrun",
            "devicectl",
            "device",
            "info",
            "apps",
            "--device",
            udid,
            "--include-default-apps",
            "--json-output",
            "-",
        ]
    )
    found: dict[str, InstalledApp] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            bundle_id = value.get("bundleIdentifier") or value.get("bundleID")
            name = value.get("name") or value.get("localizedName") or ""
            if bundle_id and (
                "fleamarket" in str(bundle_id).lower()
                or "闲鱼" in str(name)
                or "xianyu" in str(name).lower()
            ):
                found[str(bundle_id)] = InstalledApp(str(name), str(bundle_id))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return sorted(found.values(), key=lambda app: (app.name, app.bundle_id))


def installed_iphoneos_sdk_version() -> str:
    completed = subprocess.run(
        ["xcrun", "--sdk", "iphoneos", "--show-sdk-version"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return completed.stdout.strip()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _node_texts(node: ET.Element) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for child in node.iter():
        for attribute in ("label", "value", "name"):
            text = _clean_text(child.attrib.get(attribute))
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
    return texts


def _price_from_text(text: str) -> str | None:
    if "¥" not in text and "￥" not in text and not text.endswith("元"):
        return None
    match = PRICE_RE.search(text.replace(",", ""))
    return f"¥{match.group(1)}" if match else None


def _candidate_from_texts(texts: Iterable[str]) -> Item | None:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        text = _clean_text(raw)
        if text and text not in seen:
            seen.add(text)
            unique.append(text)
    price = next((_price_from_text(text) for text in unique if _price_from_text(text)), None)
    if not price:
        return None
    title_candidates = [
        text
        for text in unique
        if text not in IGNORED_TEXT
        and not _price_from_text(text)
        and not text.isdigit()
        and len(text) >= 4
    ]
    if not title_candidates:
        return None
    title = max(title_candidates, key=lambda value: (len(value), value))
    return Item(title=title, price=price, raw_text=unique)


def extract_items_from_source(source: str) -> list[Item]:
    root = ET.fromstring(source)
    container_types = {
        "XCUIElementTypeOther",
        "XCUIElementTypeCell",
        "XCUIElementTypeButton",
    }
    candidates: list[Item] = []
    for node in root.iter():
        if node.attrib.get("type") not in container_types:
            continue
        candidate = _candidate_from_texts(_node_texts(node))
        if candidate:
            candidates.append(candidate)
    deduped: dict[str, Item] = {}
    for item in sorted(candidates, key=lambda value: len(value.raw_text)):
        deduped.setdefault(item.key, item)
    return list(deduped.values())


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return {str(item) for item in value if isinstance(item, str)}
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_artifacts(driver: object, source: str, label: str) -> None:
    output = ROOT / "artifacts"
    output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = output / f"{timestamp}-{label}"
    prefix.with_suffix(".xml").write_text(source, encoding="utf-8")
    driver.save_screenshot(str(prefix.with_suffix(".png")))


def _first_displayed(elements: Iterable[object]) -> object | None:
    for element in elements:
        try:
            if element.is_displayed():
                return element
        except Exception:
            continue
    return None


def enter_search(driver: object, keyword: str) -> None:
    from appium.webdriver.common.appiumby import AppiumBy

    predicate = (
        "type == 'XCUIElementTypeSearchField' OR "
        "type == 'XCUIElementTypeTextField'"
    )
    field = _first_displayed(driver.find_elements(AppiumBy.IOS_PREDICATE, predicate))
    if field is None:
        trigger = _first_displayed(
            driver.find_elements(
                AppiumBy.IOS_PREDICATE,
                "name CONTAINS[c] '搜索' OR label CONTAINS[c] '搜索'",
            )
        )
        if trigger is None:
            save_artifacts(driver, driver.page_source, "search-input-missing")
            raise RuntimeError("未在当前页面找到搜索入口。")
        trigger.click()
        time.sleep(2)
        field = _first_displayed(
            driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)
        )
    if field is None:
        save_artifacts(driver, driver.page_source, "search-input-missing")
        raise RuntimeError("已点击搜索入口，但没有找到输入框。")
    field.click()
    try:
        field.clear()
    except Exception:
        pass
    field.send_keys(keyword)
    search_button = _first_displayed(
        driver.find_elements(
            AppiumBy.IOS_PREDICATE,
            "type == 'XCUIElementTypeButton' AND "
            "(name == '搜索' OR label == '搜索' OR name == 'Search')",
        )
    )
    if search_button:
        search_button.click()
    else:
        driver.execute_script(
            "mobile: performIoHidEvent",
            {"page": 7, "usage": 40, "durationSeconds": 0.05},
        )
    time.sleep(4)


def create_driver(
    server_url: str,
    udid: str,
    bundle_id: str,
    platform_version: str,
    team_id: str | None,
):
    from appium import webdriver
    from appium.options.ios import XCUITestOptions

    capabilities = {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:platformVersion": platform_version,
        "appium:udid": udid,
        "appium:bundleId": bundle_id,
        "appium:noReset": True,
        "appium:shouldTerminateApp": False,
        "appium:useNewWDA": False,
        "appium:newCommandTimeout": 300,
        "appium:wdaLaunchTimeout": 120000,
        "appium:wdaStartupRetries": 2,
        "appium:settings[snapshotMaxDepth]": 62,
        "appium:settings[includeNativeAccessibilityElementInPageSource]": True,
    }
    if team_id:
        capabilities.update(
            {
                "appium:xcodeOrgId": team_id,
                "appium:xcodeSigningId": "Apple Development",
                "appium:updatedWDABundleId": f"io.appium.wda.{team_id.lower()}",
                "appium:allowProvisioningDeviceRegistration": True,
            }
        )
    options = XCUITestOptions().load_capabilities(capabilities)
    return webdriver.Remote(server_url, options=options)


def monitor_once(driver: object, keyword: str, scrolls: int, probe_only: bool) -> list[Item]:
    if not probe_only:
        enter_search(driver, keyword)
    found: dict[str, Item] = {}
    for page in range(scrolls + 1):
        source = driver.page_source
        save_artifacts(driver, source, f"page-{page}")
        for item in extract_items_from_source(source):
            found.setdefault(item.key, item)
        if page < scrolls:
            driver.execute_script("mobile: swipe", {"direction": "up", "velocity": 900})
            time.sleep(2)
    return list(found.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--server-url", default=DEFAULT_SERVER)
    parser.add_argument("--udid", default=os.getenv("IOS_UDID"))
    parser.add_argument("--bundle-id", default=os.getenv("IOS_BUNDLE_ID"))
    parser.add_argument("--team-id", default=os.getenv("IOS_TEAM_ID"))
    parser.add_argument(
        "--platform-version",
        default=os.getenv("IOS_PLATFORM_VERSION"),
        help="WDA deployment target; defaults to the installed iPhoneOS SDK.",
    )
    parser.add_argument("--scrolls", type=int, default=2)
    parser.add_argument(
        "--interval", type=int, default=0, help="Polling interval; 0 runs once."
    )
    parser.add_argument("--probe-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    devices = discover_connected_iphones()
    if args.udid:
        device = Device(args.udid, "explicit")
    elif len(devices) == 1:
        device = devices[0]
    else:
        print("未发现唯一的已连接 iPhone，请通过 --udid 指定。", file=sys.stderr)
        return 2

    apps = discover_xianyu_apps(device.udid)
    bundle_id = args.bundle_id or (apps[0].bundle_id if apps else None)
    if not bundle_id:
        print("未在目标 iPhone 上发现闲鱼，请通过 --bundle-id 指定。", file=sys.stderr)
        return 2
    platform_version = args.platform_version or installed_iphoneos_sdk_version()
    driver = None
    try:
        driver = create_driver(
            args.server_url,
            device.udid,
            bundle_id,
            platform_version,
            args.team_id,
        )
        driver.activate_app(bundle_id)
        time.sleep(2)
        seen_path = ROOT / "state" / "seen.json"
        seen = load_seen(seen_path)
        while True:
            items = monitor_once(driver, args.keyword, max(0, args.scrolls), args.probe_only)
            for item in items:
                if item.key not in seen:
                    print(json.dumps(asdict(item), ensure_ascii=False))
                    seen.add(item.key)
            save_seen(seen_path, seen)
            if args.interval <= 0:
                break
            time.sleep(args.interval)
        return 0
    except Exception as exc:
        print(f"Appium 验证失败：{exc}", file=sys.stderr)
        return 3
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
