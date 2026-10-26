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
    assert share_confirmation_text(xml) == (url if expected else "")


def share_source(text, *, tag="XCUIElementTypeOther", **attributes):
    import xml.etree.ElementTree as ET

    root = ET.Element("XCUIElementTypeApplication")
    ET.SubElement(root, tag, {"visible": "true", "name": text, **attributes})
    return ET.tostring(root, encoding="unicode")


@pytest.mark.parametrize("text", [
    "https://m.tb.cn/h.example",
    "复制成功 https://m.tb.cn/h.example",
    "[闲鱼] https://m.tb.cn/h.example 点击链接直接打开",
    "闲鱼 https://m.tb.cn/h.example 点\u200b击链\ufeff接直接打开",
    "【闲\u200b鱼】https://m.tb.cn/h.example 点击链接直接打开",
    "复制成功「https://m.tb.cn/h.example」",
    "ｈｔｔｐｓ：／／ｍ．ｔｂ．ｃｎ／h.example",
    "https://m.tb.\u200bcn/h.example",
])
def test_fresh_link_does_not_require_brand_brackets_or_copy_hint(text):
    assert share_confirmation_text(share_source(text), before_source="<root />") == "https://m.tb.cn/h.example"


def test_split_confirmation_nodes_do_not_need_combined_hint_and_url():
    source = '''<root>
      <XCUIElementTypeStaticText name="点击链接直接打开" visible="true" />
      <XCUIElementTypeStaticText name="https://m.tb.cn/h.example" visible="true" />
    </root>'''
    assert share_confirmation_text(source, before_source="<root />") == "https://m.tb.cn/h.example"


def test_existing_link_and_new_link_are_distinguished():
    before = share_source("https://m.tb.cn/h.old")
    assert share_confirmation_text(before, before_source=before) == ""
    after = before.replace('</XCUIElementTypeApplication>', '''
      <XCUIElementTypeOther name="https://m.tb.cn/h.new" visible="true" />
      </XCUIElementTypeApplication>''')
    assert share_confirmation_text(after, before_source=before) == "https://m.tb.cn/h.new"


@pytest.mark.parametrize("source", [
    share_source("https://m.tb.cn/h.example", visible="false"),
    '<root visible="false">' + share_source("https://m.tb.cn/h.example") + '</root>',
    share_source("点击链接直接打开 https://m.tb.cn/h.example", tag="XCUIElementTypeTextView"),
    share_source("https://m.tb.cn.example.com/h.example"),
    share_source("https://example.com/item?id=1091531543430"),
    share_source("https://m.tb.cn/h.one https://m.tb.cn/h.two"),
])
def test_unrelated_hidden_or_ambiguous_links_are_not_copy_results(source):
    assert share_confirmation_text(source, before_source="<root />") == ""


def test_stale_confirmation_ignores_special_brand_symbols_but_not_visibility():
    text = "[闲鱼] https://m.tb.cn/h.old 点\u200b击链\ufeff接直接打开"
    assert share_confirmation_text(share_source(text)) == "https://m.tb.cn/h.old"
    assert share_confirmation_text(share_source(text, visible="false")) == ""
    assert share_confirmation_text(share_source("商品介绍 https://m.tb.cn/h.old")) == ""


def test_visible_alert_link_is_a_confirmation_without_fixed_hint():
    source = '<XCUIElementTypeAlert visible="true">' + share_source("https://m.tb.cn/h.example") + '</XCUIElementTypeAlert>'
    assert share_confirmation_text(source) == "https://m.tb.cn/h.example"


