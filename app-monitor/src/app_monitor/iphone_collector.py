"""Two-stage iPhone collection: read visible cards, share only price-matching items."""

from __future__ import annotations

import asyncio
import base64
import html
import io
import logging
import re
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import httpx
from .swift_runtime import run_swift
from PIL import Image

from .appium_server import ensure_appium_server
from .collector import CollectResult, DetailCheck, ItemCallback, emit_safely, guess_region
from .config import ROOT, Settings
from .matching import normalize_search_text

logger = logging.getLogger(__name__)
_PRICE = re.compile(r"^[¥￥]\s*([1-9]\d{1,6}(?:\.\d{1,2})?)\s*(?:元)?$")
_PRICE_OTHER = re.compile(r"^([1-9]\d{1,6}(?:\.\d{1,2})?)\s*元$")
_URL = re.compile(r"https?://[^\s<>\"']+")
_SHARE_HOSTS = {"m.tb.cn", "www.goofish.com", "h5.m.goofish.com"}
_ITEM_ID = re.compile(r"(?:[?&](?:id|itemId|item_id)=)(\d{8,})")
_IGNORED = {"新发", "综合", "价格", "筛选", "区域", "刚刚发布", "包邮", "验货宝", "想要", "搜索"}
_STORAGE_LABEL = re.compile(r"^(?:\d+GB|\d+TB)$")


class _PageWaitTimeout(RuntimeError):
    pass


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


def share_confirmation_text(source: str) -> str:
    """Read only the App's visible copy-confirmation text, never a hidden link."""
    for node in ET.fromstring(source).iter():
        text = _text(node)
        if (node.tag not in {"XCUIElementTypeOther", "XCUIElementTypeStaticText", "XCUIElementTypeTextView"}
                or node.get("visible") != "true" or "【闲鱼】" not in text or "点击链接" not in text):
            continue
        if any(urlparse(url).hostname in _SHARE_HOSTS for url in _URL.findall(text)):
            return text
    return ""


def detail_mismatch_reason(source: str, card: PhoneCard) -> str | None:
    detail = ET.fromstring(source)
    expected = normalize_search_text(card.title)
    descriptions = [_text(node) for node in detail.iter("XCUIElementTypeTextView")
                    if node.get("visible") != "false" and _text(node)]
    problems = []
    if len(expected) < 12 or not any(normalize_search_text(value).startswith(expected) for value in descriptions):
        problems.append(f"标题不匹配（列表：{card.title[:70]}；详情：{descriptions[0][:70] if descriptions else '未读到正文'}）")
    prices = []
    for node in detail.iter():
        if node.get("visible") == "false" or not 100 <= int(node.get("y") or 0) < 300:
            continue
        value = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?", _text(node).replace(',', ''))
        if value:
            prices.append(float(value.group(1)))
    if card.price not in prices:
        observed = ' / '.join(f"¥{value:g}" for value in dict.fromkeys(prices)) or '未读到详情价'
        problems.append(f"价格不匹配（列表 ¥{card.price:g}；详情 {observed}）")
    return '；'.join(problems) or None


def detail_matches(source: str, card: PhoneCard) -> bool:
    return detail_mismatch_reason(source, card) is None


def _seller_header(source: str) -> tuple[ET.Element | None, list[ET.Element]]:
    """Read only the seller header beside the detail avatar, never description text."""
    root = ET.fromstring(source)
    visible = [node for node in root.iter() if node.get("visible") != "false"]
    if not any(_text(node) == "分享按钮" for node in visible):
        return None, []
    avatar = next((node for node in visible if _text(node) == "用户头像"), None)
    if avatar is None:
        return None, []
    x, y, width, height = (int(avatar.get(key) or 0) for key in ("x", "y", "width", "height"))
    if width <= 0 or height <= 0:
        return None, []
    row = [node for node in visible
           if node.tag in {"XCUIElementTypeOther", "XCUIElementTypeButton", "XCUIElementTypeStaticText", "XCUIElementTypeImage"}
           and int(node.get("x") or 0) >= x + width
           and abs(int(node.get("y") or 0) + int(node.get("height") or 0) / 2 - (y + height / 2)) <= height / 2
           and 0 < int(node.get("height") or 0) <= height * 1.5]
    names = [node for node in row if _text(node) and _text(node) != "鱼小铺"
             and node.tag != "XCUIElementTypeImage"
             and 0 <= int(node.get("x") or 0) - (x + width) <= 28]
    if not names:
        return None, []
    name = min(names, key=lambda node: (int(node.get("x") or 0), int(node.get("width") or 0)))
    right = int(name.get("x") or 0) + int(name.get("width") or 0)
    badges = [node for node in row if int(node.get("x") or 0) >= right - 2
              and int(node.get("width") or 0) <= 120]
    return name, badges


def _badge_rect(node: ET.Element) -> tuple[int, int, int, int]:
    return tuple(int(node.get(key) or 0) for key in ("x", "y", "width", "height"))


