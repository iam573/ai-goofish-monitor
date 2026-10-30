import threading

import pytest

from src.infrastructure.external.iphone import device_lock, search_client
from src.infrastructure.external.iphone.devices import (
    IPhoneDevice,
    candidate_devices,
    parse_devicectl_devices,
    select_device,
)
from src.infrastructure.external.iphone.page_source import PhoneCard
from src.infrastructure.external.iphone.search_client import IPhoneListing, IPhoneSearchClient
from src.infrastructure.external.iphone.settings import IPhoneSettings


def make_settings(**overrides):
    values = dict(
        appium_server_url="http://127.0.0.1:4723", wda_bundle_id="com.example.wda",
        wda_derived_data_path=None, wda_local_port=8101, wda_team_id="", wda_project_path=None,
        udid="", goofish_bundle_id="com.taobao.fleamarket",
        appium_bin=None, appium_home=None, screens_per_page=3, lock_timeout_seconds=0,
    )
    values.update(overrides)
    return IPhoneSettings(**values)


DEVICE = IPhoneDevice(udid="UDID-1", name="测试 iPhone", os_version="18.1", connected=True, status="已连接")


def card_source(*cards):
    cells = "".join(
        f'''<XCUIElementTypeCell><XCUIElementTypeStaticText name="{title}" x="10" y="{y}" />
        <XCUIElementTypeStaticText name="¥{price}" x="10" y="{y + 50}" /></XCUIElementTypeCell>'''
        for title, price, y in cards
    )
    return f"<XCUIElementTypeApplication>{cells}</XCUIElementTypeApplication>"


class FakeDriver:
    def __init__(self, screens):
        self.screens = list(screens)
        self.index = 0
        self.swipes = 0
        self.quit_called = False

    @property
    def page_source(self):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute_script(self, name, args=None):
        if name == "mobile: swipe":
            self.swipes += 1
            self.index += 1

    def quit(self):
        self.quit_called = True


def make_client(monkeypatch, driver, stop_event=None):
    client = IPhoneSearchClient(make_settings(), DEVICE, stop_event=stop_event, log=lambda _: None)
    calls = []
    monkeypatch.setattr(client, "_connect", lambda: driver)
    monkeypatch.setattr(client, "_search", lambda d, keyword, actions: calls.append(("search", keyword)))
    monkeypatch.setattr(client, "_sort_new", lambda d: calls.append(("sort", "新发")))
    monkeypatch.setattr(search_client.time, "sleep", lambda _: None)
    return client, calls


def test_search_sorts_by_new_and_only_opens_candidates(monkeypatch):
    driver = FakeDriver([
        card_source(("Sony A7M4 单机身", 9800, 300), ("Sony A7M4 套机", 15000, 600)),
        card_source(("Sony A7M4 套机", 15000, 300), ("Sony A7M4 99新", 9500, 600)),
    ])
    client, calls = make_client(monkeypatch, driver)
    opened = []
    monkeypatch.setattr(client, "_open_listing", lambda d, card, actions, capture: (
        opened.append(card.title) or IPhoneListing(card, f"id-{card.price:g}", "", [], "卖家")))
    listings = []

    summary = client.search("sony a7m4", 2, lambda card: card.price < 10000, listings.append)

    assert calls == [("search", "sony a7m4"), ("sort", "新发")]
    assert driver.swipes == 1 and driver.quit_called
    assert summary.cards_seen == 3 and summary.candidates == 2 and summary.linked == 2
    assert opened == ["Sony A7M4 单机身", "Sony A7M4 99新"]
    assert [listing.item_id for listing in listings] == ["id-9800", "id-9500"]


def test_candidate_failure_continues_only_when_back_on_result_list(monkeypatch):
    driver = FakeDriver([card_source(("Sony A7M4 单机身", 9800, 300), ("Sony A7M4 99新", 9500, 600))])
    client, _ = make_client(monkeypatch, driver)

    def fail(*args):
        raise RuntimeError("分享菜单没有复制链接")

    monkeypatch.setattr(client, "_open_listing", fail)
    monkeypatch.setattr(search_client, "is_search_results", lambda source: True)
    summary = client.search("sony", 1, lambda card: True, lambda listing: None)
    assert summary.candidates == 2 and summary.linked == 0 and len(summary.warnings) == 2

    monkeypatch.setattr(search_client, "is_search_results", lambda source: False)
    with pytest.raises(RuntimeError, match="未回到搜索列表"):
        client.search("sony", 1, lambda card: True, lambda listing: None)
    assert driver.quit_called


def test_stop_event_ends_search_without_error(monkeypatch):
    driver = FakeDriver([card_source(("Sony A7M4 单机身", 9800, 300), ("Sony A7M4 99新", 9500, 600))])
    stop = threading.Event()
    client, _ = make_client(monkeypatch, driver, stop_event=stop)
    monkeypatch.setattr(client, "_open_listing", lambda d, card, actions, capture: IPhoneListing(card, "1", "", [], ""))

    def is_candidate(card):
        stop.set()
        return False

    summary = client.search("sony", 3, is_candidate, lambda listing: None)
    assert summary.stopped and summary.cards_seen == 1 and driver.quit_called


