"""解析 XCUITest 页面树：搜索结果卡片、详情页、分享链接与页面状态。

这些函数都是纯函数，只读取 ``driver.page_source`` 返回的 XML 文本。
"""

from __future__ import annotations

import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

_PRICE = re.compile(r"^[¥￥]\s*([1-9]\d{0,6}(?:\.\d{1,2})?)\s*(?:元)?$")
_PRICE_OTHER = re.compile(r"^([1-9]\d{0,6}(?:\.\d{1,2})?)\s*元$")
_ITEM_ID = re.compile(r"(?:[?&](?:id|itemId|item_id)=)(\d{8,})")
_IGNORED = {"新发", "综合", "价格", "筛选", "区域", "刚刚发布", "包邮", "验货宝", "想要", "搜索"}
_NOT_REGION = {
    "包邮", "全新", "验货宝", "发货极快", "信用极好", "超赞", "降价", "个人闲置", "自提",
    "小刀", "可小刀", "同城", "附近", "想要", "急出", "国行", "正品", "保真", "刚刚发布",
}
_REGION_PATTERN = re.compile(r"^[\u4e00-\u9fa5]{2,12}$")
PROVINCES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)


@dataclass(frozen=True)
class PhoneCard:
    title: str
    price: float
    region: str
    x: int
    y: int
    item_id: str = ""


def normalize_search_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z㐀-鿿]", "", text)


def _text(node: ET.Element) -> str:
    return str(node.get("value") or node.get("label") or node.get("name") or "").strip()


def _int(node: ET.Element, key: str) -> int:
    return int(node.get(key) or 0)


def _price(node: ET.Element) -> re.Match[str] | None:
    value = _text(node)
    if node.tag == "XCUIElementTypeStaticText":
        return _PRICE.fullmatch(value)
    if node.tag == "XCUIElementTypeOther":
        return _PRICE_OTHER.fullmatch(value)
    return None


def guess_region(texts: list[str]) -> str:
    """从卡片附近的短文本中找出卖家地区，仅用于展示。"""
    candidates = [text for text in texts if _REGION_PATTERN.match(text) and text not in _NOT_REGION]
    for text in candidates:
        if text.startswith(PROVINCES) or text.endswith(("省", "市", "区", "县", "州", "盟")):
            return text
    return ""


def parse_cards(source: str) -> list[PhoneCard]:
    """把每个价格绑定到包含它的最小卡片，绝不跨卡片配对标题与价格。"""
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
        price_x, price_y = _int(price_node, "x"), _int(price_node, "y")
        current = parents.get(price_node)
        while current is not None:
            leaves = list(current.iter("XCUIElementTypeStaticText"))
            prices = [node for node in current.iter() if _price(node)]
            if len(prices) > 1:
                break
            titles = [
                node for node in leaves if node is not price_node and len(_text(node)) >= 6
                and _text(node) not in _IGNORED and not _PRICE.fullmatch(_text(node))
                and not _text(node).startswith(("¥", "￥"))
                and abs(_int(node, "x") - price_x) < 190
                and abs(_int(node, "y") - price_y) < 260
            ]
            if titles:
                title_node = max(titles, key=lambda node: len(_text(node)))
                # 地区可能是标题/价格分组的兄弟节点：只在该价格仍是唯一卡片价格时向上找。
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
                    if abs(_int(node, "x") - price_x) < 190
                    and price_y - 30 <= _int(node, "y") <= price_y + 240
                ]
                found.append(PhoneCard(
                    _text(title_node), float(price_match.group(1)),
                    guess_region(region_texts),
                    _int(title_node, "x"), _int(title_node, "y"),
                ))
                used_price_nodes.add(id(price_node))
                break
            if current.tag == "XCUIElementTypeCell":
                break
            current = parents.get(current)
    return list(dict.fromkeys(found))


def item_id_from_share(share_text: str, page_html: str) -> str:
    """只接受挂在闲鱼商品 URL 上的商品 ID，不接受追踪 ID。"""
    for text in (share_text, page_html):
        decoded = unquote(html.unescape(text).replace("\\/", "/"))
        for match in re.finditer(r"https?://(?:www\.|h5\.m\.)?goofish\.com/item[^\s\"'<>]{0,1200}", decoded):
            item_id = _ITEM_ID.search(match.group(0))
            if item_id:
                return item_id.group(1)
    return ""