def detail_seller(source: str, badge_texts: dict[tuple[int, ...], str] | None = None) -> dict[str, Any]:
    name, badges = _seller_header(source)
    if name is None:
        return {"seller_name": "", "is_fish_shop": None}
    labels = badge_texts or {}
    fish_shop = any("鱼小铺" in re.sub(r"\s+", "", _text(node) or labels.get(_badge_rect(node), ""))
                    for node in badges)
    # An unlabeled badge is not evidence that this is an ordinary seller.
    known_other_badges = {"减碳榜样", "信用极好", "信用优秀", "信用良好"}
    ambiguous = any(node.tag == "XCUIElementTypeImage" and not _text(node)
                    and re.sub(r"\s+", "", labels.get(_badge_rect(node), "")) not in known_other_badges
                    for node in badges)
    return {"seller_name": _text(name), "is_fish_shop": True if fish_shop else (None if ambiguous else False)}


def recognize_seller_badges(png: bytes, source: str, badges: list[ET.Element], *, sdk: str = "") -> dict[tuple[int, ...], str]:
    """OCR only unnamed seller badges; full screenshots remain in memory."""
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    logical_width, logical_height = int(app.get("width") or 0), int(app.get("height") or 0)
    if not logical_width or not logical_height:
        return {}
    labels = {}
    with Image.open(io.BytesIO(png)) as image, tempfile.TemporaryDirectory(prefix="goofish-badge-") as temp:
        scale_x, scale_y = image.width / logical_width, image.height / logical_height
        for index, badge in enumerate(badges):
            x, y, width, height = rect = _badge_rect(badge)
            if width <= 0 or height <= 0 or not (0 <= x < x + width <= logical_width and 0 <= y < y + height <= logical_height):
                continue
            crop = image.crop((max(0, (x - 2) * scale_x), max(0, (y - 2) * scale_y),
                               min(image.width, (x + width + 2) * scale_x), min(image.height, (y + height + 2) * scale_y)))
            path = Path(temp) / f"badge-{index}.png"
            crop.resize((crop.width * 3, crop.height * 3), Image.Resampling.LANCZOS).save(path)
            result = run_swift(ROOT / "scripts" / "vision_ocr.swift", [str(path)], sdk=sdk, timeout=15)
            if result.returncode == 0:
                texts = [parts[5] for line in result.stdout.splitlines() if len(parts := line.split("\t", 5)) == 6]
                labels[rect] = "".join(texts)
    return labels


def detail_content(source: str) -> dict[str, Any]:
    """Capture the complete accessible description, not just the listing summary."""
    root = ET.fromstring(source)
    descriptions = list(dict.fromkeys(_text(n) for n in root.iter("XCUIElementTypeTextView")
                                      if _text(n) and n.get("visible") != "false"))
    parameters = []
    for node in root.iter():
        if _text(node) not in {"商品规格信息", "商品参数"}:
            continue
        values = list(dict.fromkeys(_text(child) for child in node.iter()
                                    if child is not node and _text(child)
                                    and child.tag in {"XCUIElementTypeStaticText", "XCUIElementTypeTextView"}))
        parameters.extend(values)
    return {"description": "\n\n".join(descriptions), "detail_parameters": list(dict.fromkeys(parameters))}


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


def model_filter_chip(source: str) -> ET.Element | None:
    """品牌入口或已选机型只从结果页第二行标签识别，排除搜索词与商品标题。"""
    root = ET.fromstring(source)
    nodes = list(root.iter("XCUIElementTypeStaticText"))
    sort = next((n for n in nodes if _text(n) == "新发" and n.get("visible") == "true"), None)
    if sort is None:
        return None
    app = next(root.iter("XCUIElementTypeApplication"), root)
    width = int(app.get("width") or 0)
    return next((n for n in nodes if n.get("visible") == "true"
                 and int(sort.get("y") or 0) + 20 <= int(n.get("y") or 0) <= int(sort.get("y") or 0) + 70
                 and 0 <= int(n.get("x") or 0)
                 and (not width or int(n.get("x") or 0) + int(n.get("width") or 0) <= width)
                 and (_text(n) in {"品牌", "品牌机型", "品牌型号", "苹果/Apple"}
                      or re.fullmatch(r"iPhone\s*(?:\d{1,2}|X[RS]?|SE|Air)(?:[\w\s]*)", _text(n), re.I))), None)


def _model_picker_bounds(source: str) -> tuple[ET.Element, ET.Element] | None:
    root = ET.fromstring(source)
    apple = next((n for n in root.iter("XCUIElementTypeStaticText")
                  if _text(n) == "苹果/Apple" and n.get("visible") == "true"), None)
    confirm = next((n for n in root.iter("XCUIElementTypeButton")
                    if _text(n) == "确定" and n.get("visible") == "true"), None)
    return (apple, confirm) if apple is not None and confirm is not None else None


def model_picker_option(source: str, keyword: str) -> ET.Element | None:
    bounds = _model_picker_bounds(source)
    if bounds is None:
        return None
    apple, confirm = bounds
    root = ET.fromstring(source)
    for cell in root.iter("XCUIElementTypeCell"):
        if (cell.get("visible") != "true"
                or int(cell.get("x") or 0) < int(apple.get("x") or 0) + int(apple.get("width") or 0)
                or int(cell.get("y") or 0) < int(apple.get("y") or 0)
                or int(cell.get("y") or 0) + int(cell.get("height") or 0) > int(confirm.get("y") or 0)):
            continue
        for node in cell.iter("XCUIElementTypeStaticText"):
            if node.get("visible") == "true" and normalize_search_text(_text(node)) == normalize_search_text(keyword):
                return node
    return None


