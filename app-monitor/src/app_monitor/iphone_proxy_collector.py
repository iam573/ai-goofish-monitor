"""App-triggered searches/details, proxy product data; UI collector stays independent."""
from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET

from .appium_server import WDA_PORT, ensure_appium_server
from .collector import CollectResult, emit_safely
from .iphone_collector import (IPhoneCollector, _VerificationRequired, _raise_if_verification,
                               _brief_error, _quit_driver, _text, detail_back_rect, detail_content, detail_seller, is_search_results)
from .matching import normalize_search_text
from .instance_lock import exclusive_device


def checked(events):
    if any(event.get("event") == "verification" for event in events):
        raise _VerificationRequired("闲鱼要求验证或限制请求，请在手机处理后重试")
    return events


def newest_search(events):
    return max(events, key=lambda event: (event.get("request_started", 0), event.get("sequence", 0)))


def is_new_sort(event):
    return event.get("sort_field") == "create" and event.get("sort_value") == "desc"


def sort_label(event):
    return f"{event.get('sort_field') or '未提供'} / {event.get('sort_value') or '未提供'}"


def detail_view(source, description, expected_price=None):
    """Bind a complete network body to either native text layout before reading price."""
    root = ET.fromstring(source)
    visible = [node for node in root.iter() if node.get("visible") != "false"]
    controls = {_text(node) for node in visible}
    if (not controls.intersection({"分享按钮", "返回按钮"}) and detail_back_rect(source) is None
            or "确定" in controls):
        raise ValueError("尚未进入商品详情页")
    rendered = [normalize_search_text(_text(node)) for node in visible
                if node.tag in {"XCUIElementTypeTextView", "XCUIElementTypeStaticText", "XCUIElementTypeOther"}]
    expected = normalize_search_text(description)
    body_matched = bool(expected and (any(expected in value for value in rendered) or expected in "".join(rendered)))
    avatar = next((node for node in visible if "用户头像" in {node.get("name"), node.get("label")}), None)
    top = max(100, float(avatar.get("y") or 0) + float(avatar.get("height") or 0)) if avatar is not None else 100
    from .proxy_protocol import price
    amounts = {amount for node in visible if node.tag in {"XCUIElementTypeStaticText", "XCUIElementTypeOther"}
               and top <= float(node.get("y") or 0) < max(300, top + 180)
               and (amount := price(_text(node).replace(",", ""))) is not None}
    if len(amounts) != 1:
        raise ValueError("详情售价未就绪或存在多个数值")
    amount = amounts.pop()
    if not body_matched and (expected_price is None or amount != expected_price):
        raise ValueError("当前页面尚未对应代理正文或售价")
    return {"price": amount, "body_matched": body_matched}


def detail_below_fold(source):
    root = ET.fromstring(source)
    app = root if root.tag == "XCUIElementTypeApplication" else root.find(".//XCUIElementTypeApplication")
    width, height = (float(app.get(key) or 0) for key in ("width", "height")) if app is not None else (0, 0)
    if not width or not height:
        return None
    visible = [node for node in root.iter() if node.get("visible") != "false"]
    if not any(_text(node) in {"分享按钮", "返回按钮"} for node in visible) and detail_back_rect(source) is None:
        return None
    from .proxy_protocol import price
    low_price = any(node.tag in {"XCUIElementTypeStaticText", "XCUIElementTypeOther"}
                    and height * .5 < float(node.get("y") or 0) < height * .9
                    and price(_text(node)) is not None for node in visible)
    return (width, height) if low_price else None