@pytest.mark.parametrize("clipboard, expected", [
    ("来闲鱼 https://www.goofish.com/item?id=1091531543430", "1091531543430"),
    ("copy-marker", None),
])
def test_copy_item_id_requires_a_fresh_clipboard(monkeypatch, clipboard, expected):
    class Element:
        rect = {"x": 10, "y": 700, "width": 60, "height": 60}

        def click(self):
            pass

    class Driver:
        def find_elements(self, *args):
            return [Element()]

        def execute_script(self, *args):
            pass

    client = IPhoneSearchClient(make_settings(), DEVICE, log=lambda _: None)
    monkeypatch.setattr(search_client, "prepare_pasteboard", lambda udid: "copy-marker")
    monkeypatch.setattr(search_client, "read_pasteboard", lambda udid: clipboard)
    monkeypatch.setattr(search_client.time, "sleep", lambda _: None)
    if expected is None:
        with pytest.raises(RuntimeError, match="剪贴板未产生新链接"):
            client._copy_item_id(Driver(), Element())
    else:
        assert client._copy_item_id(Driver(), Element()) == expected


def test_devicectl_parser_and_device_selection():
    payload = {"result": {"devices": [
        {"hardwareProperties": {"udid": "A", "deviceType": "iPhone", "reality": "physical"},
         "deviceProperties": {"name": "离线手机", "osVersionNumber": "17.0"},
         "connectionProperties": {"pairingState": "paired", "tunnelState": "disconnected"}},
        {"hardwareProperties": {"udid": "B", "deviceType": "iPhone", "reality": "physical"},
         "deviceProperties": {"name": "主力机", "osVersionNumber": "18.1"},
         "connectionProperties": {"pairingState": "paired", "tunnelState": "connected"}},
        {"hardwareProperties": {"udid": "C", "deviceType": "iPad", "reality": "physical"}},
    ]}}
    devices = parse_devicectl_devices(payload)
    assert [(device.udid, device.connected) for device in devices] == [("A", False), ("B", True)]
    assert select_device(devices).udid == "B"
    with pytest.raises(RuntimeError, match="未连接"):
        select_device(devices, "A")
    with pytest.raises(RuntimeError, match="没有已连接"):
        select_device(devices[:1])


def test_devicectl_status_explains_why_phone_is_offline():
    def device(udid, connection, developer_mode="enabled"):
        return {"hardwareProperties": {"udid": udid, "deviceType": "iPhone", "reality": "physical"},
                "deviceProperties": {"name": udid, "developerModeStatus": developer_mode},
                "connectionProperties": connection}

    payload = {"result": {"devices": [
        device("unplugged", {"pairingState": "paired", "tunnelState": "unavailable"}),
        device("locked", {"pairingState": "paired", "tunnelState": "disconnected", "transportType": "wired"}),
        device("no-dev-mode", {"pairingState": "paired", "tunnelState": "connected", "transportType": "wired"},
               developer_mode="disabled"),
    ]}}
    statuses = {item.udid: (item.connected, item.status) for item in parse_devicectl_devices(payload)}
    assert statuses["unplugged"] == (False, "未连接：请插上数据线并解锁手机")
    assert statuses["locked"] == (False, "通道未建立：请解锁手机，或重新插拔数据线")
    assert statuses["no-dev-mode"] == (False, "未开启开发者模式")


def test_device_lock_rejects_second_holder(monkeypatch, tmp_path):
    monkeypatch.setattr(device_lock, "device_lock_path", lambda udid: tmp_path / f"{udid}.lock")
    with device_lock.iphone_device_lock("UDID-1", 0):
        with pytest.raises(RuntimeError, match="等待 iPhone 空闲超时"):
            with device_lock.iphone_device_lock("UDID-1", 0):
                pass
    with device_lock.iphone_device_lock("UDID-1", 0):
        pass


def test_any_device_lock_picks_first_idle_phone(monkeypatch, tmp_path):
    monkeypatch.setattr(device_lock, "device_lock_path", lambda udid: tmp_path / f"{udid}.lock")
    with device_lock.iphone_device_lock("UDID-1", 0):
        assert device_lock.iphone_device_busy("UDID-1")
        assert not device_lock.iphone_device_busy("UDID-2")
        with device_lock.any_iphone_device_lock(["UDID-1", "UDID-2"], 0) as udid:
            assert udid == "UDID-2"
            with pytest.raises(RuntimeError, match="等待 iPhone 空闲超时"):
                with device_lock.any_iphone_device_lock(["UDID-1", "UDID-2"], 0):
                    pass
    assert not device_lock.iphone_device_busy("UDID-1")


def test_candidate_devices_follow_selection_order():
    devices = [
        IPhoneDevice("A", "甲", "18", True, "已连接"),
        IPhoneDevice("B", "乙", "18", False, "未连接"),
        IPhoneDevice("C", "丙", "18", True, "已连接"),
    ]
    assert [device.udid for device in candidate_devices(devices, [])] == ["A"]
    assert [device.udid for device in candidate_devices(devices, [], "C")] == ["C"]
    assert [device.udid for device in candidate_devices(devices, ["C", "B", "A"])] == ["C", "A"]
    with pytest.raises(RuntimeError, match="已选的 iPhone 都未连接（乙：未连接；GONE01：未发现）"):
        candidate_devices(devices, ["B", "GONE01"])


def test_port_offset_changes_forwarded_ports():
    assert "appium:mjpegServerPort" not in IPhoneSearchClient(make_settings(), DEVICE)._capabilities()
    capabilities = IPhoneSearchClient(make_settings(), DEVICE, port_offset=2)._capabilities()
    assert capabilities["appium:wdaLocalPort"] == 8103
    assert capabilities["appium:mjpegServerPort"] == 9102


def test_missing_wda_bundle_id_fails_before_connecting():
    client = IPhoneSearchClient(make_settings(wda_bundle_id=""), DEVICE)
    with pytest.raises(RuntimeError, match="IOS_WDA_BUNDLE_ID"):
        client._connect()