def detail_mismatch_reason(source: str, card: PhoneCard) -> str | None:
    detail = ET.fromstring(source)
    expected = normalize_search_text(card.title)
    descriptions = [_text(node) for node in detail.iter("XCUIElementTypeTextView")
                    if node.get("visible") != "false" and _text(node)]
    problems = []
    if len(expected) < 6 or not any(normalize_search_text(value).startswith(expected) for value in descriptions):
        problems.append(f"标题不匹配（列表：{card.title[:70]}；详情：{descriptions[0][:70] if descriptions else '未读到正文'}）")
    prices = []
    for node in detail.iter():
        if node.get("visible") == "false" or not 100 <= _int(node, "y") < 300:
            continue
        value = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?", _text(node).replace(",", ""))
        if value:
            prices.append(float(value.group(1)))
    if card.price not in prices:
        observed = " / ".join(f"¥{value:g}" for value in dict.fromkeys(prices)) or "未读到详情价"
        problems.append(f"价格不匹配（列表 ¥{card.price:g}；详情 {observed}）")
    return "；".join(problems) or None


def detail_matches(source: str, card: PhoneCard) -> bool:
    return detail_mismatch_reason(source, card) is None


def detail_seller_name(source: str) -> str:
    """只读取详情页头像右侧的卖家昵称，不读取正文。"""
    root = ET.fromstring(source)
    visible = [node for node in root.iter() if node.get("visible") != "false"]
    if not any(_text(node) == "分享按钮" for node in visible):
        return ""
    avatar = next((node for node in visible if _text(node) == "用户头像"), None)
    if avatar is None:
        return ""
    x, y, width, height = (_int(avatar, key) for key in ("x", "y", "width", "height"))
    if width <= 0 or height <= 0:
        return ""
    names = [node for node in visible
             if node.tag in {"XCUIElementTypeOther", "XCUIElementTypeButton", "XCUIElementTypeStaticText"}
             and _text(node) and _text(node) != "鱼小铺"
             and 0 <= _int(node, "x") - (x + width) <= 28
             and abs(_int(node, "y") + _int(node, "height") / 2 - (y + height / 2)) <= height / 2
             and 0 < _int(node, "height") <= height * 1.5]
    if not names:
        return ""
    return _text(min(names, key=lambda node: (_int(node, "x"), _int(node, "width"))))


def detail_content(source: str) -> dict[str, Any]:
    """读取详情页完整正文与商品参数。"""
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
    return {"description": "\n\n".join(descriptions), "parameters": list(dict.fromkeys(parameters))}


def has_result_sort_bar(source: str) -> bool:
    """排序栏在滚动后可能不可见，但「新发」与「筛选」仍在同一行。"""
    nodes = list(ET.fromstring(source).iter("XCUIElementTypeStaticText"))
    return any(abs(_int(new, "y") - _int(filters, "y")) <= 5
               for new in nodes if _text(new) == "新发"
               for filters in nodes if _text(filters) == "筛选")


def is_search_results(source: str) -> bool:
    if not has_result_sort_bar(source):
        return False
    root = ET.fromstring(source)
    visible = [node for node in root.iter() if node.get("visible") == "true"]
    if any((node.get("name") or node.get("label")) in {"分享按钮", "返回按钮", "确定"}
           for node in visible):
        return False
    return any(node.tag == "XCUIElementTypeButton"
               and (node.get("name") or node.get("label")) in {"search more", "图片搜索"}
               and _int(node, "y") < 180 for node in visible)


def home_tabs(source: str) -> list[ET.Element]:
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    height = _int(app, "height")
    if not height:
        return []
    tabs = sorted((node for node in root.iter("XCUIElementTypeButton")
                   if node.get("visible") == "true" and _text(node)
                   and _int(node, "y") >= height * 0.85
                   and 25 <= _int(node, "width") <= 130),
                  key=lambda node: _int(node, "x"))
    return tabs if len(tabs) >= 4 and (tabs[0].get("label") or tabs[0].get("name")) == "首页" else []