class IPhoneProxyCollector(IPhoneCollector):
    def __init__(self, settings, proxy):
        super().__init__(settings)
        self.proxy = proxy

    async def collect(self, udid, os_version, keyword, region_hint, region_steps, storages, scroll_pages,
                      is_candidate, on_items=None, check_detail=None, *, client_ip):
        return await asyncio.to_thread(self._collect_proxy, udid, os_version, keyword, region_hint,
                                      region_steps, storages, scroll_pages, is_candidate, on_items,
                                      check_detail, client_ip)

    def _connect(self, udid, os_version):
        from appium import webdriver
        from appium.options.ios import XCUITestOptions
        if not self.settings.ios_wda_bundle_id:
            raise RuntimeError("未配置 IOS_WDA_BUNDLE_ID")
        ensure_appium_server(self.settings)
        caps = {"platformName": "iOS", "appium:automationName": "XCUITest", "appium:udid": udid,
                "appium:bundleId": self.settings.bundle_id, "appium:noReset": True,
                "appium:shouldTerminateApp": False, "appium:usePrebuiltWDA": True,
                "appium:derivedDataPath": str(self.settings.ios_wda_derived_data_path),
                "appium:updatedWDABundleId": self.settings.ios_wda_bundle_id,
                "appium:wdaLocalPort": WDA_PORT, "appium:newCommandTimeout": 300,
                "appium:waitForIdleTimeout": 0.5,
                "appium:settings": {"waitForIdleTimeout": 0.5, "animationCoolOffTimeout": 0.3}}
        if os_version:
            caps["appium:platformVersion"] = os_version
        return webdriver.Remote(self.settings.appium_server_url, options=XCUITestOptions().load_capabilities(caps))

    def _drag_list(self, driver):
        source = driver.page_source
        _raise_if_verification(source)
        if not is_search_results(source):
            raise RuntimeError("代理翻页前未确认搜索列表")
        size = driver.get_window_size()
        driver.execute_script("mobile: dragFromToForDuration", {"duration": 0.35,
            "fromX": size["width"] * .48, "fromY": size["height"] * .84,
            "toX": size["width"] * .48, "toY": size["height"] * .3})

    def _confirm_storage_filter(self, driver):
        self._storage_requested_at = time.time()
        super()._confirm_storage_filter(driver)

    def _sort_new(self, driver):
        # Proxy responses provide readiness; no fixed sleep or new session.
        button = self._find(driver, "新发", ("XCUIElementTypeStaticText",))
        if button is None:
            raise RuntimeError("找不到「新发」排序按钮")
        button.click()

    def _prepare_pages(self, driver, client_ip, keyword, storage, scroll_pages, actions):
        self._storage_requested_at = None
        clicked = self._apply_storage(driver, storage)
        actions.append({"action": "容量筛选", "target": storage, "clicked": clicked, "success": True})
        initial = None
        since = self._storage_requested_at
        verification_since = since
        if since is not None:
            initial = checked(self.proxy.wait(client_ip, since, keyword=keyword, timeout=2))
        if initial and is_new_sort(newest_search(initial)):
            actions.append({"action": "保持新发排序", "target": "create / desc", "success": True,
                            "method": "容量提交的本次返回已确认新发，无需重复点击"})
        else:
            since = time.time()
            self._sort_new(driver)
            initial = None
            actions.append({"action": "切换新发排序", "success": True})
        return self._pages(driver, client_ip, keyword, since, scroll_pages, initial_events=initial, actions=actions,
                           verification_since=verification_since)

    def _pages(self, driver, client_ip, keyword, since, scroll_pages, *, initial_events=None, actions=None,
               verification_since=None):
        verification_since = since if verification_since is None else verification_since
        actions = actions if actions is not None else []
        events = checked(initial_events if initial_events is not None else
                         self.proxy.wait(client_ip, since, keyword=keyword, verification_since=verification_since, timeout=18))
        if not events:
            _raise_if_verification(driver.page_source)
            raise RuntimeError("未收到本次搜索的代理数据；请检查手机 Wi-Fi 代理、证书与设备 IP")
        # Response arrival order does not identify the latest UI request.
        first = newest_search(events)
        if not is_new_sort(first):
            actual = sort_label(first)
            if first.get("sort_field") == "create" and first.get("sort_value") == "asc":
                # Re-clicking an already selected New tab can reverse its direction.
                since = time.time()
                self._sort_new(driver)
                actions.append({"action": "纠正新发排序", "target": actual, "success": True,
                                "method": "已收到发布时间升序，本次仅补点一次并等待降序返回"})
            else:
                since = max(since, first.get("request_started", since))
                actions.append({"action": "等待新发排序返回", "target": actual, "success": True})
            events = checked(self.proxy.wait(client_ip, since, keyword=keyword, sort_field="create", sort_value="desc",
                                            verification_since=verification_since, timeout=18))
            if not events or not is_new_sort(newest_search(events)):
                _raise_if_verification(driver.page_source)
                observed = sort_label(newest_search(events)) if events else actual
                raise RuntimeError(f"代理响应未确认新发排序（实际 {observed}），停止采集")
            first = newest_search(events)
        fingerprint = first["filter_fingerprint"]
        batches = [event for event in events if is_new_sort(event) and event.get("filter_fingerprint") == fingerprint]
        actions.append({"action": "确认新发排序", "target": "create / desc", "success": True,
                        "method": "本次代理请求与返回确认"})
        last_sequence = max(event["sequence"] for event in batches)
        for _ in range(max(0, scroll_pages)):
            if batches[-1].get("has_more") is False:
                break
            self._drag_list(driver)
            more = checked(self.proxy.wait(client_ip, since, keyword=keyword, fingerprint=fingerprint,
                                           sort_field="create", sort_value="desc",
                                           after_sequence=last_sequence, verification_since=verification_since, timeout=1.5))
            if more:
                batches.extend(more)
                last_sequence = max(event["sequence"] for event in more)
        _raise_if_verification(driver.page_source)
        result = {}
        for batch in batches:
            for item in batch.get("items", []):
                result.setdefault(item["item_id"], item)
        return list(result.values()), len(batches)

    def _detail(self, driver, raw, client_ip, actions):
        target, since = raw["item_id"], time.time()
        driver.execute_script("mobile: deepLink", {"url": f"fleamarket://item?id={target}",
                                                  "bundleId": self.settings.bundle_id})
        actions.append({"action": "打开候选详情", "target": target, "method": "商品 ID", "success": True})
        events = checked(self.proxy.wait(client_ip, since, kind="detail", target=target, timeout=15))
        if not events:
            _raise_if_verification(driver.page_source)
            raise RuntimeError("未收到该商品本次详情返回，未使用列表摘要或旧缓存代替")
        item = events[-1].get("item")
        if not item or item.get("item_id") != target or not item.get("description"):
            raise RuntimeError("代理详情缺少对应商品的完整正文")
        item = {**raw, **item, "image_urls": item.get("image_urls") or raw.get("image_urls", [])}
        observed = {}
        def ready(xml):
            try:
                current = detail_view(xml, item["description"], item.get("price"))
                observed.update(current)
                return True
            except (ValueError, ET.ParseError):
                return False
        source = driver.page_source
        _raise_if_verification(source)
        for _ in range(2):
            if ready(source):
                break
            bounds = detail_below_fold(source)
            if bounds is None:
                break
            width, height = bounds
            driver.execute_script("mobile: dragFromToForDuration", {"duration": .35,
                "fromX": width * .5, "fromY": height * .82, "toX": width * .5, "toY": height * .34})
            actions.append({"action": "展开详情正文", "method": "大图布局，按售价位置受控下滑", "success": True})
            source = driver.page_source
            _raise_if_verification(source)
        if not observed:
            source = self._wait_for_source(driver, ready, "代理详情尚未对应当前页面正文与售价")
        # Display price is authoritative while native price layouts are still varied.
        item["price"] = observed["price"]
        if observed["body_matched"]:
            extra = detail_content(source)
            item["detail_parameters"] = list(dict.fromkeys(item.get("detail_parameters", []) + extra.get("detail_parameters", [])))
            seller = detail_seller(source)
            if seller["seller_name"]:
                item.update(seller)
        item.update(item_url=f"https://www.goofish.com/item?id={target}", acquisition_stage="valuation")
        actions.append({"action": "读取代理详情", "target": target, "success": True})
        return item

    @exclusive_device
    def _collect_proxy(self, udid, os_version, keyword, region_hint, region_steps, storages, scroll_pages,
                       is_candidate, on_items, check_detail, client_ip):
        self.proxy.wait_ready()
        result, driver, setup = CollectResult(items=[]), None, []
        with self.proxy.session(client_ip):
            try:
                driver = self._connect(udid, os_version)
                emit_safely(on_items, stage="代理模式 · 搜索", storage=None)
                inherited = self._search(driver, keyword, region_hint, setup)
                if region_hint:
                    if not inherited:
                        self._apply_region(driver, region_hint, region_steps)
                    result.region_applied = True
                emit_safely(on_items, stage="代理模式 · 筛选机型", storage=None)
                self._apply_model(driver, keyword)
                setup.append({"action": "机型及地区筛选", "target": keyword, "success": True})
                for storage in storages:
                    actions, pass_items, cards, candidates, error = setup.copy(), [], [], 0, None
                    setup = []
                    meta = {"storage": storage, "region_applied": result.region_applied, "filter_applied": True}
                    try:
                        emit_safely(on_items, stage=f"代理筛选 {storage} 并确认新发", storage=storage)
                        cards, batches = self._prepare_pages(driver, client_ip, keyword, storage, scroll_pages, actions)
                        actions.append({"action": "批量读取代理列表", "target": f"{batches} 批 · {len(cards)} 件", "success": True})
                        for raw in cards:
                            if not is_candidate(raw, result.region_applied, storage):
                                emit_safely(on_items, cards=[raw], **meta)
                                continue
                            candidates += 1
                            item = {**raw, "acquisition_stage": "detail"}
                            opened = False
                            detail_error = None
                            try:
                                opened = True
                                emit_safely(on_items, stage=f"代理读取 {storage} 候选详情", storage=storage)
                                item = self._detail(driver, raw, client_ip, actions)
                                decision = check_detail(item, result.region_applied, storage) if check_detail else None
                                if decision and not decision.get("matched") and decision.get("code") != "pending":
                                    emit_safely(on_items, cards=[{**item, "detail_rejection": decision}], **meta)
                                    continue
                                item.pop("acquisition_stage", None)
                                item.update(source_storage=storage, source_pass="新发", source_channel="proxy")
                                pass_items.append(item)
                                result.items.append(item)
                                emit_safely(on_items, [item], **meta)
                            except _VerificationRequired as exc:
                                detail_error = exc
                                raise
                            except Exception as exc:
                                detail_error = exc
                                emit_safely(on_items, cards=[{**item, "link_error": _brief_error(exc)}], **meta)
                                actions.append({"action": "读取代理详情", "target": raw["item_id"],
                                                "success": False, "error": _brief_error(exc)})
                            finally:
                                if opened and not isinstance(detail_error, _VerificationRequired):
                                    try:
                                        self._return_to_results(driver, actions)
                                    except _VerificationRequired:
                                        raise
                                    except Exception as return_error:
                                        if detail_error is not None:
                                            raise RuntimeError(f"详情读取失败：{_brief_error(detail_error)}；"
                                                               f"返回列表失败：{_brief_error(return_error)}") from detail_error
                                        raise
                    except Exception as exc:
                        error = _brief_error(exc)
                    entry = {"label": "新发", "storage": storage, "channel": "proxy", "model_applied": True,
                             "region_applied": result.region_applied, "filter_applied": error is None,
                             "observed": len(pass_items), "cards": len(cards), "candidates": candidates,
                             "card_samples": cards[:40], "cards_truncated": len(cards) > 40,
                             "items": pass_items[:25], "items_total": len(pass_items),
                             "items_truncated": len(pass_items) > 25, "actions": actions, "error": error}
                    result.passes.append(entry)
                    emit_safely(on_items, pass_info=entry, storage=storage)
                    if error:
                        break
            except Exception as exc:
                result.passes.append({"label": "新发", "storage": None, "channel": "proxy",
                                      "error": _brief_error(exc), "observed": 0, "actions": setup})
            finally:
                _quit_driver(driver)
        result.filter_applied = bool(result.passes) and all(entry.get("filter_applied") for entry in result.passes)
        return result
