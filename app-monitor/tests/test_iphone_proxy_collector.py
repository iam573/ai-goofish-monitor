import time
from types import SimpleNamespace

import pytest

from app_monitor.iphone_proxy_collector import IPhoneProxyCollector, checked, detail_view
from app_monitor.iphone_collector import _VerificationRequired


def test_proxy_pages_require_new_sort_and_keep_only_same_filter_context(monkeypatch):
    first = {"event": "search", "sequence": 1, "sort_field": "create", "sort_value": "desc",
             "filter_fingerprint": "correct", "items": [{"item_id": "1093306353441"}], "has_more": True}
    second = {**first, "sequence": 2, "has_more": False, "items": [{"item_id": "1092690649226"}]}
    calls = []
    def wait(*args, **kwargs):
        calls.append(kwargs)
        return [first] if len(calls) == 1 else [second]
    collector = IPhoneProxyCollector(SimpleNamespace(), SimpleNamespace(wait=wait))
    monkeypatch.setattr(collector, "_drag_list", lambda driver: None)
    items, count = collector._pages(SimpleNamespace(page_source="<Application/>"), "192.168.1.20", "iPhone 15 Pro", time.time(), 3)
    assert [item["item_id"] for item in items] == ["1093306353441", "1092690649226"]
    assert count == 2 and len(calls) == 2 and calls[1]["fingerprint"] == "correct"
    first["sort_field"] = "price"
    calls.clear()
    with pytest.raises(RuntimeError, match="新发排序"):
        collector._pages(SimpleNamespace(page_source="<Application/>"), "192.168.1.20", "iPhone 15 Pro", time.time(), 0)


def test_verification_stops_instead_of_becoming_an_empty_list():
    with pytest.raises(_VerificationRequired):
        checked([{"event": "verification"}])


def test_detail_opens_explicit_id_and_never_shares_or_uses_list_description():
    key = "1093306353441"
    body = "iPhone 15 Pro 256G，换过第三方屏，电池78%"
    calls = []
    snapshot = {"item_id": key, "title": body, "description": body, "price": 2799, "image_urls": [], "detail_parameters": []}
    proxy = SimpleNamespace(wait=lambda *args, **kwargs: [{"event": "detail", "item": snapshot}])
    source = f'<XCUIElementTypeApplication><XCUIElementTypeOther name="分享按钮"/><XCUIElementTypeStaticText name="2799" y="157"/><XCUIElementTypeTextView name="{body}" visible="true"/></XCUIElementTypeApplication>'
    driver = SimpleNamespace(page_source=source, execute_script=lambda name, args: calls.append((name, args)))
    collector = IPhoneProxyCollector(SimpleNamespace(bundle_id="com.taobao.fleamarket"), proxy)
    item = collector._detail(driver, {"item_id": key, "title": "列表摘要", "price": 2800}, "192.168.1.20", [])
    assert item["description"] == body and item["price"] == 2799
    assert calls == [("mobile: deepLink", {"url": f"fleamarket://item?id={key}", "bundleId": "com.taobao.fleamarket"})]


def test_no_current_detail_response_is_not_replaced_by_old_list_data():
    collector = IPhoneProxyCollector(SimpleNamespace(bundle_id="app"), SimpleNamespace(wait=lambda *a, **k: []))
    driver = SimpleNamespace(page_source="<Application/>", execute_script=lambda *a: None)
    with pytest.raises(RuntimeError, match="旧缓存"):
        collector._detail(driver, {"item_id": "1093306353441", "title": "列表文字", "price": 100}, "192.168.1.20", [])


def test_transition_waits_for_requested_body_before_reading_price():
    key = "1093306353441"
    def source(body, amount):
        return f'<XCUIElementTypeApplication><XCUIElementTypeOther name="返回按钮"/><XCUIElementTypeStaticText name="{amount}" y="157"/><XCUIElementTypeTextView name="{body}"/></XCUIElementTypeApplication>'
    class Driver:
        reads = 0
        @property
        def page_source(self):
            self.reads += 1
            return source("旧商品的正文", 900) if self.reads == 1 else source("当前商品的完整正文", 2500)
        def execute_script(self, *args):
            pass
    driver = Driver()
    response = {"event": "detail", "item": {"item_id": key, "description": "当前商品的完整正文", "price": None}}
    collector = IPhoneProxyCollector(SimpleNamespace(bundle_id="app"), SimpleNamespace(wait=lambda *a, **k: [response]))
    item = collector._detail(driver, {"item_id": key, "title": "列表摘要", "price": 2600}, "192.168.1.20", [])
    assert driver.reads >= 2 and item["price"] == 2500 and item["description"] == "当前商品的完整正文"


def test_short_native_static_text_body_is_valid_without_a_text_view():
    body = "苹果15pro 256G 白色钛金属 国行 电池89 外观靓机\n3688\n厦门同城可自提"
    source = f'<XCUIElementTypeApplication><XCUIElementTypeOther name="返回按钮"/><XCUIElementTypeStaticText name="3688" y="157"/><XCUIElementTypeStaticText name="{body}" y="310"/></XCUIElementTypeApplication>'
    assert detail_view(source, body)["price"] == 3688
    with pytest.raises(ValueError, match="正文或售价"):
        detail_view(source, "另一件商品的正文")


def test_unexposed_body_only_accepts_same_id_network_price_and_does_not_guess():
    source = '<XCUIElementTypeApplication><XCUIElementTypeOther name="返回按钮"/><XCUIElementTypeStaticText name="3688" y="157"/></XCUIElementTypeApplication>'
    assert detail_view(source, "代理中的完整正文", 3688) == {"price": 3688, "body_matched": False}
    with pytest.raises(ValueError):
        detail_view(source, "代理中的完整正文", 3000)
    with pytest.raises(ValueError):
        detail_view(source, "代理中的完整正文")


def test_photo_first_detail_scrolls_to_body_and_uses_relative_seller_price_area():
    key, body = "1093306353441", "苹果15pro 256G 国行 电池89\n3688\n厦门同城可自提"
    top = '<XCUIElementTypeApplication width="390" height="844"><XCUIElementTypeOther name="返回按钮"/><XCUIElementTypeOther name="用户头像" y="592" height="32"/><XCUIElementTypeStaticText name="3688" y="642"/></XCUIElementTypeApplication>'
    shown = f'<XCUIElementTypeApplication width="390" height="844"><XCUIElementTypeOther name="返回按钮"/><XCUIElementTypeOther name="用户头像" y="190" height="32"/><XCUIElementTypeStaticText name="3688" y="240"/><XCUIElementTypeStaticText name="{body}" y="330"/></XCUIElementTypeApplication>'
    class Driver:
        page_source = top
        calls = []
        def execute_script(self, name, args):
            self.calls.append(name)
            if name == "mobile: dragFromToForDuration":
                self.page_source = shown
    driver = Driver()
    response = {"event": "detail", "item": {"item_id": key, "description": body, "price": None}}
    collector = IPhoneProxyCollector(SimpleNamespace(bundle_id="app"), SimpleNamespace(wait=lambda *a, **k: [response]))
    item = collector._detail(driver, {"item_id": key, "title": "列表摘要", "price": 3600}, "192.168.1.20", [])
    assert item["price"] == 3688 and driver.calls.count("mobile: dragFromToForDuration") == 1
