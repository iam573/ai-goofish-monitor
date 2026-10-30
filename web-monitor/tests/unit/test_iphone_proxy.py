import shutil
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import iphone
from src.infrastructure.external.iphone.devices import IPhoneDevice
from src.infrastructure.external.iphone.page_source import detail_back_rect
from src.infrastructure.external.iphone.proxy.capture import ProxyService
from src.infrastructure.external.iphone.proxy.proxy_protocol import parse_search, verification
from src.infrastructure.external.iphone.proxy.subscriber import ProxySubscriber
from src.infrastructure.external.iphone.proxy_search_client import (
    IPhoneProxySearchClient,
    detail_view,
    local_publish_time,
)
from src.infrastructure.external.iphone.search_client import IPhoneVerificationRequired
from src.infrastructure.external.iphone.settings import IPhoneSettings
from src.infrastructure.persistence.iphone_device_selection import (
    load_iphone_device_channels,
    proxy_client_ips,
    save_iphone_device_channel,
)

PHONE_IP = "192.168.1.23"


def make_settings(**overrides):
    values = dict(
        appium_server_url="http://127.0.0.1:4723", wda_bundle_id="com.example.wda",
        wda_derived_data_path=None, wda_local_port=8101, wda_team_id="", wda_project_path=None,
        udid="", goofish_bundle_id="com.taobao.fleamarket",
        appium_bin=None, appium_home=None, screens_per_page=3, lock_timeout_seconds=0,
    )
    values.update(overrides)
    return IPhoneSettings(**values)