def search_entry_points(source: str) -> list[dict[str, Any]]:
    """只点击搜索栏内的文字，绝不点击历史记录或商品。"""
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    width, height = _int(app, "width"), _int(app, "height")
    if not width or not height:
        return []
    visible = [node for node in root.iter() if node.get("visible") == "true"]
    points: list[dict[str, Any]] = []

    def point(node: ET.Element, method: str) -> dict[str, Any]:
        return {"target": str(node.get("name") or node.get("label") or "搜索栏"), "method": method,
                "x": _int(node, "x") + _int(node, "width") // 2,
                "y": _int(node, "y") + _int(node, "height") // 2}

    if home_tabs(source):
        bars = [node for node in visible if node.tag == "XCUIElementTypeButton"
                and _int(node, "width") >= width * 0.55
                and 30 <= _int(node, "y") < height * 0.22
                and ("SearchField" in (node.get("traits") or "")
                     or any(child.tag == "XCUIElementTypeImage" and child.get("name") == "搜索"
                            and abs(_int(child, "y") - _int(node, "y")) < 30
                            for child in visible))]
        if not bars:
            return []
        bar = min(bars, key=lambda node: _int(node, "y"))
        name = bar.get("name") or bar.get("label")
        texts = [node for node in visible if node.tag == "XCUIElementTypeStaticText"
                 and len(_text(node)) >= 2 and _text(node) not in {"搜索", "Search", "拍照", "扫一扫"}
                 and _int(bar, "x") <= _int(node, "x")
                 and _int(node, "x") + _int(node, "width") <= _int(bar, "x") + _int(bar, "width")
                 and abs(_int(node, "y") - _int(bar, "y")) < 25]
        if texts:
            points.append(point(min(texts, key=lambda node: (
                (node.get("name") or node.get("label")) != name,
                abs(_int(node, "y") - _int(bar, "y")), _int(node, "x"))), "轻点搜索栏关键词文字中心"))
        points.append({"target": str(name or "搜索栏"), "method": "轻点搜索栏文字区域",
                       "x": _int(bar, "x") + _int(bar, "width") // 4,
                       "y": _int(bar, "y") + _int(bar, "height") // 2})
    elif has_result_sort_bar(source):
        images = [node for node in visible if node.get("name") == "图片搜索"]
        if not images:
            return []
        image = min(images, key=lambda node: _int(node, "y"))
        texts = [node for node in visible if node.tag == "XCUIElementTypeStaticText"
                 and _int(node, "x") >= 35
                 and _int(node, "x") + _int(node, "width") < _int(image, "x")
                 and abs(_int(node, "y") - _int(image, "y")) < 20
                 and len(_text(node)) >= 3]
        if texts:
            points.append(point(min(texts, key=lambda node: _int(node, "x")), "轻点结果页搜索词"))
    return points


def has_visible_alert(source: str) -> bool:
    return any(node.get("visible") == "true" for node in ET.fromstring(source).iter("XCUIElementTypeAlert"))


_VERIFICATION_PROMPTS = {"亲，请按照说明进行验证哦", "请拖动下方滑块还原拼图", "拖动左侧滑块完成上方拼图"}


def verification_required(source: str) -> bool:
    """闲鱼的滑块/拼图安全验证页；商品正文（TextView）里的同样文字不算。"""
    texts: set[str] = set()

    def walk(node: ET.Element) -> None:
        if node.get("visible") == "false":
            return
        if node.tag in {"XCUIElementTypeOther", "XCUIElementTypeStaticText", "XCUIElementTypeWebView"}:
            texts.update(node.get(key) or "" for key in ("name", "label", "value"))
        if node.tag != "XCUIElementTypeTextView":
            for child in node:
                walk(child)

    walk(ET.fromstring(source))
    return "验证码拦截" in texts or len(texts & _VERIFICATION_PROMPTS) >= 2


def detail_back_rect(source: str) -> dict[str, int] | None:
    """深链打开的另一种原生详情布局：顶部返回控件没有名称，需由底栏、更多菜单与搜索区共同确认。"""
    root = ET.fromstring(source)
    app = next(root.iter("XCUIElementTypeApplication"), root)
    width, height = _int(app, "width"), _int(app, "height")
    if not width or not height:
        return None
    visible: list[ET.Element] = []

    def walk(node: ET.Element) -> None:
        if node.get("visible") == "false":
            return
        if node.get("visible") == "true":
            visible.append(node)
        for child in node:
            walk(child)

    walk(app)
    # 弹窗、分享面板等覆盖层上绝不当作详情导航栏点击
    if any(node.tag == "XCUIElementTypeAlert" or _text(node) in {"确定", "复制链接"} for node in visible):
        return None
    footer = {_text(node) for node in visible if height * .85 <= _int(node, "y") < height}
    if not {"留言按钮", "收藏"} <= footer:
        return None
    more = {tuple(_int(node, key) for key in ("x", "y", "width", "height"))
            for node in visible if _text(node) == "更多"
            and _int(node, "x") > width * .8 and 30 <= _int(node, "y") < 120}
    if len(more) != 1:
        return None
    top = next(iter(more))[1]
    bars = [node for node in visible if node.tag == "XCUIElementTypeOther"
            and 35 <= _int(node, "x") < width * .25
            and _int(node, "width") >= width * .5
            and abs(_int(node, "y") - top) <= 12
            and 24 <= _int(node, "height") <= 60]
    if not bars:
        return None
    candidates = set()
    for node in visible:
        x, y, w, h = (_int(node, key) for key in ("x", "y", "width", "height"))
        if (node.tag == "XCUIElementTypeOther" and _text(node) in {"", "返回", "返回按钮"}
                and 0 <= x <= 12 and abs(y - top) <= 5 and 24 <= w <= 64 and 24 <= h <= 64
                and x + w <= _int(bars[0], "x") and y + h <= height):
            candidates.add((x, y, w, h))
    if len(candidates) != 1:
        return None
    return dict(zip(("x", "y", "width", "height"), candidates.pop()))