def checked_models(source: str, screenshot: bytes) -> set[str]:
    """机型黄底选中态未暴露给 AX；仅检查已定位的面板单元格，不保存截图。"""
    bounds = _model_picker_bounds(source)
    if bounds is None:
        return set()
    apple, confirm = bounds
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    width, height = int(app.get("width") or 0), int(app.get("height") or 0)
    if not width or not height:
        return set()
    selected = set()
    with Image.open(io.BytesIO(screenshot)) as raw:
        image = raw.convert("RGB")
        sx, sy = image.width / width, image.height / height
        for cell in root.iter("XCUIElementTypeCell"):
            x, y, w, h = (int(cell.get(k) or 0) for k in ("x", "y", "width", "height"))
            if (cell.get("visible") != "true" or w <= 6 or h <= 6
                    or x < int(apple.get("x") or 0) + int(apple.get("width") or 0)
                    or y < int(apple.get("y") or 0) or y + h > int(confirm.get("y") or 0)):
                continue
            labels = [_text(n) for n in cell.iter("XCUIElementTypeStaticText")
                      if n.get("visible") == "true" and _text(n).lower().startswith("iphone")
                      and "系列" not in _text(n)]
            if len(labels) != 1:
                continue
            patch = image.crop((int((x + 3) * sx), int((y + 3) * sy),
                                int((x + w - 3) * sx), int((y + h - 3) * sy))).resize((24, 12))
            yellow = sum(r >= 190 and g >= 150 and b <= 110 and r >= g * .9 for r, g, b in patch.getdata())
            if yellow / (24 * 12) >= .3:
                selected.add(labels[0])
    return selected


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


def _pasteboard(driver: Any, *, settings: Settings, content: str | None = None) -> str:
    """WDA must be foreground to access the real device clipboard on iOS 17.

    Temporarily disable XCTest's idle wait when activating its own runner, then
    verify the actual foreground app. Always restore Goofish and the prior wait.
    Clipboard contents must never be included in errors or logs.
    """
    runner = settings.ios_wda_bundle_id.removesuffix(".xctrunner") + ".xctrunner"
    if not settings.ios_wda_bundle_id:
        raise RuntimeError("手机剪贴板不可用：未配置 WDA bundle ID")
    try:
        idle_timeout = driver.get_settings().get("waitForIdleTimeout", 0.5)

        def activate(bundle_id: str) -> None:
            driver.execute_script("mobile: activateApp", {"bundleId": bundle_id})
            deadline = time.monotonic() + 5
            while driver.execute_script("mobile: activeAppInfo").get("bundleId") != bundle_id:
                if time.monotonic() >= deadline:
                    raise RuntimeError("剪贴板操作未确认应用前台状态")
                time.sleep(0.15)

        try:
            driver.update_settings({"waitForIdleTimeout": 0})
            activate(runner)
            if content is not None:
                driver.execute_script("mobile: setClipboard", {
                    "contentType": "plaintext",
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                })
            encoded = driver.execute_script("mobile: getClipboard", {"contentType": "plaintext"})
            if not isinstance(encoded, str):
                raise ValueError("Unexpected clipboard response")
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        finally:
            try:
                activate(settings.bundle_id)
            finally:
                driver.update_settings({"waitForIdleTimeout": idle_timeout})
    except Exception as exc:
        raise RuntimeError("手机剪贴板读写失败，请检查 WDA 连接及手机粘贴权限；本件未取得链接") from exc


def _prepare_pasteboard(driver: Any, *, settings: Settings) -> str:
    # A fresh marker verifies this copy action even if the same product was
    # copied on a previous run. Never accept a stale share URL as a new copy.
    marker = f"app-monitor-copy-{uuid.uuid4().hex}"
    if _pasteboard(driver, settings=settings, content=marker) != marker:
        raise RuntimeError("手机剪贴板标记写入后回读不一致，已跳过复制以免使用旧链接")
    return marker


