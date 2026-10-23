import base64

import pytest

from app_monitor.config import Settings
from app_monitor.iphone_collector import IPhoneCollector, PhoneCard, _pasteboard, _prepare_pasteboard, share_confirmation_text


@pytest.mark.parametrize("visible,url,prefix,suffix,expected", [
    ("true", "https://m.tb.cn/h.example", "【闲鱼】", "点击链接直接打开", True),
    ("false", "https://m.tb.cn/h.example", "【闲鱼】", "点击链接直接打开", False),
    ("true", "https://example.com/item?id=123456789", "【闲鱼】", "点击链接直接打开", False),
    ("true", "https://m.tb.cn/h.example", "商品介绍", "", False),
    ("true", "https://m.tb.cn/h.example", "【闲鱼】", "", False),
])
def test_share_confirmation_requires_visible_app_generated_share_text(visible, url, prefix, suffix, expected):
    text = f"{prefix}{url} {suffix}"
    xml = f'<XCUIElementTypeApplication><XCUIElementTypeOther visible="{visible}" name="{text}" /></XCUIElementTypeApplication>'
    assert share_confirmation_text(xml) == (text if expected else "")


class ClipboardDriver:
    def __init__(self, *, suffix=False):
        self.settings = Settings(ios_wda_bundle_id="test.runner" + (".xctrunner" if suffix else ""))
        self.foreground = self.settings.bundle_id
        self.idle = 0.5
        self.encoded = ""
        self.calls = []
        self.fail_read = False
        self.ignore_write = False

    def get_settings(self):
        return {"waitForIdleTimeout": self.idle}

    def update_settings(self, settings):
        self.idle = settings["waitForIdleTimeout"]

    def execute_script(self, command, args=None):
        self.calls.append((command, args))
        if command == "mobile: activateApp":
            assert self.idle == 0
            self.foreground = args["bundleId"]
        elif command == "mobile: activeAppInfo":
            return {"bundleId": self.foreground}
        elif command == "mobile: setClipboard":
            assert self.foreground == "test.runner.xctrunner"
            if not self.ignore_write:
                self.encoded = args["content"]
        elif command == "mobile: getClipboard":
            assert self.foreground == "test.runner.xctrunner"
            if self.fail_read:
                raise RuntimeError("private device ID and clipboard data")
            return self.encoded
        else:
            raise AssertionError(command)


@pytest.mark.parametrize("suffix", [True, False])
def test_clipboard_writes_and_verifies_fresh_marker_then_restores_app(suffix):
    driver = ClipboardDriver(suffix=suffix)
    marker = _prepare_pasteboard(driver, settings=driver.settings)
    assert marker.startswith("app-monitor-copy-")
    assert base64.b64decode(driver.encoded).decode() == marker
    assert driver.foreground == driver.settings.bundle_id
    assert driver.idle == 0.5
    assert not any(command == "mobile: tap" for command, _ in driver.calls)


def test_clipboard_decodes_goofish_share_text_without_using_host_clipboard():
    driver = ClipboardDriver()
    text = "闲鱼分享 https://www.goofish.com/item?id=1091531543430"
    driver.encoded = base64.b64encode(text.encode()).decode()
    assert _pasteboard(driver, settings=driver.settings) == text
    assert driver.foreground == driver.settings.bundle_id
    assert driver.idle == 0.5


@pytest.mark.parametrize("response", ["not base64!", "//4=", None])
def test_unreadable_clipboard_restores_app_and_reports_short_error(response):
    driver = ClipboardDriver()
    driver.encoded = response
    with pytest.raises(RuntimeError, match="手机剪贴板读写失败"):
        _pasteboard(driver, settings=driver.settings)
    assert driver.foreground == driver.settings.bundle_id
    assert driver.idle == 0.5


def test_clipboard_transport_failure_does_not_expose_native_error():
    driver = ClipboardDriver()
    driver.fail_read = True
    with pytest.raises(RuntimeError) as caught:
        _prepare_pasteboard(driver, settings=driver.settings)
    assert "private" not in str(caught.value)
    assert driver.foreground == driver.settings.bundle_id
    assert driver.idle == 0.5


def test_clipboard_noop_write_never_accepts_old_share_link():
    driver = ClipboardDriver()
    driver.ignore_write = True
    driver.encoded = base64.b64encode(b"https://www.goofish.com/item?id=1091531543430").decode()
    with pytest.raises(RuntimeError, match="标记写入后回读不一致"):
        _prepare_pasteboard(driver, settings=driver.settings)
    assert driver.foreground == driver.settings.bundle_id
    assert driver.idle == 0.5


def test_clipboard_never_reads_if_runner_did_not_reach_foreground(monkeypatch):
    driver = ClipboardDriver()
    original = driver.execute_script

    def execute(command, args=None):
        if command == "mobile: activateApp" and args["bundleId"] == "test.runner.xctrunner":
            return
        return original(command, args)

    ticks = iter([0, 6])
    monkeypatch.setattr("app_monitor.iphone_collector.time.monotonic", lambda: next(ticks))
    driver.execute_script = execute
    with pytest.raises(RuntimeError, match="手机剪贴板读写失败"):
        _pasteboard(driver, settings=driver.settings)
    assert not any(command == "mobile: getClipboard" for command, _ in driver.calls)
    assert driver.foreground == driver.settings.bundle_id
    assert driver.idle == 0.5


@pytest.mark.parametrize("failure", ["marker_failure", "changed_detail", "stale_confirmation"])
def test_share_stops_before_copy_if_marker_fails_or_detail_changes(monkeypatch, failure):
    card = PhoneCard("iPhone 13 Pro Max 128G 个人自用", 2000, "翔安区", 20, 400)
    valid = f'''<XCUIElementTypeApplication>
      <XCUIElementTypeTextView name="{card.title}" visible="true" />
      <XCUIElementTypeStaticText name="2000" y="157" visible="true" />
    </XCUIElementTypeApplication>'''

    class Title:
        rect = {"x": 20, "y": 400, "width": 100, "height": 40}

        def is_displayed(self):
            return True

        def get_attribute(self, key):
            return card.title

        def click(self):
            pass

    class Driver:
        page_source = valid

        def find_elements(self, *args):
            return [Title()]

        def execute_script(self, *args):
            raise AssertionError("Must never tap the copy link")

    driver = Driver()
    collector = IPhoneCollector(Settings())
    returned = []
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    monkeypatch.setattr(collector, "_find", lambda *args: Title())
    monkeypatch.setattr(collector, "_return_to_results", lambda *args: returned.append(True))
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source", lambda *args: wait(*args, timeout=0))

    def prepare(*args, **kwargs):
        if failure == "changed_detail":
            driver.page_source = valid.replace(card.title, "iPhone 16 Pro 256G")
            return "marker"
        if failure == "stale_confirmation":
            driver.page_source = valid.replace('</XCUIElementTypeApplication>', '''
              <XCUIElementTypeOther visible="true" name="【闲鱼】https://m.tb.cn/h.old 点击链接直接打开" />
            </XCUIElementTypeApplication>''')
            return "marker"
        raise RuntimeError("手机剪贴板标记写入后回读不一致")

    monkeypatch.setattr("app_monitor.iphone_collector._prepare_pasteboard", prepare)
    with pytest.raises(RuntimeError, match="详情身份发生变化|标记写入后回读不一致|旧的复制确认文案"):
        collector._share_item_id(driver, "device", card)
    assert returned == [True]
