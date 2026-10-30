"""代理采集：仍由正式 App 发起搜索与详情请求，商品数据取自手机 Wi-Fi 代理转发的白名单字段。

流程沿用 app-monitor 真机验证过的路径：搜索 → 点「新发」并以代理返回确认发布时间降序 →
受控拖动列表读取后续批次 → 候选按商品 ID 深链打开详情，正文取代理返回、售价以页面显示为准 → 返回列表。
相比界面采集，不需要分享复制链接，且能拿到商品图片与发布时间。
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Callable

from src.infrastructure.external.iphone.devices import IPhoneDevice
from src.infrastructure.external.iphone.page_source import (
    PhoneCard,
    _text,
    detail_back_rect,
    detail_content,
    detail_seller_name,
    is_search_results,
    normalize_search_text,
)
from src.infrastructure.external.iphone.proxy.proxy_protocol import price as parse_price
from src.infrastructure.external.iphone.proxy.subscriber import ProxySubscriber
from src.infrastructure.external.iphone.search_client import (
    IPhoneListing,
    IPhoneSearchClient,
    IPhoneSearchSummary,
    IPhoneStopped,
    IPhoneVerificationRequired,
    _brief_error,
    raise_if_verification,
)
from src.infrastructure.external.iphone.settings import IPhoneSettings

SEARCH_TIMEOUT_SECONDS = 18
PAGE_TIMEOUT_SECONDS = 1.5
DETAIL_TIMEOUT_SECONDS = 15


def checked(events: list[dict]) -> list[dict]:
    if any(event.get("event") == "verification" for event in events):
        raise IPhoneVerificationRequired("闲鱼要求验证或限制请求，请在手机处理后重试")
    return events


def newest_search(events: list[dict]) -> dict:
    # 返回到达顺序不代表界面最后一次请求
    return max(events, key=lambda event: (event.get("request_started", 0), event.get("sequence", 0)))


def is_new_sort(event: dict) -> bool:
    return event.get("sort_field") == "create" and event.get("sort_value") == "desc"


def sort_label(event: dict) -> str:
    return f"{event.get('sort_field') or '未提供'} / {event.get('sort_value') or '未提供'}"


def _is_detail_page(visible: list[ET.Element], source: str) -> bool:
    return bool({_text(node) for node in visible} & {"分享按钮", "返回按钮"}) or detail_back_rect(source) is not None


def detail_view(source: str, description: str, expected_price: float | None = None) -> dict:
    """把代理返回的完整正文绑定到当前详情页（两种原生布局），再读取页面显示售价。"""
    root = ET.fromstring(source)
    visible = [node for node in root.iter() if node.get("visible") != "false"]
    if not _is_detail_page(visible, source) or "确定" in {_text(node) for node in visible}:
        raise ValueError("尚未进入商品详情页")
    rendered = [normalize_search_text(_text(node)) for node in visible
                if node.tag in {"XCUIElementTypeTextView", "XCUIElementTypeStaticText", "XCUIElementTypeOther"}]
    expected = normalize_search_text(description)
    body_matched = bool(expected and (any(expected in value for value in rendered) or expected in "".join(rendered)))
    avatar = next((node for node in visible if "用户头像" in {node.get("name"), node.get("label")}), None)
    top = max(100, float(avatar.get("y") or 0) + float(avatar.get("height") or 0)) if avatar is not None else 100
    amounts = {amount for node in visible if node.tag in {"XCUIElementTypeStaticText", "XCUIElementTypeOther"}
               and top <= float(node.get("y") or 0) < max(300, top + 180)
               and (amount := parse_price(_text(node).replace(",", ""))) is not None}
    if len(amounts) != 1:
        raise ValueError("详情售价未就绪或存在多个数值")
    amount = amounts.pop()
    if not body_matched and (expected_price is None or amount != expected_price):
        raise ValueError("当前页面尚未对应代理正文或售价")
    return {"price": amount, "body_matched": body_matched}


def detail_below_fold(source: str) -> tuple[float, float] | None:
    """大图布局把售价与正文放在首屏下半部，返回可受控下滑的窗口尺寸。"""
    root = ET.fromstring(source)
    app = root if root.tag == "XCUIElementTypeApplication" else root.find(".//XCUIElementTypeApplication")
    width, height = (float(app.get(key) or 0) for key in ("width", "height")) if app is not None else (0, 0)
    if not width or not height:
        return None
    visible = [node for node in root.iter() if node.get("visible") != "false"]
    if not _is_detail_page(visible, source):
        return None
    low_price = any(node.tag in {"XCUIElementTypeStaticText", "XCUIElementTypeOther"}
                    and height * .5 < float(node.get("y") or 0) < height * .9
                    and parse_price(_text(node)) is not None for node in visible)
    return (width, height) if low_price else None


def local_publish_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


class IPhoneProxySearchClient(IPhoneSearchClient):
    def __init__(
        self,
        settings: IPhoneSettings,
        device: IPhoneDevice,
        *,
        client_ip: str,
        proxy: Any | None = None,
        **kwargs,
    ):
        super().__init__(settings, device, **kwargs)
        self.client_ip = client_ip
        self.proxy = proxy or ProxySubscriber(settings.proxy_socket_path, check_stop=self._check_stop)

    def search(self, keyword: str, screens: int, *args, **kwargs) -> IPhoneSearchSummary:
        with self.proxy.session(self.client_ip):
            self.proxy.wait_ready()
            return super().search(keyword, screens, *args, **kwargs)

    def _sort_new(self, driver: Any) -> None:
        # 以代理返回确认排序，不做固定等待
        button = self._find(driver, "新发", ("XCUIElementTypeStaticText",))
        if button is None:
            raise RuntimeError("找不到「新发」排序按钮")
        button.click()

    def _drag_list(self, driver: Any) -> None:
        source = driver.page_source
        raise_if_verification(source)
        if not is_search_results(source):
            raise RuntimeError("代理翻页前未确认搜索列表")
        size = driver.get_window_size()
        driver.execute_script("mobile: dragFromToForDuration", {
            "duration": 0.35, "fromX": size["width"] * .48, "fromY": size["height"] * .84,
            "toX": size["width"] * .48, "toY": size["height"] * .3,
        })

    def _pages(
        self, driver: Any, keyword: str, drags: int, actions: list[dict[str, Any]], verification_since: float,
    ) -> tuple[list[dict], int]:
        since = time.time()
        self._sort_new(driver)
        actions.append({"action": "点击排序", "target": "新发", "success": True})
        events = checked(self.proxy.wait(since, keyword=keyword, verification_since=verification_since,
                                         timeout=SEARCH_TIMEOUT_SECONDS))
        if not events:
            raise_if_verification(driver.page_source)
            raise RuntimeError("未收到本次搜索的代理数据；请检查手机 Wi-Fi 代理、证书与设备 IP")
        first = newest_search(events)
        if not is_new_sort(first):
            actual = sort_label(first)
            if first.get("sort_field") == "create" and first.get("sort_value") == "asc":
                # 已选中的「新发」再点一次会反转方向，只补点一次
                since = time.time()
                self._sort_new(driver)
                actions.append({"action": "纠正新发排序", "target": actual, "success": True})
            else:
                since = max(since, first.get("request_started", since))
            events = checked(self.proxy.wait(since, keyword=keyword, sort_field="create", sort_value="desc",
                                             verification_since=verification_since, timeout=SEARCH_TIMEOUT_SECONDS))
            if not events or not is_new_sort(newest_search(events)):
                raise_if_verification(driver.page_source)
                observed = sort_label(newest_search(events)) if events else actual
                raise RuntimeError(f"代理响应未确认新发排序（实际 {observed}），停止采集")
            first = newest_search(events)
        fingerprint = first["filter_fingerprint"]
        batches = [event for event in events if is_new_sort(event) and event.get("filter_fingerprint") == fingerprint]
        actions.append({"action": "确认新发排序", "target": "create / desc", "success": True})
        last_sequence = max(event["sequence"] for event in batches)
        for _ in range(max(0, drags)):
            self._check_stop()
            if batches[-1].get("has_more") is False:
                break
            self._drag_list(driver)
            more = checked(self.proxy.wait(since, keyword=keyword, fingerprint=fingerprint,
                                           sort_field="create", sort_value="desc", after_sequence=last_sequence,
                                           verification_since=verification_since, timeout=PAGE_TIMEOUT_SECONDS))
            if more:
                batches.extend(more)
                last_sequence = max(event["sequence"] for event in more)
        raise_if_verification(driver.page_source)
        items: dict[str, dict] = {}
        for batch in batches:
            for item in batch.get("items", []):
                items.setdefault(item["item_id"], item)
        return list(items.values()), len(batches)

    def _collect(
        self,
        driver: Any,
        summary: IPhoneSearchSummary,
        keyword: str,
        screens: int,
        is_candidate: Callable[[PhoneCard], bool],
        on_listing: Callable[[IPhoneListing], None],
        on_card: Callable[[PhoneCard], None] | None,
        capture_screenshot: bool,
    ) -> None:
        actions = summary.actions
        verification_since = time.time()
        self._search(driver, keyword, actions)
        raws, batches = self._pages(driver, keyword, max(1, screens) - 1, actions, verification_since)
        self.log(f"LOG: [iPhone·代理] 收到 {batches} 批列表，共 {len(raws)} 件商品")
        for raw in raws:
            self._check_stop()
            card = PhoneCard(title=raw["title"], price=raw["price"], region=raw.get("display_region") or "",
                             x=0, y=0, item_id=raw["item_id"])
            summary.cards_seen += 1
            if on_card is not None:
                on_card(card)
            if not is_candidate(card):
                continue
            summary.candidates += 1
            try:
                listing = self._open_detail(driver, raw, card, actions, capture_screenshot)
            except (IPhoneStopped, IPhoneVerificationRequired):
                raise
            except Exception as exc:
                error = _brief_error(exc)
                summary.warnings.append(f"{card.title[:30]}：{error}")
                actions.append({"action": "读取代理详情", "target": raw["item_id"], "success": False, "error": error})
                self.log(f"LOG: [iPhone·代理] 候选处理失败 '{card.title[:30]}': {error}")
                if not is_search_results(driver.page_source):
                    raise RuntimeError("候选处理失败后未回到搜索列表，停止本次采集") from exc
                continue
            summary.linked += 1
            on_listing(listing)

    def _wait_for_source(self, driver: Any, ready: Callable[[str], bool], failure: str, timeout: float = 8) -> str:
        deadline = time.monotonic() + timeout
        while True:
            self._check_stop()
            source = driver.page_source
            try:
                raise_if_verification(source)
                if ready(source):
                    return source
            except ET.ParseError:
                pass  # 转场期间的空快照不作为成功依据
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{failure}（等待 {timeout:g} 秒超时）")
            time.sleep(0.15)

    def _open_detail(
        self, driver: Any, raw: dict, card: PhoneCard, actions: list[dict[str, Any]], capture_screenshot: bool,
    ) -> IPhoneListing:
        target, since = raw["item_id"], time.time()
        self._ensure_foreground(driver, actions)
        driver.execute_script("mobile: deepLink", {"url": f"fleamarket://item?id={target}",
                                                  "bundleId": self.settings.goofish_bundle_id})
        actions.append({"action": "打开候选详情", "target": target, "method": "商品 ID", "success": True})
        operation_error: Exception | None = None
        try:
            return self._read_detail(driver, raw, card, target, since, actions, capture_screenshot)
        except Exception as exc:
            operation_error = exc
            raise
        finally:
            if not isinstance(operation_error, (IPhoneVerificationRequired, IPhoneStopped)):
                try:
                    self._return_to_results(driver, actions)
                except Exception as return_error:
                    if operation_error is not None:
                        raise RuntimeError(f"{_brief_error(operation_error)}；"
                                           f"返回列表失败：{_brief_error(return_error)}") from operation_error
                    raise

    def _read_detail(
        self, driver: Any, raw: dict, card: PhoneCard, target: str, since: float,
        actions: list[dict[str, Any]], capture_screenshot: bool,
    ) -> IPhoneListing:
        events = checked(self.proxy.wait(since, kind="detail", target=target, timeout=DETAIL_TIMEOUT_SECONDS))
        if not events:
            raise_if_verification(driver.page_source)
            raise RuntimeError("未收到该商品本次详情返回，未使用列表摘要或旧缓存代替")
        detail = events[-1].get("item") or {}
        if detail.get("item_id") != target or not detail.get("description"):
            raise RuntimeError("代理详情缺少对应商品的完整正文")
        observed: dict = {}

        def ready(xml: str) -> bool:
            try:
                observed.update(detail_view(xml, detail["description"], detail.get("price")))
                return True
            except (ValueError, ET.ParseError):
                return False

        source = driver.page_source
        raise_if_verification(source)
        for _ in range(2):
            if ready(source):
                break
            bounds = detail_below_fold(source)
            if bounds is None:
                break
            width, height = bounds
            driver.execute_script("mobile: dragFromToForDuration", {
                "duration": .35, "fromX": width * .5, "fromY": height * .82, "toX": width * .5, "toY": height * .34,
            })
            actions.append({"action": "展开详情正文", "method": "大图布局，按售价位置受控下滑", "success": True})
            source = driver.page_source
            raise_if_verification(source)
        if not observed:
            source = self._wait_for_source(driver, ready, "代理详情尚未对应当前页面正文与售价")
        parameters = list(detail.get("detail_parameters") or [])
        seller_name = str(raw.get("seller_name") or "")
        if observed["body_matched"]:
            parameters = list(dict.fromkeys(parameters + detail_content(source)["parameters"]))
            seller_name = detail_seller_name(source) or seller_name
        images = tuple(dict.fromkeys(list(detail.get("image_urls") or []) + list(raw.get("image_urls") or [])))
        screenshot = None
        if capture_screenshot and not images:
            try:
                screenshot = driver.get_screenshot_as_png()
            except Exception as exc:
                self.log(f"LOG: [iPhone·代理] 详情截图失败，AI 将只分析文字: {_brief_error(exc)}")
        actions.append({"action": "读取代理详情", "target": target, "success": True})
        # 页面显示的售价最可靠：原生售价布局仍有多种
        displayed = PhoneCard(title=card.title, price=observed["price"], region=card.region,
                              x=0, y=0, item_id=target)
        return IPhoneListing(
            card=displayed, item_id=target, description=detail["description"], parameters=parameters,
            seller_name=seller_name, screenshot_png=screenshot, image_urls=images,
            publish_time=local_publish_time(raw.get("publish_time")), channel="proxy",
        )