class IPhoneCollector:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _wait_for_source(
        driver: Any, ready: Callable[[str], bool], failure: str,
        *, timeout: float = 8, interval: float = 0.15,
    ) -> str:
        """条件满足立即返回该快照；超时仅作为失败上限，不固定等待。"""
        started = time.monotonic()
        deadline = started + timeout
        polls = 0
        while True:
            source = driver.page_source
            polls += 1
            try:
                if ready(source):
                    logger.debug("iPhone 页面条件满足 elapsed=%.3fs polls=%s", time.monotonic() - started, polls)
                    return source
            except ET.ParseError:
                pass  # 转场期间的空快照不作为成功依据。
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _PageWaitTimeout(f"{failure}（等待 {timeout:g} 秒超时）")
            time.sleep(min(interval, remaining))

    async def collect(
        self, udid: str, os_version: str, keyword: str, region_hint: str,
        region_steps: list[str], storages: list[str], scroll_pages: int,
        is_candidate: Callable[[dict[str, Any], bool, str], bool],
        on_items: ItemCallback | None = None,
        check_detail: DetailCheck | None = None,
    ) -> CollectResult:
        return await asyncio.to_thread(
            self._collect, udid, os_version, keyword, region_hint, region_steps,
            storages, scroll_pages, is_candidate, on_items, check_detail,
        )

    def _collect(
        self, udid: str, os_version: str, keyword: str, region_hint: str,
        region_steps: list[str], storages: list[str], scroll_pages: int,
        is_candidate: Callable[[dict[str, Any], bool, str], bool],
        on_items: ItemCallback | None = None,
        check_detail: DetailCheck | None = None,
    ) -> CollectResult:
        """在线程里运行；每拿到一件候选商品的链接就通过 ``on_items`` 交出，边搜边判价、推送。"""
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
            "appium:waitForIdleTimeout": 0.5,
            "appium:settings": {"waitForIdleTimeout": 0.5, "animationCoolOffTimeout": 0.3},
        }
        if os_version:
            capabilities["appium:platformVersion"] = os_version
        result = CollectResult(items=[])

        def add_pass(entry: dict[str, Any]) -> None:
            result.passes.append(entry)
            emit_safely(on_items, pass_info=entry, storage=entry.get("storage"))

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
            try:
                emit_safely(on_items, stage=f"iPhone 筛选机型 {keyword}")
                clicked = self._apply_model(driver, keyword)
                setup_actions.append({"action": "机型筛选", "target": keyword,
                                      "clicked": clicked, "success": True})
            except Exception as exc:
                error = _brief_error(exc)
                setup_actions.append({"action": "机型筛选", "target": keyword,
                                      "success": False, "error": error})
                add_pass({"label": "新发", "storage": None, "observed": 0,
                          "error": error, "actions": setup_actions, "model_applied": False})
                return result
            for storage in storages:
                storage_label = storage if storage.endswith("T") else f"{storage}G"
                emit_safely(on_items, stage=f"iPhone 切换容量 {storage_label}", storage=storage)
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
                    source = driver.page_source
                    list_changed = False
                    for page in range(max(0, scroll_pages) + 1):
                        if page:
                            # Detail visits may move/collapse the list. Use its
                            # actual current viewport as the gesture baseline.
                            previous = driver.page_source if list_changed else source
                            source = self._scroll_results(driver, previous)
                            if parse_cards(source) == parse_cards(previous):
                                actions.append({"action": "结束列表翻页", "method": "拖动及备用滑动后列表位置均未变化",
                                                "success": True})
                                break
                        list_changed = False
                        if page:
                            actions.append({"action": "上滑列表", "target": f"第 {page + 1} 屏", "success": True})
                        emit_safely(on_items, stage=f"iPhone 读取 {storage_label} 第 {page + 1}/{max(0, scroll_pages) + 1} 屏",
                                    storage=storage)
                        cards = parse_cards(source, region_hint)
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
                            card_meta = {"storage": storage, "region_applied": result.region_applied,
                                         "filter_applied": True}
                            if not is_candidate(raw, result.region_applied, storage):
                                # 不符合条件的卡片不进详情取链接，但也记进运行记录，方便核对「看到了什么」
                                emit_safely(on_items, cards=[dict(raw)], **card_meta)
                                continue
                            candidates += 1
                            seller_info: dict[str, Any] = {}
                            try:
                                list_changed = True
                                detail_gate = (lambda item: check_detail(item, result.region_applied, storage)) if check_detail else None
                                item_id = self._share_item_id(driver, udid, card, actions, seller_info,
                                                              check_detail=detail_gate)
                            except Exception as exc:
                                logger.warning("iPhone 候选分享失败 title=%s error=%s", card.title[:40], exc)
                                actions.append({"action": "复制候选链接", "target": card.title[:40],
                                                "success": False, "error": _brief_error(exc)})
                                emit_safely(on_items, cards=[{**raw, "link_error": _brief_error(exc)}], **card_meta)
                                if not is_search_results(driver.page_source):
                                    raise RuntimeError("分享失败后未回到搜索列表，停止本机采集") from exc
                                continue
                            rejection = seller_info.pop("detail_rejection", None)
                            if rejection:
                                emit_safely(on_items, cards=[{**raw, "detail_rejection": rejection}], **card_meta)
                            elif item_id:
                                item = {**raw, **seller_info, "item_id": item_id,
                                        "item_url": f"https://www.goofish.com/item?id={item_id}",
                                        "image_urls": [], "source_storage": storage, "source_pass": "新发"}
                                pass_items.append(item)
                                result.items.append(item)
                                emit_safely(on_items, [dict(item)], storage=storage,
                                            region_applied=result.region_applied, filter_applied=True,
                                            stage=f"iPhone 读取 {storage_label}")
                                actions.append({"action": "复制候选链接", "target": card.title[:40],
                                                "success": True})
                                logger.info("iPhone 候选关联 item_id=%s price=%s title=%s",
                                            item_id, card.price, card.title[:60])
                            else:
                                emit_safely(on_items, cards=[{**raw, "link_error": "分享内容里没有商品 ID"}], **card_meta)
                except Exception as exc:
                    error = _brief_error(exc)
                    actions.append({"action": "容量筛选或采集", "target": storage,
                                    "success": False, "error": error})
                    logger.error("iPhone 容量采集失败 storage=%s error=%s", storage, error)
                add_pass({
                    "label": "新发", "storage": storage, "filter_applied": error is None,
                    "region_applied": result.region_applied, "observed": len(pass_items),
                    "model_applied": True,
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

        quoted = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"(name == '{quoted}' OR label == '{quoted}') AND visible == true"
        if types:
            query += " AND type IN {" + ", ".join(f"'{kind}'" for kind in types) + "}"
        # 可见性和类型交给同一次原生查询，避免逐个元素再次跨设备读属性。
        elements = driver.find_elements(AppiumBy.IOS_PREDICATE, query)
        return next(iter(elements), None)

    @staticmethod
    def _candidate_title(driver: Any, card: PhoneCard) -> Any | None:
        from appium.webdriver.common.appiumby import AppiumBy

        quoted = card.title.replace("\\", "\\\\").replace("'", "\\'")
        # CONTAINS retains names with surrounding whitespace. Only an exact
        # stripped name at the observed card position may actually be clicked.
        query = (f"type == 'XCUIElementTypeStaticText' AND visible == true "
                 f"AND name CONTAINS '{quoted}'")
        return next((element for element in driver.find_elements(AppiumBy.IOS_PREDICATE, query)
                     if str(element.get_attribute("name") or "").strip() == card.title
                     and abs(element.rect["y"] - card.y) < 100), None)

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

    def _scroll_results(self, driver: Any, source: str) -> str:
        """A bounded drag keeps adjacent viewports overlapping; wait for progress."""
        if not is_search_results(source):
            raise RuntimeError("列表滚动前未确认搜索结果页")
        root = ET.fromstring(source)
        app = root if root.tag == "XCUIElementTypeApplication" else root.find(".//XCUIElementTypeApplication")
        width = float(app.get("width") or 0) if app is not None else 0
        height = float(app.get("height") or 0) if app is not None else 0
        if not width or not height:
            size = driver.get_window_size()
            width, height = size["width"], size["height"]
        top = max(height * .22, max((float(node.get("y") or 0) + 100 for node in root.iter()
                  if _text(node) in {"新发", "筛选"} and node.get("visible") == "true"), default=0))
        bottom = height * .86
        distance = (bottom - top) * .75
        if distance < 80:
            raise RuntimeError("搜索列表可滚动区域不足")

        def position(xml: str) -> tuple:
            return tuple((card.title, card.price, card.region, card.y) for card in parse_cards(xml))

        before = position(source)
        before_y = {(title, price, region): y for title, price, region, y in before}
        started = time.monotonic()
        last = source

        def progressed(xml: str) -> bool:
            nonlocal last
            last = xml
            if not is_search_results(xml):
                return False
            current = position(xml)
            # A collapsing toolbar or a few pixels of animation is not a new
            # viewport. Require a new card or meaningful upward movement.
            return any((title, price, region) not in before_y
                       or before_y[(title, price, region)] - y >= min(60, distance * .25)
                       for title, price, region, y in current)

        try:
            driver.execute_script("mobile: dragFromToWithVelocity", {
                "pressDuration": .5, "holdDuration": .5, "velocity": 600,
                "fromX": width / 2, "toX": width / 2,
                "fromY": bottom, "toY": bottom - distance,
            })
        except Exception as exc:
            logger.warning("iPhone 受控拖动失败，核对当前页面后使用备用手势：%s", _brief_error(exc))
            # A command may fail after moving the UI. Re-read before recovery.
            if progressed(driver.page_source):
                return last
        else:
            logger.info("iPhone 列表拖动 command_elapsed=%.3fs distance=%.0f", time.monotonic() - started, distance)
            try:
                current = self._wait_for_source(driver, progressed, "拖动后列表未变化", timeout=5)
                logger.info("iPhone 列表进度 method=受控拖动 elapsed=%.3fs", time.monotonic() - started)
                return current
            except _PageWaitTimeout:
                pass
        # A bottom boundary or an App version with a different gesture handler
        # retains the original swipe path, never another screen's controls.
        if not is_search_results(last):
            raise RuntimeError("拖动后离开搜索结果页，停止滚动")
        logger.info("iPhone 列表进度 method=回退原生滑动")
        driver.execute_script("mobile: swipe", {"direction": "up", "velocity": 900})
        try:
            return self._wait_for_source(driver, progressed, "滑动后列表未变化", timeout=5)
        except _PageWaitTimeout:
            if is_search_results(last):
                return last
            raise

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
        for _ in range(12):
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
            if (hidden is not None and width and horizontal_attempts < 8
                    and (int(hidden.get("x") or 0) + int(hidden.get("width") or 0) > width
                         or int(hidden.get("x") or 0) < 0)):
                # Correct the observed overflow in small steps. A full-width
                # drag can fling the chip across the opposite edge on iOS.
                off_right = int(hidden.get("x") or 0) + int(hidden.get("width") or 0) > width
                y = int(hidden.get("y") or 0) + int(hidden.get("height") or 0) // 2
                overflow = (int(hidden.get("x") or 0) + int(hidden.get("width") or 0) - width
                            if off_right else -int(hidden.get("x") or 0))
                distance = min(width * .4, max(45, overflow + 24))
                start = width * (.75 if off_right else .25)
                driver.execute_script("mobile: dragFromToWithVelocity", {
                    "pressDuration": 0.5, "holdDuration": 0.5, "velocity": 180,
                    "fromX": int(start), "fromY": y,
                    "toX": int(start - distance if off_right else start + distance), "toY": y,
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

    @staticmethod
    def _tap_node(driver: Any, node: ET.Element) -> None:
        driver.execute_script("mobile: tap", {
            "x": int(node.get("x") or 0) + int(node.get("width") or 0) / 2,
            "y": int(node.get("y") or 0) + int(node.get("height") or 0) / 2,
        })

    @staticmethod
    def _scroll_model_picker(driver: Any, bounds: tuple[ET.Element, ET.Element], direction: str) -> None:
        apple, confirm = bounds
        x = (int(apple.get("x") or 0) + int(apple.get("width") or 0)
             + int(confirm.get("x") or 0) + int(confirm.get("width") or 0)) / 2
        top = int(apple.get("y") or 0) + 12
        bottom = int(confirm.get("y") or 0) - 20
        middle = (top + bottom) / 2
        distance = min(180, (bottom - top) * .45)
        if distance <= 0:
            raise RuntimeError("机型面板滚动区域无效")
        # Keep successive viewports overlapping. The old fast, long gesture
        # could fling past an entire model series between snapshots.
        start, end = middle + distance / 2, middle - distance / 2
        driver.execute_script("mobile: dragFromToWithVelocity", {
            "pressDuration": 0.5, "holdDuration": 0.5, "velocity": 180,
            "fromX": x, "toX": x,
            "fromY": start if direction == "up" else end,
            "toY": end if direction == "up" else start,
        })

    @staticmethod
    def _model_picker_position(source: str) -> tuple[tuple[str, str], ...]:
        """Only panel model cells describe scroll progress, never product text."""
        bounds = _model_picker_bounds(source)
        if bounds is None:
            return ()
        apple, confirm = bounds
        return tuple((_text(node), node.get("y", ""))
                     for cell in ET.fromstring(source).iter("XCUIElementTypeCell")
                     if cell.get("visible") == "true"
                     and int(cell.get("x") or 0) >= int(apple.get("x") or 0) + int(apple.get("width") or 0)
                     and int(cell.get("y") or 0) < int(confirm.get("y") or 0)
                     for node in cell.iter("XCUIElementTypeStaticText")
                     if node.get("visible") == "true")

    def _apply_model(self, driver: Any, keyword: str) -> list[str]:
        """精确核对面板选中态，并确认提交后的品牌标签已生效。"""
        source = driver.page_source
        clicked = []
        if _model_picker_bounds(source) is None:
            for _ in range(4):
                chip = model_filter_chip(source)
                if chip is not None:
                    if normalize_search_text(_text(chip)) == normalize_search_text(keyword):
                        return [f"已确认 {_text(chip)}"]
                    self._tap_node(driver, chip)
                    clicked.append(_text(chip))
                    break
                root = ET.fromstring(source)
                sort = next((n for n in root.iter("XCUIElementTypeStaticText")
                             if _text(n) == "新发" and n.get("visible") == "true"), None)
                if sort is None:
                    raise RuntimeError("结果页排序栏不可见，无法定位品牌机型入口")
                app = next(root.iter("XCUIElementTypeApplication"), root)
                width = int(app.get("width") or 0)
                if width <= 0:
                    raise RuntimeError("无法确定筛选标签栏宽度")
                driver.execute_script("mobile: dragFromToForDuration", {
                    "duration": 0.3, "fromX": width - 20, "toX": 20,
                    "fromY": int(sort.get("y") or 0) + 45, "toY": int(sort.get("y") or 0) + 45,
                })
                source = driver.page_source
            source = self._wait_for_source(driver, lambda xml: _model_picker_bounds(xml) is not None,
                                           "品牌机型面板未出现，未继续采集")
        bounds = _model_picker_bounds(source)
        if bounds is None:
            raise RuntimeError("品牌机型面板未出现，未继续采集")
        self._tap_node(driver, bounds[0])
        clicked.append("苹果/Apple")
        # 「全部」可消除上次勾选，防止再次点击当前机型反而取消选中。
        previous_position = None
        unchanged = 0
        for attempt in range(25):
            source = driver.page_source
            bounds = _model_picker_bounds(source)
            if bounds is None:
                raise RuntimeError("品牌机型面板意外关闭")
            apple, confirm = bounds
            all_option = next((n for n in ET.fromstring(source).iter("XCUIElementTypeButton")
                               if _text(n) == "全部" and n.get("visible") == "true"
                               and int(n.get("x") or 0) >= int(apple.get("x") or 0) + int(apple.get("width") or 0)
                               and int(apple.get("y") or 0) <= int(n.get("y") or 0) < int(confirm.get("y") or 0)), None)
            if all_option is not None:
                self._tap_node(driver, all_option)
                clicked.append("全部")
                break
            position = self._model_picker_position(source)
            unchanged = unchanged + 1 if position == previous_position else 0
            if attempt == 24 or unchanged >= 2:
                raise RuntimeError("无法清空品牌面板中的旧机型")
            previous_position = position
            self._scroll_model_picker(driver, bounds, "down")
        previous_position = None
        unchanged = 0
        for attempt in range(25):
            source = driver.page_source
            option = model_picker_option(source, keyword)
            if option is not None:
                self._tap_node(driver, option)
                clicked.append(_text(option))
                break
            bounds = _model_picker_bounds(source)
            if bounds is None:
                raise RuntimeError("机型选项面板意外关闭")
            position = self._model_picker_position(source)
            unchanged = unchanged + 1 if position == previous_position else 0
            if attempt == 24 or unchanged >= 2:
                raise RuntimeError(f"苹果品牌下未找到精确机型「{keyword}」，未继续采集")
            previous_position = position
            self._scroll_model_picker(driver, bounds, "up")
        def selected_model_ready(xml: str) -> bool:
            return (_model_picker_bounds(xml) is not None
                    and {normalize_search_text(name) for name in checked_models(xml, driver.get_screenshot_as_png())}
                    == {normalize_search_text(keyword)})

        source = self._wait_for_source(driver, selected_model_ready, f"机型勾选校验失败：期望 {keyword}")
        bounds = _model_picker_bounds(source)
        if bounds is None:
            raise RuntimeError("选择机型后未找到确认面板")
        clicked.append(f"已校验 {keyword}")
        self._tap_node(driver, bounds[1])
        clicked.append("确定")
        def model_applied(xml: str) -> bool:
            chip = model_filter_chip(xml)
            return (chip is not None and _model_picker_bounds(xml) is None
                    and (_text(chip) == "苹果/Apple"
                         or normalize_search_text(_text(chip)) == normalize_search_text(keyword)))
        self._wait_for_source(driver, model_applied, f"提交后未确认精确机型「{keyword}」，未继续采集")
        return clicked

    def _apply_storage(self, driver: Any, storage: str) -> list[str]:
        from appium.webdriver.common.appiumby import AppiumBy

        target = f"{storage}B" if storage.endswith("T") else f"{storage}GB"
        current, restored = self._reveal_storage_chip(driver)
        if current == target:
            return [*restored, f"已确认 {target}"]
        sort = self._find(driver, "新发", ("XCUIElementTypeStaticText",))
        sort_y = sort.rect["y"] if sort else -1000
        choices = driver.find_elements(AppiumBy.IOS_PREDICATE,
                                       f"(name == '{current}' OR label == '{current}') "
                                       "AND type == 'XCUIElementTypeStaticText' AND visible == true")
        entrance = next((element for element in choices
                         if sort_y + 20 <= element.rect["y"] <= sort_y + 70), None)
        if entrance is None:
            raise RuntimeError(f"找不到容量入口「{current}」")
        entrance.click()
        clicked = [*restored, current]
        def picker_open(xml: str) -> bool:
            return any(_text(n) == "确定" and n.get("visible") == "true"
                       for n in ET.fromstring(xml).iter("XCUIElementTypeButton"))
        source = self._wait_for_source(driver, picker_open, "点击容量入口后未出现筛选面板")
        for previous in sorted(checked_storages(source) - {target}):
            selected = self._find(driver, previous, ("XCUIElementTypeStaticText",))
            if selected is None:
                raise RuntimeError(f"无法取消旧容量「{previous}」")
            selected.click()
            clicked.append(f"取消 {previous}")
            source = self._wait_for_source(driver, lambda xml: picker_open(xml) and previous not in checked_storages(xml),
                                           f"无法确认取消旧容量「{previous}」")
        if target not in checked_storages(source):
            option = self._find(driver, target, ("XCUIElementTypeStaticText",))
            if option is None:
                raise RuntimeError(f"容量选项「{target}」未出现")
            option.click()
            clicked.append(target)
            source = self._wait_for_source(driver, lambda xml: picker_open(xml) and checked_storages(xml) == {target},
                                           f"容量勾选校验失败：期望 {target}")
        self._find(driver, "确定", ("XCUIElementTypeButton",)).click()
        clicked.append("确定")
        self._wait_for_source(driver, lambda xml: not picker_open(xml) and result_bar_label(xml, target),
                              f"点击确定后列表未显示「{target}」")
        logger.info("iPhone 容量已应用 storage=%s clicked=%s", target, clicked)
        return clicked

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
            has_share = any("分享按钮" in {node.get("name"), node.get("label"), _text(node)} and node.get("visible") != "false"
                            for node in ET.fromstring(source).iter())
            if not clicked_back and has_share:
                back = self._find(driver, "返回按钮")
                if back:
                    back.click()
                    clicked_back = True
                    actions.append({"action": "详情返回", "target": "返回按钮", "success": True})
            time.sleep(0.5)
        actions.append({"action": "确认搜索列表", "success": False, "error": "未识别到搜索结果页"})
        raise RuntimeError("返回后未到搜索结果页，停止继续采集")

    def _copy_from_share(self, driver: Any, actions: list[dict[str, Any]], *, wait_confirmation=True) -> str:
        from appium.webdriver.common.appiumby import AppiumBy

        share = self._find(driver, "分享按钮")
        if share is None:
            raise RuntimeError("详情页找不到分享按钮")
        share.click()
        actions.append({"action": "打开分享面板", "target": "分享按钮", "success": True})
        for _ in range(10):
            choices = driver.find_elements(AppiumBy.IOS_PREDICATE,
                                          "(name == '复制链接' OR label == '复制链接') AND visible == true")
            rect = next((bounds for element in choices if (bounds := element.rect)["width"] > 20), None)
            if rect:
                break
            time.sleep(.3)
        else:
            raise RuntimeError("分享菜单没有复制链接")
        if share_confirmation_text(driver.page_source):
            raise RuntimeError("分享面板仍有旧的复制确认文案，已跳过以免关联旧商品")
        driver.execute_script("mobile: tap", {
            "x": int(rect["x"] + rect["width"] / 2), "y": int(rect["y"] + rect["height"] * .9),
        })
        if not wait_confirmation:
            return ""
        try:
            source = self._wait_for_source(driver, lambda xml: bool(share_confirmation_text(xml)),
                                           "未读到本次复制确认文案", timeout=3)
        except _PageWaitTimeout:
            return ""
        actions.append({"action": "读取分享确认文案", "target": "本次可见确认框", "success": True})
        return share_confirmation_text(source)

    def _share_item_id(
        self, driver: Any, udid: str, card: PhoneCard,
        actions: list[dict[str, Any]] | None = None,
        seller_info: dict[str, Any] | None = None,
        *, check_detail: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> str:
        title = self._candidate_title(driver, card)
        if title is None:
            raise RuntimeError("候选卡片已离开当前屏，跳过以免点错商品")
        actions = actions if actions is not None else []
        seller_info = seller_info if seller_info is not None else {}
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
            # The share button can appear before the description and price load.
            for attempt in range(6):
                source = driver.page_source
                if detail_matches(source, card):
                    break
                if attempt < 5:
                    time.sleep(0.35)
            else:
                reason = detail_mismatch_reason(source, card) or "详情身份校验未通过"
                actions.append({"action": "核对详情标题和价格", "target": f"¥{card.price:g}",
                                "success": False, "error": reason})
                raise RuntimeError(f"详情身份校验失败：{reason}；已跳过复制")
            actions.append({"action": "核对详情标题和价格", "target": f"¥{card.price:g}", "success": True})
            seller_info.update(detail_content(source))
            if check_detail:
                rejection = check_detail({"title": card.title, "price": card.price,
                                          "display_region": card.region, **seller_info})
                if rejection:
                    seller_info["detail_rejection"] = rejection
                    actions.append({"action": "详情提前判价", "target": rejection["reason"],
                                    "method": "不符合条件，跳过分享取链接", "success": True})
                    return ""
            seller = detail_seller(source)
            # Seller header accessibility may become visible shortly after the description.
            for _ in range(2):
                if seller["seller_name"]:
                    break
                time.sleep(0.3)
                source = driver.page_source
                if not detail_matches(source, card):
                    raise RuntimeError("读取卖家信息时详情页发生变化，已跳过复制")
                seller = detail_seller(source)
            seller_method = "页面树"
            if seller["seller_name"] and seller["is_fish_shop"] is None:
                try:
                    _, badges = _seller_header(source)
                    images = [node for node in badges if node.tag == "XCUIElementTypeImage" and not _text(node)]
                    labels = recognize_seller_badges(driver.get_screenshot_as_png(), source, images, sdk=self.settings.swift_sdk)
                    seller = detail_seller(source, labels)
                    seller_method = "页面树名称 + 标识局部OCR"
                except Exception as exc:
                    logger.warning("iPhone 卖家标识识别失败，保留未识别状态：%s", _brief_error(exc))
            if seller_info is not None:
                seller_info.update(seller)
            shop_label = {True: "是", False: "否", None: "未识别"}[seller["is_fish_shop"]]
            actions.append({"action": "读取卖家信息", "target": f"{seller['seller_name'] or '未识别'} · 鱼小铺：{shop_label}",
                            "method": seller_method, "success": bool(seller["seller_name"])})
            # Most copies expose a fresh confirmation. Avoid switching apps
            # and preparing a clipboard marker unless that path is unavailable.
            copy_started = time.monotonic()
            after = self._copy_from_share(driver, actions)
            method = "本次确认框"
            if not after:
                method = "标记后重新复制并读剪贴板"
                self._close_share_sheet(driver)
                self._wait_for_source(driver, lambda xml: detail_matches(xml, card),
                                      "备用复制前详情身份发生变化，已跳过复制")
                marker = _prepare_pasteboard(driver, settings=self.settings)
                self._wait_for_source(driver, lambda xml: detail_matches(xml, card),
                                      "剪贴板准备后详情身份发生变化，已跳过复制")
                actions.append({"action": "准备手机剪贴板", "target": "本次复制标记已核验", "success": True})
                # A second copy AFTER marker preparation is mandatory. Never
                # fall back to reading a clipboard left by the first attempt.
                self._copy_from_share(driver, actions, wait_confirmation=False)
                for _ in range(4):
                    after = _pasteboard(driver, settings=self.settings)
                    if after and marker not in after:
                        break
                    time.sleep(0.3)
                if not after or marker in after:
                    raise RuntimeError("手机剪贴板未产生新链接")
            logger.info("iPhone 分享复制 method=%s elapsed=%.3fs", method, time.monotonic() - copy_started)
            short_urls = [url.rstrip("，。)）") for url in _URL.findall(after)
                          if urlparse(url).hostname in _SHARE_HOSTS]
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