def test_share_link_attributes_and_encoded_query_are_preserved():
    source = share_source("1", label="https://www.goofish.com/item?id=1091531543430&x=%2F")
    assert share_confirmation_text(source, before_source="<root />") == "https://www.goofish.com/item?id=1091531543430&x=%2F"


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
def test_clipboard_fallback_stops_before_recopy_if_marker_fails_or_detail_changes(monkeypatch, failure):
    card = PhoneCard("iPhone 13 Pro Max 128G 个人自用", 2000, "翔安区", 20, 400)
    valid = f'''<XCUIElementTypeApplication>
      <XCUIElementTypeOther name="分享按钮" visible="true" />
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
        taps = 0

        def get_window_size(self):
            return {"width": 390, "height": 844}

        def find_elements(self, *args):
            return [Title()]

        def execute_script(self, *args):
            self.taps += 1
            assert self.taps == 1, "The clipboard fallback must never copy after failed verification"

    driver = Driver()
    collector = IPhoneCollector(Settings())
    returned = []
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    monkeypatch.setattr(collector, "_find", lambda *args: Title())
    monkeypatch.setattr(collector, "_return_to_results", lambda *args: returned.append(True))
    monkeypatch.setattr(collector, "_close_share_sheet", lambda *args: None)
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source", lambda *args, **kwargs: wait(*args, **(kwargs | {"timeout": 0})))

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


class ShareSheetDriver:
    """Model the copy image that XCTest wrongly calls invisible."""

    card = PhoneCard("iPhone 14 256G 国行白色个人自用", 1950, "思明区", 20, 400)
    detail = f'''<XCUIElementTypeApplication>
      <XCUIElementTypeOther name="分享按钮" visible="true" />
      <XCUIElementTypeTextView name="{card.title}" visible="true" />
      <XCUIElementTypeStaticText name="1950" y="157" visible="true" />
    </XCUIElementTypeApplication>'''

    def __init__(self, *, opens_after=1, changed=False):
        self.opens_after = opens_after
        self.changed = changed
        self.openings = 0
        self.panel = False
        self.copied = False
        self.queries = []
        self.copy_rects = [{"x": 120, "y": 650, "width": 60, "height": 80}]

    @property
    def page_source(self):
        if self.copied:
            return '''<XCUIElementTypeApplication><XCUIElementTypeOther visible="true"
                name="【闲鱼】https://www.goofish.com/item?id=1091531543430 点击链接直接打开" />
                </XCUIElementTypeApplication>'''
        if self.changed and self.openings:
            return self.detail.replace(self.card.title, "iPhone 16 Pro 128G 另一件商品")
        return self.detail

    def get_window_size(self):
        return {"width": 390, "height": 844}

    def find_elements(self, strategy, query):
        from types import SimpleNamespace

        self.queries.append(query)
        if "分享按钮" in query:
            return [SimpleNamespace(click=self.open)]
        if not self.panel or "visible == true" in query:
            return []
        return [SimpleNamespace(rect=rect) for rect in self.copy_rects]

    def open(self):
        self.openings += 1
        self.panel = self.openings >= self.opens_after

    def execute_script(self, command, args):
        assert command == "mobile: tap"
        if args["y"] == 200:
            self.panel = False
        else:
            assert self.panel and args == {"x": 150, "y": 722}
            self.copied = True
            self.panel = False


def test_invisible_copy_image_on_screen_is_copied_and_confirmed_without_clipboard():
    driver = ShareSheetDriver()
    actions = []
    text = IPhoneCollector(Settings())._copy_from_share(driver, actions, detail_card=driver.card)
    assert "1091531543430" in text and driver.copied and driver.openings == 1
    assert any(a["action"] == "定位复制链接" for a in actions)
    assert any(a["action"] == "点击复制链接" for a in actions)


def test_clicked_copy_without_new_url_is_distinct_from_missing_button(monkeypatch):
    class Driver(ShareSheetDriver):
        @property
        def page_source(self):
            # The old bare URL is visible even before copying; never call it fresh.
            return super().page_source if not self.copied else share_source("https://m.tb.cn/h.old")

    driver = Driver()
    driver.detail = driver.detail.replace('</XCUIElementTypeApplication>', '''
      <XCUIElementTypeOther name="https://m.tb.cn/h.old" visible="true" />
      </XCUIElementTypeApplication>''')
    collector = IPhoneCollector(Settings())
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source",
                        lambda *args, **kwargs: wait(*args, **(kwargs | {"timeout": 0})))
    actions = []
    assert collector._copy_from_share(driver, actions, detail_card=driver.card) == ""
    assert driver.copied and driver.openings == 1
    assert any(a["action"] == "点击复制链接" for a in actions)
    assert any(a["action"] == "切换链接读取方式" for a in actions)


@pytest.mark.parametrize("bad_rect", [
    {"x": 120, "y": 900, "width": 60, "height": 80},
    {"x": -100, "y": 650, "width": 60, "height": 80},
    {"x": 120, "y": 650, "width": 60, "height": 0},
    {"x": 350, "y": 650, "width": 60, "height": 80},
    {"x": 120, "y": 800, "width": 60, "height": 80},
])
def test_offscreen_or_empty_copy_nodes_are_not_tapped(bad_rect):
    driver = ShareSheetDriver()
    driver.panel = True
    driver.copy_rects = [bad_rect]
    assert IPhoneCollector._copy_link_rect(driver) is None
    driver.copy_rects.append({"x": 120, "y": 650, "width": 60, "height": 80})
    assert IPhoneCollector._copy_link_rect(driver) == driver.copy_rects[-1]


def test_copy_reopens_share_once_when_first_tap_did_not_open_it(monkeypatch):
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = ShareSheetDriver(opens_after=2)
    actions = []
    assert "1091531543430" in IPhoneCollector(Settings())._copy_from_share(
        driver, actions, detail_card=driver.card)
    assert driver.openings == 2 and driver.copied
    assert sum(a["action"] == "重试分享面板" for a in actions) == 1


def test_copy_failure_is_bounded_and_does_not_claim_the_menu_has_no_link(monkeypatch):
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = ShareSheetDriver(opens_after=3)
    with pytest.raises(RuntimeError, match="未能定位复制链接按钮.*重试"):
        IPhoneCollector(Settings())._copy_from_share(driver, [], detail_card=driver.card)
    assert driver.openings == 2 and not driver.copied


def test_copy_does_not_reopen_share_if_detail_changed(monkeypatch):
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = ShareSheetDriver(opens_after=2, changed=True)
    collector = IPhoneCollector(Settings())
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source",
                        lambda *args, **kwargs: wait(*args, **(kwargs | {"timeout": 0})))
    with pytest.raises(RuntimeError, match="重新打开分享面板前详情发生变化"):
        collector._copy_from_share(driver, [], detail_card=driver.card)
    assert driver.openings == 1 and not driver.copied


def test_share_cleanup_also_handles_invisible_copy_image():
    driver = ShareSheetDriver()
    driver.panel = True
    IPhoneCollector._close_share_sheet(driver)
    assert not driver.panel and not driver.copied