@pytest.fixture
def runtime():
    # Unix socket 路径有长度上限，pytest 的 tmp_path 在 macOS 上太长
    path = Path(tempfile.mkdtemp(prefix="gp-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_protocol_parses_search_items_and_verification():
    payload = {"data": {"resultList": [{"data": {"item": {"main": {
        "exContent": {"itemId": "1091531543430", "title": "Sony A7M4", "price": "¥9800", "area": "上海",
                      "picUrl": "https://img.alicdn.com/bao/a.jpg"},
        "clickParam": {"args": {"publishTime": "1793000000000"}},
    }}}}], "resultInfo": {"hasNextPage": "false"}}}
    result = parse_search(payload)
    assert result["has_more"] is False
    item = result["items"][0]
    assert (item["item_id"], item["price"], item["display_region"]) == ("1091531543430", 9800.0, "上海")
    assert item["image_urls"] == ["https://img.alicdn.com/bao/a.jpg"]
    assert local_publish_time(item["publish_time"])
    assert verification({"ret": ["FAIL_SYS_USER_VALIDATE::请验证"]})
    assert verification({}, status=419) and not verification({"ret": ["SUCCESS::调用成功"]})


def test_relay_forwards_only_subscribed_phone_events(runtime):
    service = ProxyService(make_settings(proxy_runtime=runtime))
    service.configure([PHONE_IP, "192.168.1.24"])
    service._ensure_socket_server()
    service._ready = True
    try:
        subscriber = ProxySubscriber(service.settings.proxy_socket_path)
        with subscriber.session(PHONE_IP):
            subscriber.wait_ready(timeout=2)
            with pytest.raises(RuntimeError, match="已有代理采集任务"):
                with ProxySubscriber(service.settings.proxy_socket_path).session(PHONE_IP):
                    pass
            started = time.time()
            service.ingest({"event": "search", "client_ip": "192.168.1.24", "request_started": started,
                            "keyword": "a7m4", "items": []})
            service.ingest({"event": "traffic", "client_ip": PHONE_IP})
            service.ingest({"event": "search", "client_ip": PHONE_IP, "request_started": started,
                            "keyword": "A7M4", "sort_field": "create", "sort_value": "desc", "items": []})
            events = subscriber.wait(started - 1, timeout=2, keyword="a7m4")
            assert [(event["client_ip"], event["sort_value"]) for event in events] == [(PHONE_IP, "desc")]
            assert subscriber.wait(started - 1, timeout=0.2, kind="detail") == []
            assert service.status(PHONE_IP)["busy"] is True
            assert service.status(PHONE_IP)["client_connected"] is True
        with pytest.raises(RuntimeError, match="未启用代理采集"):
            with ProxySubscriber(service.settings.proxy_socket_path).session("192.168.1.99"):
                pass
    finally:
        service.stop()
    assert not service.settings.proxy_socket_path.exists()


def test_subscriber_reports_missing_service(runtime):
    with pytest.raises(RuntimeError, match="代理服务未运行"):
        with ProxySubscriber(runtime / "events.sock").session(PHONE_IP):
            pass


def test_channel_validation_rejects_bad_and_duplicate_ips(tmp_path):
    db = str(tmp_path / "app.sqlite3")
    assert load_iphone_device_channels(db) == {}
    assert save_iphone_device_channel("A", "proxy", f" {PHONE_IP} ", db) == {"channel": "proxy", "client_ip": PHONE_IP}
    with pytest.raises(ValueError, match="另一台代理设备"):
        save_iphone_device_channel("B", "proxy", PHONE_IP, db)
    for bad in ("127.0.0.1", "0.0.0.0", "phone"):
        with pytest.raises(ValueError, match="有效 IP"):
            save_iphone_device_channel("B", "proxy", bad, db)
    with pytest.raises(ValueError, match="需要填写"):
        save_iphone_device_channel("B", "proxy", "", db)
    save_iphone_device_channel("A", "ui", PHONE_IP, db)
    save_iphone_device_channel("B", "proxy", PHONE_IP, db)
    channels = load_iphone_device_channels(db)
    assert channels["A"] == {"channel": "ui", "client_ip": PHONE_IP}
    assert proxy_client_ips(channels) == [PHONE_IP]


def test_channel_api_saves_and_reports_proxy_status(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("IOS_WDA_BUNDLE_ID", "")
    monkeypatch.setattr(iphone.sys, "platform", "darwin")
    monkeypatch.setattr(iphone, "discover_devices", lambda: [IPhoneDevice("UDID-A", "主力机", "18.1", True, "已连接")])
    monkeypatch.setattr(iphone, "iphone_device_busy", lambda udid: False)
    monkeypatch.setattr(iphone, "appium_ready", lambda url: True)
    synced = []
    monkeypatch.setattr(iphone, "sync_proxy_service", lambda: synced.append(True))
    app = FastAPI()
    app.include_router(iphone.router)
    client = TestClient(app)

    rejected = client.put("/api/iphone/devices/UDID-A/channel", json={"channel": "proxy", "client_ip": "127.0.0.1"})
    saved = client.put("/api/iphone/devices/UDID-A/channel", json={"channel": "proxy", "client_ip": PHONE_IP})
    device = client.get("/api/iphone/status").json()["devices"][0]

    assert rejected.status_code == 400 and "有效 IP" in rejected.json()["detail"]
    assert saved.status_code == 200 and synced == [True]
    assert device["collection_channel"] == "proxy" and device["proxy_client_ip"] == PHONE_IP
    assert device["proxy_status"]["client_ip"] == PHONE_IP


RESULTS = """<XCUIElementTypeApplication width="430" height="932">
<XCUIElementTypeButton name="search more" visible="true" y="60"/>
<XCUIElementTypeStaticText name="新发" visible="true" y="120"/>
<XCUIElementTypeStaticText name="筛选" visible="true" y="122"/>
</XCUIElementTypeApplication>"""


def detail_source(description, amount, *, named_back=True):
    controls = ('<XCUIElementTypeButton name="分享按钮" visible="true" x="380" y="50"/>'
                '<XCUIElementTypeButton name="返回按钮" visible="true" x="5" y="50"/>') if named_back else ""
    return f"""<XCUIElementTypeApplication width="430" height="932">{controls}
<XCUIElementTypeStaticText value="¥{amount}" visible="true" x="20" y="150"/>
<XCUIElementTypeTextView value="{description}" visible="true" x="20" y="400"/>
</XCUIElementTypeApplication>"""


def test_detail_view_binds_proxy_body_and_reads_displayed_price():
    assert detail_view(detail_source("自用 A7M4 无拆修", 8800), "自用A7M4 无拆修") == {
        "price": 8800.0, "body_matched": True}
    assert detail_view(detail_source("其他正文", 8800), "自用A7M4", 8800.0)["body_matched"] is False
    with pytest.raises(ValueError, match="尚未对应"):
        detail_view(detail_source("其他正文", 8800), "自用A7M4", 9000.0)
    with pytest.raises(ValueError, match="尚未进入"):
        detail_view(detail_source("自用A7M4", 8800, named_back=False), "自用A7M4")


def test_detail_back_rect_finds_unnamed_back_of_deep_link_layout():
    source = """<XCUIElementTypeApplication width="430" height="932">
<XCUIElementTypeOther visible="true" x="4" y="58" width="36" height="36"/>
<XCUIElementTypeOther name="搜索" visible="true" x="48" y="56" width="300" height="40"/>
<XCUIElementTypeButton name="更多" visible="true" x="380" y="60" width="36" height="36"/>
<XCUIElementTypeButton name="留言按钮" visible="true" x="20" y="860" width="60" height="40"/>
<XCUIElementTypeButton name="收藏" visible="true" x="100" y="860" width="60" height="40"/>
</XCUIElementTypeApplication>"""
    assert detail_back_rect(source) == {"x": 4, "y": 58, "width": 36, "height": 36}
    assert detail_back_rect(source.replace('name="收藏"', 'name="其他"')) is None


class FakeProxy:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def session(self, client_ip):
        self.calls.append(("session", client_ip))
        return nullcontext(self)

    def wait_ready(self):
        self.calls.append(("ready",))

    def wait(self, since, timeout=15, **filters):
        self.calls.append(("wait", filters))
        return self.responses.pop(0) if self.responses else []


class FakeDriver:
    def __init__(self, details):
        self.details = details
        self.page_source = RESULTS
        self.scripts = []

    def execute_script(self, name, args):
        self.scripts.append((name, args))
        if name == "mobile: deepLink":
            self.page_source = self.details[args["url"].rsplit("=", 1)[1]]

    def get_window_size(self):
        return {"width": 430, "height": 932}


def search_event(sequence, sort_value, items, has_more=True):
    return {"event": "search", "sequence": sequence, "request_started": time.time(), "sort_field": "create",
            "sort_value": sort_value, "filter_fingerprint": "f1", "has_more": has_more, "items": items}


def raw(item_id, title, amount, **extra):
    return {"item_id": item_id, "title": title, "price": amount, "display_region": "上海",
            "image_urls": [f"https://img.alicdn.com/{item_id}.jpg"], "seller_name": "列表卖家",
            "publish_time": "2026-10-30T01:15:00+00:00", **extra}


def make_client(proxy, driver):
    client = IPhoneProxySearchClient(make_settings(), IPhoneDevice("UDID-A", "主力机", "18.1", True, "已连接"),
                                     client_ip=PHONE_IP, proxy=proxy, log=lambda message: None)
    clicks = []
    client._connect = lambda: driver
    client._search = lambda driver, keyword, actions: None
    client._ensure_foreground = lambda driver, actions: None
    client._find = lambda driver, name, types=(): type("Button", (), {"click": lambda self: clicks.append(name)})()

    def back(driver, actions):
        driver.page_source = RESULTS

    client._return_to_results = back
    return client, clicks


def test_proxy_client_corrects_sort_pages_and_reads_details():
    proxy = FakeProxy([
        [search_event(1, "asc", [raw("100", "旧的", 1)])],
        [search_event(2, "desc", [raw("111", "A7M4 单机身", 9800), raw("222", "A7M4 套机", 15000)])],
        [search_event(3, "desc", [raw("222", "A7M4 套机", 15000), raw("333", "A7M4 99新", 9000)], has_more=False)],
        [{"event": "detail", "item": {"item_id": "111", "description": "自用无拆修", "price": None,
                                      "detail_parameters": ["品牌：索尼"], "image_urls": []}}],
        [{"event": "detail", "item": {"item_id": "333", "description": "99新 箱说全", "price": 9000.0,
                                      "detail_parameters": [], "image_urls": ["https://img.alicdn.com/d.jpg"]}}],
    ])
    driver = FakeDriver({"111": detail_source("自用无拆修", 9500), "333": detail_source("另一种排版", 9000)})
    client, clicks = make_client(proxy, driver)
    listings, cards = [], []

    summary = client.search("A7M4", 3, lambda card: card.price < 10000, listings.append, on_card=cards.append)

    assert proxy.calls[:2] == [("session", PHONE_IP), ("ready",)]
    assert clicks == ["新发", "新发"]
    assert [card.item_id for card in cards] == ["111", "222", "333"]
    assert summary.cards_seen == 3 and summary.candidates == 2 and summary.linked == 2
    drags = [args for name, args in driver.scripts if name == "mobile: dragFromToForDuration"]
    assert len(drags) == 1
    first, second = listings
    assert first.card.price == 9500.0 and first.description == "自用无拆修"
    assert first.parameters == ["品牌：索尼"] and first.channel == "proxy"
    assert first.image_urls == ("https://img.alicdn.com/111.jpg",)
    assert first.seller_name == "列表卖家" and first.publish_time
    assert second.image_urls == ("https://img.alicdn.com/d.jpg", "https://img.alicdn.com/333.jpg")


def test_proxy_client_stops_on_verification():
    proxy = FakeProxy([[{"event": "verification", "request_started": time.time(), "sequence": 1}]])
    client, _ = make_client(proxy, FakeDriver({}))
    with pytest.raises(IPhoneVerificationRequired):
        client.search("A7M4", 1, lambda card: True, lambda listing: None)
