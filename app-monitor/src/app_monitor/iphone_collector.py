"""Two-stage iPhone collection: read visible cards, share only price-matching items."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import httpx

from .appium_server import ensure_appium_server
from .collector import CollectResult, guess_region
from .config import Settings
from .matching import normalize_search_text

logger = logging.getLogger(__name__)
_PRICE = re.compile(r"^[¥￥]\s*([1-9]\d{1,6}(?:\.\d{1,2})?)\s*(?:元)?$")
_PRICE_OTHER = re.compile(r"^([1-9]\d{1,6}(?:\.\d{1,2})?)\s*元$")
_URL = re.compile(r"https?://[^\s<>\"']+")
_ITEM_ID = re.compile(r"(?:[?&](?:id|itemId|item_id)=)(\d{8,})")
_IGNORED = {"新发", "综合", "价格", "筛选", "区域", "刚刚发布", "包邮", "验货宝", "想要", "搜索"}
_STORAGE_LABEL = re.compile(r"^(?:\d+GB|\d+TB)$")


@dataclass(frozen=True)
class PhoneCard:
    title: str
    price: float
    region: str
    x: int
    y: int


def _text(node: ET.Element) -> str:
    return str(node.get("value") or node.get("label") or node.get("name") or "").strip()


def _price(node: ET.Element) -> re.Match[str] | None:
    value = _text(node)
    if node.tag == "XCUIElementTypeStaticText":
        return _PRICE.fullmatch(value)
    if node.tag == "XCUIElementTypeOther":
        return _PRICE_OTHER.fullmatch(value)
    return None


def parse_cards(source: str, region_hint: str = "") -> list[PhoneCard]:
    """Bind each price to the smallest enclosing card, rejecting mixed-card ancestors."""
    root = ET.fromstring(source)
    parents = {child: parent for parent in root.iter() for child in parent}
    found: list[PhoneCard] = []
    used_price_nodes: set[int] = set()
    for price_node in root.iter():
        price_match = _price(price_node)
        if not price_match or price_node.get("visible", "true") != "true":
            continue
        if id(price_node) in used_price_nodes:
            continue
        price_x, price_y = int(price_node.get("x") or 0), int(price_node.get("y") or 0)
        current = parents.get(price_node)
        while current is not None:
            leaves = [node for node in current.iter("XCUIElementTypeStaticText")]
            prices = [node for node in current.iter() if _price(node)]
            if len(prices) > 1:
                break  # parent spans more than one product; never pair across cards
            titles = [
                node for node in leaves if node is not price_node and len(_text(node)) >= 6
                and _text(node) not in _IGNORED and not _PRICE.fullmatch(_text(node))
                and not _text(node).startswith(("¥", "￥"))
                and abs(int(node.get("x") or 0) - price_x) < 190
                and abs(int(node.get("y") or 0) - price_y) < 260
            ]
            if titles:
                title_node = max(titles, key=lambda node: len(_text(node)))
                title = _text(title_node)
                x = int(title_node.get("x") or 0)
                y = int(title_node.get("y") or 0)
                # The region can be a sibling of the title/price group. Walk up
                # only while this price is still the sole card price, and keep
                # texts near the card rather than the page's city search header.
                context = current
                while context.tag != "XCUIElementTypeCell":
                    ancestor = parents.get(context)
                    if ancestor is None:
                        break
                    if sum(bool(_price(node)) for node in ancestor.iter()) != 1:
                        break
                    context = ancestor
                region_texts = [
                    _text(node) for node in context.iter("XCUIElementTypeStaticText")
                    if abs(int(node.get("x") or 0) - price_x) < 190
                    and price_y - 30 <= int(node.get("y") or 0) <= price_y + 240
                ]
                region = guess_region(region_texts, region_hint)
                found.append(PhoneCard(title, float(price_match.group(1)), region, x, y))
                used_price_nodes.add(id(price_node))
                break
            if current.tag == "XCUIElementTypeCell":
                break
            current = parents.get(current)
    return list(dict.fromkeys(found))


def item_id_from_share(share_text: str, page_html: str) -> str:
    """Only accept an item ID attached to a Goofish item URL, never a tracking ID."""
    for text in (share_text, page_html):
        decoded = unquote(html.unescape(text).replace("\\/", "/"))
        for match in re.finditer(r"https?://(?:www\.|h5\.m\.)?goofish\.com/item[^\s\"'<>]{0,1200}", decoded):
            item_id = _ITEM_ID.search(match.group(0))
            if item_id:
                return item_id.group(1)
    return ""


def detail_matches(source: str, card: PhoneCard) -> bool:
    detail = ET.fromstring(source)
    expected = normalize_search_text(card.title)
    descriptions = [normalize_search_text(_text(node)) for node in detail.iter("XCUIElementTypeTextView")]
    if len(expected) < 12 or not any(value.startswith(expected) for value in descriptions):
        return False
    return any(
        _text(node).replace("¥", "").replace("￥", "").replace("元", "") == f"{card.price:g}"
        and 100 <= int(node.get("y") or 0) < 300
        for node in detail.iter()
    )


def checked_storages(source: str) -> set[str]:
    """The picker exposes its checkmark as a sibling image, not a selected flag."""
    root = ET.fromstring(source)
    picker_open = any(_text(node) == "确定" and node.get("visible") == "true"
                      for node in root.iter("XCUIElementTypeButton"))
    selected = set()
    for node in root.iter():
        options = [child for child in node if child.tag == "XCUIElementTypeStaticText"
                   and _STORAGE_LABEL.fullmatch(_text(child)) and child.get("visible") != "false"]
        if len(options) != 1:
            continue
        text = options[0]
        for child in node:
            if child.tag != "XCUIElementTypeImage" or child.get("visible") == "false":
                continue
            named_check = _text(child) == "search_checkbox_sel"
            # Some iOS versions omit image asset names. Only accept the small
            # right-side image inside a single-option picker row as a checkmark.
            unnamed_check = (picker_open and not _text(child)
                             and 16 <= int(child.get("width") or 0) <= 32
                             and 16 <= int(child.get("height") or 0) <= 32
                             and int(child.get("x") or 0) >= int(text.get("x") or 0) + int(text.get("width") or 0)
                             and abs(int(child.get("y") or 0) - int(text.get("y") or 0)) <= 16
                             and int(child.get("x") or 0) + int(child.get("width") or 0)
                             <= int(node.get("x") or 0) + int(node.get("width") or 0))
            if named_check or unnamed_check:
                selected.add(_text(text))
    return selected


def result_bar_label(source: str, label: str) -> bool:
    """Match the active city/capacity chip beside the result sort row."""
    if label == "存储容量" or _STORAGE_LABEL.fullmatch(label):
        return active_storage_chip(source) == label
    return result_region_chip(source) == label


def _storage_chip_node(source: str, visible_only: bool = True) -> ET.Element | None:
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    width = int(app.get("width") or 0)
    nodes = list(root.iter("XCUIElementTypeStaticText"))
    sort_y = next((int(node.get("y") or 0) for node in nodes
                   if _text(node) == "新发" and node.get("visible") == "true"), None)
    if sort_y is None:
        return None
    return next((node for node in nodes
                 if (not visible_only or node.get("visible") == "true")
                 and (not visible_only or not width or
                      0 <= int(node.get("x") or 0) and
                      int(node.get("x") or 0) + int(node.get("width") or 0) <= width)
                 and sort_y + 20 <= int(node.get("y") or 0) <= sort_y + 70
                 and (_text(node) == "存储容量" or _STORAGE_LABEL.fullmatch(_text(node)))), None)


def active_storage_chip(source: str) -> str | None:
    node = _storage_chip_node(source)
    return _text(node) if node is not None else None


def has_result_sort_bar(source: str) -> bool:
    """Retain page identity even when scrolling makes the sort row invisible."""
    nodes = list(ET.fromstring(source).iter("XCUIElementTypeStaticText"))
    return any(abs(int(new.get("y") or 0) - int(filters.get("y") or 0)) <= 5
               for new in nodes if _text(new) == "新发"
               for filters in nodes if _text(filters) == "筛选")


def is_search_results(source: str) -> bool:
    """The sort row may be collapsed after returning to a scrolled list."""
    if not has_result_sort_bar(source):
        return False
    root = ET.fromstring(source)
    visible = [node for node in root.iter() if node.get("visible") == "true"]
    if any((node.get("name") or node.get("label")) in {"分享按钮", "返回按钮", "确定"}
           for node in visible):
        return False
    return any(node.tag == "XCUIElementTypeButton"
               and (node.get("name") or node.get("label")) in {"search more", "图片搜索"}
               and int(node.get("y") or 0) < 180 for node in visible)


def search_entry_points(source: str) -> list[dict[str, Any]]:
    """Choose text inside the search field, never arbitrary top/history text."""
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    width, height = int(app.get("width") or 0), int(app.get("height") or 0)
    if not width or not height:
        return []
    visible = [node for node in root.iter() if node.get("visible") == "true"]
    points: list[dict[str, Any]] = []

    def point(node: ET.Element, method: str) -> dict[str, Any]:
        rect = {key: int(node.get(key) or 0) for key in ("x", "y", "width", "height")}
        return {"target": str(node.get("name") or node.get("label") or "搜索栏"),
                "method": method, "x": rect["x"] + rect["width"] // 2,
                "y": rect["y"] + rect["height"] // 2}

    if home_tabs(source):
        bars = [node for node in visible if node.tag == "XCUIElementTypeButton"
                and int(node.get("width") or 0) >= width * 0.55
                and 30 <= int(node.get("y") or 0) < height * 0.22
                and ("SearchField" in (node.get("traits") or "")
                     or any(child.tag == "XCUIElementTypeImage" and child.get("name") == "搜索"
                            and abs(int(child.get("y") or 0) - int(node.get("y") or 0)) < 30
                            for child in visible))]
        if not bars:
            return []
        bar = min(bars, key=lambda node: int(node.get("y") or 0))
        name = bar.get("name") or bar.get("label")
        texts = [node for node in visible if node.tag == "XCUIElementTypeStaticText"
                 and len(_text(node)) >= 2 and _text(node) not in {"搜索", "Search", "拍照", "扫一扫"}
                 and int(bar.get("x") or 0) <= int(node.get("x") or 0)
                 and int(node.get("x") or 0) + int(node.get("width") or 0)
                 <= int(bar.get("x") or 0) + int(bar.get("width") or 0)
                 and abs(int(node.get("y") or 0) - int(bar.get("y") or 0)) < 25]
        if texts:
            points.append(point(min(texts, key=lambda node: (
                (node.get("name") or node.get("label")) != name,
                abs(int(node.get("y") or 0) - int(bar.get("y") or 0)), int(node.get("x") or 0))),
                                "轻点搜索栏关键词文字中心"))
        # Only retry inside the observed search bar, away from scan/camera icons.
        points.append({"target": str(name or "搜索栏"), "method": "轻点搜索栏文字区域",
                       "x": int(bar.get("x") or 0) + int(bar.get("width") or 0) // 4,
                       "y": int(bar.get("y") or 0) + int(bar.get("height") or 0) // 2})
    elif has_result_sort_bar(source):
        images = [node for node in visible if node.get("name") == "图片搜索"]
        if not images:
            return []
        image = min(images, key=lambda node: int(node.get("y") or 0))
        texts = [node for node in visible if node.tag == "XCUIElementTypeStaticText"
                 and int(node.get("x") or 0) >= 35
                 and int(node.get("x") or 0) + int(node.get("width") or 0) < int(image.get("x") or 0)
                 and abs(int(node.get("y") or 0) - int(image.get("y") or 0)) < 20
                 and len(_text(node)) >= 3]
        if texts:
            points.append(point(min(texts, key=lambda node: int(node.get("x") or 0)), "轻点结果页搜索词"))
    return points


def home_tabs(source: str) -> list[ET.Element]:
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    height = int(app.get("height") or 0)
    if not height:
        return []
    tabs = sorted((node for node in root.iter("XCUIElementTypeButton")
                   if node.get("visible") == "true" and _text(node)
                   and int(node.get("y") or 0) >= height * 0.85
                   and 25 <= int(node.get("width") or 0) <= 130),
                  key=lambda node: int(node.get("x") or 0))
    return tabs if len(tabs) >= 4 and (tabs[0].get("label") or tabs[0].get("name")) == "首页" else []


def home_search_city(source: str, city: str) -> bool:
    """City text must sit to the left of the wide home search field."""
    root = ET.fromstring(source)
    cities = [node for node in root.iter("XCUIElementTypeStaticText")
              if _text(node) == city and node.get("visible") == "true"
              and int(node.get("x") or 0) < 70 and 50 <= int(node.get("y") or 0) < 110]
    return any(button.get("visible") == "true" and int(button.get("width") or 0) >= 200
               and int(button.get("x") or 0) >= int(node.get("x") or 0) + int(node.get("width") or 0)
               and abs(int(button.get("y") or 0) - int(node.get("y") or 0)) <= 20
               for node in cities for button in root.iter("XCUIElementTypeButton"))


def result_region_chip(source: str) -> str | None:
    nodes = [node for node in ET.fromstring(source).iter("XCUIElementTypeStaticText")
             if node.get("visible") == "true"]
    new = next((node for node in nodes if _text(node) == "新发"), None)
    filtering = next((node for node in nodes if _text(node) == "筛选"), None)
    if new is None or filtering is None:
        return None
    return next((_text(node) for node in nodes
                 if int(new.get("x") or 0) + int(new.get("width") or 0) < int(node.get("x") or 0)
                 < int(filtering.get("x") or 0)
                 and abs(int(node.get("y") or 0) - int(new.get("y") or 0)) <= 5), None)


def _brief_error(exc: Exception) -> str:
    return (str(exc).splitlines()[0] or type(exc).__name__)[:400]


def _pasteboard(udid: str) -> str:
    process = subprocess.run(
        ["xcrun", "devicectl", "device", "pasteboard", "paste", "--device", udid],
        capture_output=True, text=True, timeout=12, check=True,
    )
    return process.stdout.strip()


def _prepare_pasteboard(udid: str) -> str:
    # A fresh marker verifies this copy action even if the same product was
    # copied on a previous run. Never accept a stale share URL as a new copy.
    marker = f"app-monitor-copy-{uuid.uuid4().hex}"
    subprocess.run(
        ["xcrun", "devicectl", "device", "pasteboard", "copy", "--device", udid],
        input=marker, capture_output=True, text=True, timeout=12, check=True,
    )
    return marker


class IPhoneCollector:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def collect(
        self, udid: str, os_version: str, keyword: str, region_hint: str,
        region_steps: list[str], storages: list[str], scroll_pages: int,
        is_candidate: Callable[[dict[str, Any], bool, str], bool],
    ) -> CollectResult:
        return await asyncio.to_thread(
            self._collect, udid, os_version, keyword, region_hint, region_steps,
            storages, scroll_pages, is_candidate,
        )

    def _collect(
        self, udid: str, os_version: str, keyword: str, region_hint: str,
        region_steps: list[str], storages: list[str], scroll_pages: int,
        is_candidate: Callable[[dict[str, Any], bool, str], bool],
    ) -> CollectResult:
        from appium import webdriver
        from appium.options.ios import XCUITestOptions

        if not self.settings.ios_wda_bundle_id:
            raise RuntimeError("未配置 IOS_WDA_BUNDLE_ID；请填写已签名的 WebDriverAgent bundle ID")
        ensure_appium_server(self.settings)
        capabilities = {
            "platformName": "iOS", "appium:automationName": "XCUITest",
            "appium:udid": udid, "appium:bundleId": self.settings.bundle_id,
            "appium:noReset": True, "appium:shouldTerminateApp": False,
            "appium:usePrebuiltWDA": True,
            "appium:derivedDataPath": str(self.settings.ios_wda_derived_data_path),
            "appium:updatedWDABundleId": self.settings.ios_wda_bundle_id,
            "appium:wdaLocalPort": 8101,
            "appium:newCommandTimeout": 300,
        }
        if os_version:
            capabilities["appium:platformVersion"] = os_version
        result = CollectResult(items=[])
        setup_actions: list[dict[str, Any]] = []
        driver = None
        try:
            try:
                driver = webdriver.Remote(
                    self.settings.appium_server_url,
                    options=XCUITestOptions().load_capabilities(capabilities),
                )
            except Exception as exc:
                raise RuntimeError(f"iPhone Appium 连接失败（检查服务、WDA 和 Inspector 会话）：{exc}") from exc
            try:
                inherited_region = self._search(driver, keyword, region_hint, setup_actions)
            except Exception as exc:
                error = _brief_error(exc)
                result.passes.append({"label": "新发", "storage": None, "observed": 0,
                                      "error": error, "actions": [*setup_actions,
                                          {"action": "搜索", "target": keyword, "success": False, "error": error}]})
                return result
            if region_hint:
                try:
                    if inherited_region:
                        setup_actions.append({"action": "跳过地区筛选", "target": region_hint,
                                              "method": "首页第2Tab、搜索栏及结果区域均匹配", "success": True})
                    else:
                        clicked = self._apply_region(driver, region_hint, region_steps)
                        setup_actions.append({"action": "地区筛选", "target": region_hint,
                                              "clicked": clicked, "success": True})
                    result.region_applied = True
                except Exception as exc:
                    error = _brief_error(exc)
                    setup_actions.append({"action": "地区筛选", "target": region_hint,
                                          "success": False, "error": error})
                    result.region_error = error
                    logger.error("iPhone 地区筛选失败 city=%s error=%s", region_hint, exc)
                    result.passes.append({"label": "新发", "storage": None, "observed": 0,
                                          "error": error, "actions": setup_actions})
                    return result  # Never collect nationwide cards as if the city filter succeeded.
            for storage in storages:
                actions = setup_actions.copy()
                setup_actions = []
                seen: set[tuple[str, float, str]] = set()
                card_samples: list[dict[str, Any]] = []
                pass_items: list[dict[str, Any]] = []
                candidates = 0
                error = None
                try:
                    clicked = self._apply_storage(driver, storage)
                    actions.append({"action": "容量筛选", "target": storage,
                                    "clicked": clicked, "success": True})
                    self._sort_new(driver)
                    actions.append({"action": "点击排序", "target": "新发", "success": True})
                    for page in range(max(0, scroll_pages) + 1):
                        if page:
                            driver.execute_script("mobile: swipe", {"direction": "up", "velocity": 900})
                            time.sleep(1)
                            actions.append({"action": "上滑列表", "target": f"第 {page + 1} 屏", "success": True})
                        cards = parse_cards(driver.page_source, region_hint)
                        logger.info("iPhone 列表 udid=%s storage=%s page=%s cards=%s", udid[-6:], storage, page, len(cards))
                        for card in cards:
                            key = (card.title, card.price, card.region)
                            if key in seen:
                                continue
                            seen.add(key)
                            logger.info("iPhone 商品 storage=%s price=%s region=%s title=%s",
                                        storage, card.price, card.region or "未显示", card.title[:100])
                            if len(card_samples) < 40:
                                card_samples.append({"title": card.title, "price": card.price,
                                                     "display_region": card.region})
                            raw = {"title": card.title, "price": card.price, "display_region": card.region}
                            if not is_candidate(raw, result.region_applied, storage):
                                continue
                            candidates += 1
                            try:
                                item_id = self._share_item_id(driver, udid, card, actions)
                            except Exception as exc:
                                logger.warning("iPhone 候选分享失败 title=%s error=%s", card.title[:40], exc)
                                actions.append({"action": "复制候选链接", "target": card.title[:40],
                                                "success": False, "error": _brief_error(exc)})
                                if not is_search_results(driver.page_source):
                                    raise RuntimeError("分享失败后未回到搜索列表，停止本机采集") from exc
                                continue
                            if item_id:
                                item = {**raw, "item_id": item_id,
                                        "item_url": f"https://www.goofish.com/item?id={item_id}",
                                        "image_urls": [], "source_storage": storage, "source_pass": "新发"}
                                pass_items.append(item)
                                result.items.append(item)
                                actions.append({"action": "复制候选链接", "target": card.title[:40],
                                                "success": True})
                                logger.info("iPhone 候选关联 item_id=%s price=%s title=%s",
                                            item_id, card.price, card.title[:60])
                except Exception as exc:
                    error = _brief_error(exc)
                    actions.append({"action": "容量筛选或采集", "target": storage,
                                    "success": False, "error": error})
                    logger.error("iPhone 容量采集失败 storage=%s error=%s", storage, error)
                result.passes.append({
                    "label": "新发", "storage": storage, "filter_applied": error is None,
                    "region_applied": result.region_applied, "observed": len(pass_items),
                    "cards": len(seen), "candidates": candidates, "actions": actions,
                    "card_samples": card_samples, "cards_truncated": len(seen) > len(card_samples),
                    "items": pass_items[:25], "items_total": len(pass_items),
                    "items_truncated": len(pass_items) > 25, "error": error,
                })
                if error:
                    break  # The picker/list state is unknown; do not click the next capacity.
            result.filter_applied = bool(result.passes) and all(
                entry.get("filter_applied") for entry in result.passes)
            return result
        finally:
            if driver is not None:
                driver.quit()

    @staticmethod
    def _find(driver: Any, name: str, types: tuple[str, ...] = ()) -> Any | None:
        from appium.webdriver.common.appiumby import AppiumBy

        for element in driver.find_elements(AppiumBy.IOS_PREDICATE, f"name == '{name}' OR label == '{name}'"):
            if (not types or element.get_attribute("type") in types) and element.is_displayed():
                return element
        return None

    @staticmethod
    def _close_share_sheet(driver: Any) -> None:
        from appium.webdriver.common.appiumby import AppiumBy

        for _ in range(3):
            if not driver.find_elements(AppiumBy.IOS_PREDICATE, "name == '复制链接'"):
                return
            size = driver.get_window_size()
            driver.execute_script("mobile: tap", {"x": size["width"] // 2, "y": 200})
            time.sleep(0.5)
        if driver.find_elements(AppiumBy.IOS_PREDICATE, "name == '复制链接'"):
            raise RuntimeError("分享面板未关闭，停止后续点击")

    def _select_home_city(self, driver: Any, city: str, actions: list[dict[str, Any]]) -> bool:
        """Only take the shortcut when the actual second bottom tab matches."""
        returned = 0
        for _ in range(5):
            tabs = home_tabs(driver.page_source)
            if tabs:
                if returned:
                    actions.append({"action": "返回首页", "target": f"返回 {returned} 次", "success": True})
                # A selected native tab may expose value="1"; its city is
                # carried by label/name rather than the generic value field.
                second = str(tabs[1].get("label") or tabs[1].get("name") or "")
                if second != city:
                    logger.info("iPhone 首页第2Tab不匹配 configured=%s actual=%s", city, second)
                    actions.append({"action": "首页地区检查", "target": f"第2Tab={second}，目标={city}",
                                    "method": "使用结果页地区筛选", "success": True})
                    return False
                button = self._find(driver, city, ("XCUIElementTypeButton",))
                if button is None or button.rect["y"] < int(tabs[1].get("y") or 0) - 5:
                    return False
                button.click()
                actions.append({"action": "点击首页第2Tab", "target": city, "success": True})
                for _ in range(6):
                    if home_search_city(driver.page_source, city):
                        logger.info("iPhone 首页地区确认 tab=%s search_bar=%s", city, city)
                        actions.append({"action": "搜索栏地区确认", "target": city, "success": True})
                        return True
                    time.sleep(0.3)
                actions.append({"action": "首页地区检查", "target": city,
                                "method": "搜索栏未确认地区，使用结果页地区筛选", "success": True})
                return False
            back = self._find(driver, "返回", ("XCUIElementTypeButton",)) or self._find(driver, "返回按钮")
            if back is None:
                break
            back.click()
            returned += 1
            time.sleep(0.3)
        logger.info("iPhone 首页地区入口不可用 city=%s，使用结果页地区筛选", city)
        actions.append({"action": "首页地区检查", "target": city,
                        "method": "首页入口不可用，使用结果页地区筛选", "success": True})
        return False

    def _ensure_foreground(self, driver: Any, actions: list[dict[str, Any]]) -> None:
        for attempt in range(3):
            info = driver.execute_script("mobile: activeAppInfo")
            if info.get("bundleId") == self.settings.bundle_id:
                return
            if attempt == 0:
                driver.activate_app(self.settings.bundle_id)
                actions.append({"action": "恢复闲鱼前台", "success": True,
                                "target": str(info.get("bundleId") or "未知前台应用")})
            time.sleep(0.4)
        raise RuntimeError("闲鱼未恢复到前台，停止操作以免点击系统界面")

    @staticmethod
    def _search_input(driver: Any) -> Any | None:
        from appium.webdriver.common.appiumby import AppiumBy

        fields = driver.find_elements(AppiumBy.IOS_PREDICATE,
                                      "type IN {'XCUIElementTypeTextField', 'XCUIElementTypeSearchField'}")
        return next((field for field in fields if field.is_displayed()
                     and field.rect["y"] < 200 and field.rect["width"] > 100), None)

    def _open_search_input(self, driver: Any, actions: list[dict[str, Any]]) -> Any:
        for attempt in range(2):
            self._ensure_foreground(driver, actions)
            field = self._search_input(driver)
            if field is not None:
                return field
            source = driver.page_source
            if any(node.get("visible") == "true" for node in ET.fromstring(source).iter("XCUIElementTypeAlert")):
                raise RuntimeError("系统弹窗阻挡搜索，停止点击；请处理弹窗后重试")
            points = search_entry_points(source)
            if not points:
                raise RuntimeError("当前页面没有可确认的搜索入口，已停止以免点击历史记录或商品")
            target = points[min(attempt, len(points) - 1)]
            logger.info("iPhone 点击搜索入口 attempt=%s target=%s method=%s point=(%s,%s)",
                        attempt + 1, target["target"], target["method"], target["x"], target["y"])
            driver.execute_script("mobile: tap", {"x": target["x"], "y": target["y"]})
            actions.append({"action": "点击搜索入口", "target": target["target"], "success": True,
                            "method": f"{target['method']} ({target['x']}, {target['y']})，第{attempt + 1}次"})
            for _ in range(8):
                field = self._search_input(driver)
                if field is not None:
                    actions.append({"action": "搜索输入框确认", "success": True,
                                    "method": str(field.get_attribute("type"))})
                    return field
                time.sleep(0.3)
            logger.warning("iPhone 点击搜索入口后尚未出现输入框 attempt=%s", attempt + 1)
        raise RuntimeError("点击搜索入口并重新定位重试后仍未出现输入框")

    def _search(
        self, driver: Any, keyword: str, region_hint: str = "",
        actions: list[dict[str, Any]] | None = None,
    ) -> bool:
        actions = actions if actions is not None else []
        driver.activate_app(self.settings.bundle_id)
        self._ensure_foreground(driver, actions)
        self._close_share_sheet(driver)
        for _ in range(3):
            if not self._find(driver, "分享按钮"):
                break
            back = self._find(driver, "返回按钮")
            if back:
                back.click()
            else:
                driver.back()
        home_confirmed = self._select_home_city(driver, region_hint, actions) if region_hint else False
        field = self._open_search_input(driver, actions)
        field.click()
        field.clear()
        field.send_keys(keyword)
        if str(field.get_attribute("value") or "").strip() != keyword:
            raise RuntimeError("搜索词输入后回读不一致，已停止提交")
        button = None
        for _ in range(8):
            button = self._find(driver, "Search", ("XCUIElementTypeButton",)) or self._find(driver, "搜索", ("XCUIElementTypeButton",))
            if button:
                break
            time.sleep(0.3)
        if button is None:
            raise RuntimeError("找不到键盘搜索按钮")
        button.click()
        actions.append({"action": "搜索", "target": keyword, "success": True})
        for _ in range(10):
            if self._find(driver, "新发", ("XCUIElementTypeStaticText",)):
                inherited = home_confirmed and result_bar_label(driver.page_source, region_hint)
                if home_confirmed:
                    logger.info("iPhone 地区继承确认 city=%s confirmed=%s", region_hint, inherited)
                    actions.append({"action": "结果区域确认", "target": region_hint,
                                    "method": "与首页和搜索栏一致" if inherited else "未继承，使用结果页地区筛选",
                                    "success": True})
                return inherited
            time.sleep(0.5)
        raise RuntimeError("提交搜索后没有出现结果排序栏")

    def _sort_new(self, driver: Any) -> None:
        button = self._find(driver, "新发", ("XCUIElementTypeStaticText",))
        if button is None:
            raise RuntimeError("找不到「新发」排序按钮")
        button.click()
        time.sleep(0.8)

    def _region_option(self, driver: Any, label: str, clicked: list[str]) -> Any | None:
        from appium.webdriver.common.appiumby import AppiumBy

        option = self._find(driver, label, ("XCUIElementTypeStaticText",))
        if option is not None:
            return option
        source = ET.fromstring(driver.page_source)
        if not any(node.get("visible") == "true" and _text(node).startswith("查看")
                   and "宝贝" in _text(node) for node in source.iter("XCUIElementTypeButton")):
            return None
        # Table descendants belong to the region picker, avoiding same-name
        # seller locations in cards behind it. XCTest scrolls the proper column.
        candidates = driver.find_elements(AppiumBy.XPATH,
                                          f"//XCUIElementTypeTable//XCUIElementTypeStaticText[@name='{label}' or @label='{label}']")
        if not candidates:
            return None
        logger.info("iPhone 地区选项滚动显示 target=%s", label)
        driver.execute_script("mobile: scrollToElement", {"elementId": candidates[0].id})
        clicked.append(f"滚动至 {label}")
        for _ in range(6):
            option = self._find(driver, label, ("XCUIElementTypeStaticText",))
            if option is not None:
                return option
            time.sleep(0.3)
        return None

    def _apply_region(self, driver: Any, city: str, steps: list[str]) -> list[str]:
        from appium.webdriver.common.appiumby import AppiumBy

        if result_bar_label(driver.page_source, city):
            logger.info("iPhone 地区已选中 city=%s", city)
            return [f"已选中 {city}"]
        clicked: list[str] = []
        path = [label for step in steps if not (label := step.removeprefix("?")).startswith("查看")]
        if not path or path[0] != "区域":
            path.insert(0, "区域")
        path[0] = result_region_chip(driver.page_source) or path[0]
        for step in path:
            element = self._region_option(driver, step, clicked)
            if element is None:
                raise RuntimeError(f"地区选项「{step}」未出现（已点击 {' → '.join(clicked)}）")
            element.click()
            clicked.append(step)
            logger.info("iPhone 地区点击 %s", step)
            time.sleep(0.3)
        # The phone picker exposes a third district column. Selecting the
        # whole city avoids retaining a district from the previous search.
        whole_city = self._find(driver, f"全{city}", ("XCUIElementTypeStaticText",))
        if whole_city and f"全{city}" not in clicked:
            whole_city.click()
            clicked.append(f"全{city}")
        buttons = driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeButton")
        confirm = next((element for element in buttons if element.is_displayed()
                        and str(element.get_attribute("name") or "").startswith("查看")
                        and "宝贝" in str(element.get_attribute("name") or "")), None)
        if confirm is None:
            raise RuntimeError(f"地区确认按钮未出现（已点击 {' → '.join(clicked)}）")
        confirm.click()
        clicked.append("查看宝贝")
        for _ in range(12):
            if result_bar_label(driver.page_source, city):
                logger.info("iPhone 地区已应用 city=%s clicked=%s", city, clicked)
                return clicked
            time.sleep(0.4)
        raise RuntimeError(f"地区确认后排序栏未显示「{city}」（已点击 {' → '.join(clicked)}）")

    def _reveal_storage_chip(self, driver: Any) -> tuple[str, list[str]]:
        restored: list[str] = []
        vertical_attempts = horizontal_attempts = 0
        for _ in range(6):
            source = driver.page_source
            current = active_storage_chip(source)
            if current is not None:
                return current, restored
            if not has_result_sort_bar(source):
                raise RuntimeError("当前页面缺少搜索结果排序栏，停止容量切换以免误点商品")
            hidden = _storage_chip_node(source, visible_only=False)
            root = ET.fromstring(source)
            app = next(root.iter("XCUIElementTypeApplication"), root)
            width = int(app.get("width") or 0)
            if (hidden is not None and width and horizontal_attempts < 2
                    and (int(hidden.get("x") or 0) + int(hidden.get("width") or 0) > width
                         or int(hidden.get("x") or 0) < 0)):
                # The capacity chip can sit beyond the edge on narrow phones.
                # Drag only the observed chip row, using this device's geometry.
                off_right = int(hidden.get("x") or 0) + int(hidden.get("width") or 0) > width
                y = int(hidden.get("y") or 0) + int(hidden.get("height") or 0) // 2
                driver.execute_script("mobile: dragFromToForDuration", {
                    "duration": 0.3, "fromX": int(width * (0.85 if off_right else 0.25)), "fromY": y,
                    "toX": int(width * (0.25 if off_right else 0.85)), "toY": y,
                })
                horizontal_attempts += 1
                restored.append("横向显示容量筛选")
                logger.info("iPhone 容量入口在屏幕外，横向显示筛选项 attempt=%s width=%s", horizontal_attempts, width)
            elif vertical_attempts < 3:
                vertical_attempts += 1
                logger.info("iPhone 容量入口未显示，下滑恢复筛选栏 attempt=%s", vertical_attempts)
                driver.execute_script("mobile: swipe", {"direction": "down", "velocity": 700})
                restored.append("下滑恢复筛选栏")
            else:
                break
            time.sleep(0.6)
        raise RuntimeError(f"恢复筛选栏后仍找不到存储容量入口（下滑{vertical_attempts}次，横向{horizontal_attempts}次）")

    def _apply_storage(self, driver: Any, storage: str) -> list[str]:
        from appium.webdriver.common.appiumby import AppiumBy

        target = f"{storage}B" if storage.endswith("T") else f"{storage}GB"
        current, restored = self._reveal_storage_chip(driver)
        sort = self._find(driver, "新发", ("XCUIElementTypeStaticText",))
        sort_y = sort.rect["y"] if sort else -1000
        choices = driver.find_elements(AppiumBy.IOS_PREDICATE, f"name == '{current}' OR label == '{current}'")
        entrance = next((element for element in choices if element.is_displayed()
                         and element.get_attribute("type") == "XCUIElementTypeStaticText"
                         and sort_y + 20 <= element.rect["y"] <= sort_y + 70), None)
        if entrance is None:
            raise RuntimeError(f"找不到容量入口「{current}」")
        entrance.click()
        clicked = [*restored, current]
        if self._find(driver, "确定", ("XCUIElementTypeButton",)) is None:
            raise RuntimeError("点击容量入口后未出现筛选面板")
        for previous in sorted(checked_storages(driver.page_source) - {target}):
            selected = self._find(driver, previous, ("XCUIElementTypeStaticText",))
            if selected is None:
                raise RuntimeError(f"无法取消旧容量「{previous}」")
            selected.click()
            clicked.append(f"取消 {previous}")
        if target not in checked_storages(driver.page_source):
            option = self._find(driver, target, ("XCUIElementTypeStaticText",))
            if option is None:
                raise RuntimeError(f"容量选项「{target}」未出现")
            option.click()
            clicked.append(target)
        selected = checked_storages(driver.page_source)
        if selected != {target}:
            raise RuntimeError(f"容量勾选校验失败：期望 {target}，实际 {sorted(selected)}")
        self._find(driver, "确定", ("XCUIElementTypeButton",)).click()
        clicked.append("确定")
        for _ in range(12):
            if result_bar_label(driver.page_source, target):
                logger.info("iPhone 容量已应用 storage=%s clicked=%s", target, clicked)
                return clicked
            time.sleep(0.4)
        raise RuntimeError(f"点击确定后列表未显示「{target}」")

    def _return_to_results(self, driver: Any, actions: list[dict[str, Any]]) -> None:
        self._ensure_foreground(driver, actions)
        self._close_share_sheet(driver)
        clicked_back = False
        for _ in range(8):
            source = driver.page_source
            if is_search_results(source):
                collapsed = active_storage_chip(source) is None
                actions.append({"action": "确认搜索列表", "success": True,
                                "method": "排序栏收起，按页面结构确认" if collapsed else "结果页结构确认"})
                logger.info("iPhone 已返回搜索列表 sort_collapsed=%s", collapsed)
                return
            if not clicked_back and self._find(driver, "分享按钮"):
                back = self._find(driver, "返回按钮")
                if back:
                    back.click()
                    clicked_back = True
                    actions.append({"action": "详情返回", "target": "返回按钮", "success": True})
            time.sleep(0.5)
        actions.append({"action": "确认搜索列表", "success": False, "error": "未识别到搜索结果页"})
        raise RuntimeError("返回后未到搜索结果页，停止继续采集")

    def _share_item_id(
        self, driver: Any, udid: str, card: PhoneCard,
        actions: list[dict[str, Any]] | None = None,
    ) -> str:
        from appium.webdriver.common.appiumby import AppiumBy

        title = next((el for el in driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeStaticText")
                      if el.is_displayed() and str(el.get_attribute("name") or "").strip() == card.title
                      and abs(el.rect["y"] - card.y) < 100), None)
        if title is None:
            raise RuntimeError("候选卡片已离开当前屏，跳过以免点错商品")
        actions = actions if actions is not None else []
        self._ensure_foreground(driver, actions)
        title.click()
        actions.append({"action": "打开候选详情", "target": card.title[:60], "success": True})
        operation_error = None
        try:
            for _ in range(8):
                share = self._find(driver, "分享按钮")
                if share:
                    break
                time.sleep(0.4)
            else:
                raise RuntimeError("进入详情后找不到分享按钮")
            if not detail_matches(driver.page_source, card):
                raise RuntimeError("详情页标题或价格与候选卡片不一致，已跳过复制")
            actions.append({"action": "核对详情标题和价格", "target": f"¥{card.price:g}", "success": True})
            share.click()
            actions.append({"action": "打开分享面板", "target": "分享按钮", "success": True})
            copy = None
            for _ in range(10):
                choices = driver.find_elements(AppiumBy.IOS_PREDICATE, "name == '复制链接' OR label == '复制链接'")
                copy = next((element for element in choices if element.rect["width"] > 20), None)
                if copy:
                    break
                time.sleep(0.3)
            if copy is None:
                raise RuntimeError("分享菜单没有复制链接")
            marker = _prepare_pasteboard(udid)
            # Share-sheet icons are present in the AX tree but XCUITest marks
            # them invisible; a coordinate tap on the located icon works.
            rect = copy.rect
            driver.execute_script("mobile: tap", {
                "x": int(rect["x"] + rect["width"] / 2),
                "y": int(rect["y"] + rect["height"] / 2),
            })
            time.sleep(0.4)
            after = ""
            for _ in range(4):
                after = _pasteboard(udid)
                if after and marker not in after:
                    break
                time.sleep(0.3)
            if not after or marker in after:
                raise RuntimeError("手机剪贴板未产生新链接")
            short_urls = [url.rstrip("，。)）") for url in _URL.findall(after)
                          if urlparse(url).hostname in {"m.tb.cn", "www.goofish.com", "h5.m.goofish.com"}]
            if not short_urls:
                raise RuntimeError("复制内容中没有闲鱼分享链接")
            direct = item_id_from_share(after, "")
            if direct:
                actions.append({"action": "取得商品链接", "target": f"ID {direct}", "success": True})
                return direct
            with httpx.Client(timeout=10, follow_redirects=True) as client:
                response = client.get(short_urls[0])
                response.raise_for_status()
            item_id = item_id_from_share(after, response.text)
            if not item_id:
                raise RuntimeError("短链页面中找不到商品 ID")
            actions.append({"action": "取得商品链接", "target": f"ID {item_id}", "success": True})
            return item_id
        except Exception as exc:
            operation_error = exc
            raise
        finally:
            try:
                self._return_to_results(driver, actions)
            except Exception as return_error:
                if operation_error is not None:
                    raise RuntimeError(f"{_brief_error(operation_error)}；{_brief_error(return_error)}") from operation_error
                raise
