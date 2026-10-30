"""在已登录的 iPhone 闲鱼 App 中搜索：读取列表卡片，只为候选商品进入详情并复制分享链接。

流程沿用 app-monitor 已在真机验证的操作路径：搜索关键词 → 「新发」排序 → 逐屏读取卡片 →
候选进详情核对标题与价格 → 复制分享链接取得商品 ID → 返回列表。
任何一步无法确认页面状态时立即停止，避免误点商品或系统界面。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from src.infrastructure.external.iphone.appium_server import ensure_appium_server
from src.infrastructure.external.iphone.devices import IPhoneDevice, prepare_pasteboard, read_pasteboard
from src.infrastructure.external.iphone.page_source import (
    PhoneCard,
    detail_back_rect,
    detail_content,
    detail_matches,
    detail_mismatch_reason,
    detail_seller_name,
    has_visible_alert,
    is_search_results,
    item_id_from_share,
    parse_cards,
    search_entry_points,
    verification_required,
)
from src.infrastructure.external.iphone.settings import IPhoneSettings
from src.infrastructure.external.iphone.wda_signing import ensure_wda_signed

_URL = re.compile(r"https?://[^\s<>\"']+")
_SHARE_HOSTS = {"m.tb.cn", "www.goofish.com", "h5.m.goofish.com"}
DEFAULT_MJPEG_SERVER_PORT = 9100


class IPhoneStopped(RuntimeError):
    """任务被停止或已达到处理上限。"""


class IPhoneVerificationRequired(RuntimeError):
    """闲鱼要求安全验证；继续点击只会加重风控，必须停下等人处理。"""


def raise_if_verification(source: str) -> None:
    if verification_required(source):
        raise IPhoneVerificationRequired("闲鱼要求安全验证，请在手机完成验证后重试")


@dataclass(frozen=True)
class IPhoneListing:
    card: PhoneCard
    item_id: str
    description: str
    parameters: list[str]
    seller_name: str
    screenshot_png: bytes | None = None
    image_urls: tuple[str, ...] = ()
    publish_time: str = ""
    channel: str = "ui"


@dataclass
class IPhoneSearchSummary:
    device_name: str
    cards_seen: int = 0
    candidates: int = 0
    linked: int = 0
    stopped: bool = False
    actions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _brief_error(exc: Exception) -> str:
    return (str(exc).splitlines()[0] if str(exc) else type(exc).__name__)[:400]


class IPhoneSearchClient:
    def __init__(
        self,
        settings: IPhoneSettings,
        device: IPhoneDevice,
        *,
        stop_event: threading.Event | None = None,
        log: Callable[[str], None] = print,
        port_offset: int = 0,
    ):
        self.settings = settings
        self.device = device
        self.port_offset = port_offset
        self.stop_event = stop_event or threading.Event()
        self.log = log

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise IPhoneStopped("任务已停止")

    def _capabilities(self) -> dict[str, Any]:
        capabilities = {
            "platformName": "iOS", "appium:automationName": "XCUITest",
            "appium:udid": self.device.udid, "appium:bundleId": self.settings.goofish_bundle_id,
            "appium:noReset": True, "appium:shouldTerminateApp": False,
            "appium:usePrebuiltWDA": True,
            "appium:updatedWDABundleId": self.settings.wda_bundle_id,
            "appium:wdaLocalPort": self.settings.wda_local_port + self.port_offset,
            "appium:newCommandTimeout": 300,
        }
        if self.port_offset:
            # 多台手机同时运行时，WDA 与 MJPEG 的本机转发端口不能重复
            capabilities["appium:mjpegServerPort"] = DEFAULT_MJPEG_SERVER_PORT + self.port_offset
        if self.settings.wda_derived_data_path is not None:
            capabilities["appium:derivedDataPath"] = str(self.settings.wda_derived_data_path)
        if self.device.os_version:
            capabilities["appium:platformVersion"] = self.device.os_version
        return capabilities

    def _connect(self) -> Any:
        if not self.settings.wda_bundle_id:
            raise RuntimeError("未配置 IOS_WDA_BUNDLE_ID；请填写已签名的 WebDriverAgent bundle ID")
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ImportError as exc:
            raise RuntimeError("未安装 Appium-Python-Client：请运行 pip install -r requirements.txt") from exc
        ensure_wda_signed(self.settings, self.device.udid, log=self.log)
        ensure_appium_server(self.settings)
        try:
            return webdriver.Remote(
                self.settings.appium_server_url,
                options=XCUITestOptions().load_capabilities(self._capabilities()),
            )
        except Exception as exc:
            raise RuntimeError(f"iPhone Appium 连接失败（检查服务、WDA 和 Inspector 会话）：{_brief_error(exc)}") from exc

    def search(
        self,
        keyword: str,
        screens: int,
        is_candidate: Callable[[PhoneCard], bool],
        on_listing: Callable[[IPhoneListing], None],
        *,
        on_card: Callable[[PhoneCard], None] | None = None,
        capture_screenshot: bool = False,
    ) -> IPhoneSearchSummary:
        summary = IPhoneSearchSummary(device_name=self.device.name)
        driver = self._connect()
        try:
            self._collect(driver, summary, keyword, screens, is_candidate, on_listing,
                          on_card, capture_screenshot)
        except IPhoneStopped:
            summary.stopped = True
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        return summary

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
        self._search(driver, keyword, actions)
        self._sort_new(driver)
        actions.append({"action": "点击排序", "target": "新发", "success": True})
        seen: set[tuple[str, float, str]] = set()
        total = max(1, screens)
        for page in range(total):
            self._check_stop()
            if page:
                driver.execute_script("mobile: swipe", {"direction": "up", "velocity": 900})
                time.sleep(1)
            source = driver.page_source
            raise_if_verification(source)
            cards = parse_cards(source)
            self.log(f"LOG: [iPhone] 第 {page + 1}/{total} 屏读到 {len(cards)} 张卡片")
            for card in cards:
                self._check_stop()
                key = (card.title, card.price, card.region)
                if key in seen:
                    continue
                seen.add(key)
                summary.cards_seen += 1
                if on_card is not None:
                    on_card(card)
                if not is_candidate(card):
                    continue
                summary.candidates += 1
                try:
                    listing = self._open_listing(driver, card, actions, capture_screenshot)
                except (IPhoneStopped, IPhoneVerificationRequired):
                    raise
                except Exception as exc:
                    error = _brief_error(exc)
                    summary.warnings.append(f"{card.title[:30]}：{error}")
                    actions.append({"action": "复制候选链接", "target": card.title[:40],
                                    "success": False, "error": error})
                    self.log(f"LOG: [iPhone] 候选处理失败 '{card.title[:30]}': {error}")
                    if not is_search_results(driver.page_source):
                        raise RuntimeError("候选处理失败后未回到搜索列表，停止本次采集") from exc
                    continue
                summary.linked += 1
                on_listing(listing)

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

    def _ensure_foreground(self, driver: Any, actions: list[dict[str, Any]]) -> None:
        bundle_id = self.settings.goofish_bundle_id
        for attempt in range(3):
            info = driver.execute_script("mobile: activeAppInfo")
            if info.get("bundleId") == bundle_id:
                return
            if attempt == 0:
                driver.activate_app(bundle_id)
                actions.append({"action": "恢复闲鱼前台", "success": True,
                                "target": str(info.get("bundleId") or "未知前台应用")})
            time.sleep(0.4)
        raise RuntimeError("闲鱼未恢复到前台，停止操作以免点击系统界面")

    @staticmethod
    def _search_input(driver: Any) -> Any | None:
        from appium.webdriver.common.appiumby import AppiumBy

        fields = driver.find_elements(AppiumBy.IOS_PREDICATE,
                                      "type IN {'XCUIElementTypeTextField', 'XCUIElementTypeSearchField'}")
        return next((item for item in fields if item.is_displayed()
                     and item.rect["y"] < 200 and item.rect["width"] > 100), None)

    def _open_search_input(self, driver: Any, actions: list[dict[str, Any]]) -> Any:
        for attempt in range(2):
            self._ensure_foreground(driver, actions)
            search_field = self._search_input(driver)
            if search_field is not None:
                return search_field
            source = driver.page_source
            if has_visible_alert(source):
                raise RuntimeError("系统弹窗阻挡搜索，停止点击；请处理弹窗后重试")
            points = search_entry_points(source)
            if not points:
                raise RuntimeError("当前页面没有可确认的搜索入口，已停止以免点击历史记录或商品")
            target = points[min(attempt, len(points) - 1)]
            driver.execute_script("mobile: tap", {"x": target["x"], "y": target["y"]})
            actions.append({"action": "点击搜索入口", "target": target["target"], "success": True,
                            "method": f"{target['method']} ({target['x']}, {target['y']})，第{attempt + 1}次"})
            for _ in range(8):
                search_field = self._search_input(driver)
                if search_field is not None:
                    return search_field
                time.sleep(0.3)
        raise RuntimeError("点击搜索入口并重新定位重试后仍未出现输入框")

    def _search(self, driver: Any, keyword: str, actions: list[dict[str, Any]]) -> None:
        driver.activate_app(self.settings.goofish_bundle_id)
        self._ensure_foreground(driver, actions)
        self._close_share_sheet(driver)
        # 上次停在商品详情时先退回，搜索入口只在首页或结果页上找。
        for _ in range(3):
            if not self._find(driver, "分享按钮"):
                break
            back = self._find(driver, "返回按钮")
            if back:
                back.click()
            else:
                driver.back()
        search_field = self._open_search_input(driver, actions)
        search_field.click()
        search_field.clear()
        search_field.send_keys(keyword)
        if str(search_field.get_attribute("value") or "").strip() != keyword:
            raise RuntimeError("搜索词输入后回读不一致，已停止提交")
        button = None
        for _ in range(8):
            button = (self._find(driver, "Search", ("XCUIElementTypeButton",))
                      or self._find(driver, "搜索", ("XCUIElementTypeButton",)))
            if button:
                break
            time.sleep(0.3)
        if button is None:
            raise RuntimeError("找不到键盘搜索按钮")
        button.click()
        actions.append({"action": "搜索", "target": keyword, "success": True})
        for _ in range(10):
            if self._find(driver, "新发", ("XCUIElementTypeStaticText",)):
                return
            time.sleep(0.5)
        raise RuntimeError("提交搜索后没有出现结果排序栏")

    def _sort_new(self, driver: Any) -> None:
        button = self._find(driver, "新发", ("XCUIElementTypeStaticText",))
        if button is None:
            raise RuntimeError("找不到「新发」排序按钮")
        button.click()
        time.sleep(0.8)

    def _return_to_results(self, driver: Any, actions: list[dict[str, Any]]) -> None:
        self._ensure_foreground(driver, actions)
        self._close_share_sheet(driver)
        clicked_back = False
        for _ in range(8):
            source = driver.page_source
            raise_if_verification(source)
            if is_search_results(source):
                return
            if not clicked_back:
                back = self._find(driver, "返回按钮") if self._find(driver, "分享按钮") else None
                if back:
                    back.click()
                    clicked_back = True
                else:
                    rect = detail_back_rect(source)
                    if rect is not None:
                        driver.execute_script("mobile: tap", {"x": rect["x"] + rect["width"] // 2,
                                                             "y": rect["y"] + rect["height"] // 2})
                        clicked_back = True
                        actions.append({"action": "详情返回", "target": "详情顶部返回容器", "success": True})
            time.sleep(0.5)
        reason = "点击详情返回后未到搜索结果页" if clicked_back else "未识别到可安全点击的详情返回入口"
        raise RuntimeError(f"{reason}，停止继续采集")

    def _open_listing(
        self,
        driver: Any,
        card: PhoneCard,
        actions: list[dict[str, Any]],
        capture_screenshot: bool,
    ) -> IPhoneListing:
        from appium.webdriver.common.appiumby import AppiumBy

        title = next((el for el in driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeStaticText")
                      if el.is_displayed() and str(el.get_attribute("name") or "").strip() == card.title
                      and abs(el.rect["y"] - card.y) < 100), None)
        if title is None:
            raise RuntimeError("候选卡片已离开当前屏，跳过以免点错商品")
        self._ensure_foreground(driver, actions)
        title.click()
        operation_error: Exception | None = None
        try:
            share = None
            for _ in range(8):
                share = self._find(driver, "分享按钮")
                if share:
                    break
                time.sleep(0.4)
            if share is None:
                raise RuntimeError("进入详情后找不到分享按钮")
            source = driver.page_source
            for attempt in range(6):
                source = driver.page_source
                if detail_matches(source, card):
                    break
                if attempt < 5:
                    time.sleep(0.35)
            else:
                reason = detail_mismatch_reason(source, card) or "详情身份校验未通过"
                raise RuntimeError(f"详情身份校验失败：{reason}；已跳过复制")
            content = detail_content(source)
            seller_name = detail_seller_name(source)
            for _ in range(2):
                if seller_name:
                    break
                time.sleep(0.3)
                source = driver.page_source
                if not detail_matches(source, card):
                    raise RuntimeError("读取卖家信息时详情页发生变化，已跳过复制")
                seller_name = detail_seller_name(source)
            screenshot = None
            if capture_screenshot:
                try:
                    screenshot = driver.get_screenshot_as_png()
                except Exception as exc:
                    self.log(f"LOG: [iPhone] 详情截图失败，AI 将只分析文字: {_brief_error(exc)}")
            item_id = self._copy_item_id(driver, share)
            actions.append({"action": "取得商品链接", "target": f"ID {item_id}", "success": True})
            return IPhoneListing(
                card=card, item_id=item_id, description=content["description"],
                parameters=content["parameters"], seller_name=seller_name, screenshot_png=screenshot,
            )
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

    def _copy_item_id(self, driver: Any, share: Any) -> str:
        from appium.webdriver.common.appiumby import AppiumBy

        share.click()
        copy = None
        for _ in range(10):
            choices = driver.find_elements(AppiumBy.IOS_PREDICATE, "name == '复制链接' OR label == '复制链接'")
            copy = next((element for element in choices if element.rect["width"] > 20), None)
            if copy:
                break
            time.sleep(0.3)
        if copy is None:
            raise RuntimeError("分享菜单没有复制链接")
        udid = self.device.udid
        marker = prepare_pasteboard(udid)
        # 分享面板图标在页面树里被标为不可见，按定位到的坐标轻点。
        rect = copy.rect
        driver.execute_script("mobile: tap", {
            "x": int(rect["x"] + rect["width"] / 2),
            "y": int(rect["y"] + rect["height"] / 2),
        })
        time.sleep(0.4)
        copied = ""
        for _ in range(4):
            copied = read_pasteboard(udid)
            if copied and marker not in copied:
                break
            time.sleep(0.3)
        if not copied or marker in copied:
            raise RuntimeError("手机剪贴板未产生新链接")
        share_urls = [url.rstrip("，。)）") for url in _URL.findall(copied)
                      if urlparse(url).hostname in _SHARE_HOSTS]
        if not share_urls:
            raise RuntimeError("复制内容中没有闲鱼分享链接")
        direct = item_id_from_share(copied, "")
        if direct:
            return direct
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            response = client.get(share_urls[0])
            response.raise_for_status()
        item_id = item_id_from_share(copied, response.text)
        if not item_id:
            raise RuntimeError("短链页面中找不到商品 ID")
        return item_id
