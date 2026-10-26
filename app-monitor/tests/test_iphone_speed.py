from types import SimpleNamespace

import pytest

from app_monitor.config import Settings
from app_monitor.iphone_collector import IPhoneCollector, PhoneCard, _PageWaitTimeout, current_detail, detail_matches, parse_cards


@pytest.fixture
def share_phone(monkeypatch):
    card = PhoneCard("iPhone 12 Pro 128G 国行个人自用", 1350, "集美区", 20, 400)
    full = f'''<XCUIElementTypeApplication>
      <XCUIElementTypeOther name="分享按钮" visible="true" />
      <XCUIElementTypeOther name="用户头像" x="12" y="99" width="32" height="32" visible="true" />
      <XCUIElementTypeOther name="测试卖家" x="52" y="99" width="80" height="32" visible="true" />
      <XCUIElementTypeTextView name="{card.title}，新原装电芯100健康" visible="true" />
      <XCUIElementTypeStaticText name="1350" y="157" visible="true" />
    </XCUIElementTypeApplication>'''
    confirmation = '''<XCUIElementTypeApplication><XCUIElementTypeOther visible="true"
      name="【闲鱼】https://www.goofish.com/item?id=1091531543430 点击链接直接打开" /></XCUIElementTypeApplication>'''
    events = []
    state = SimpleNamespace(screen="detail", native=True, old_popup=False, copies=0,
                            clipboard="old-link", second_copy=True, switched=False, change_on_marker=False,
                            confirmation=confirmation)

    class Element:
        rect = {"x": 20, "y": 400, "width": 100, "height": 30}

        def click(self):
            state.screen = "panel"

    class Driver:
        def get_window_size(self):
            return {"width": 390, "height": 844}

        @property
        def page_source(self):
            if state.switched:
                return full.replace('name="1350"', 'name="1500"')
            if state.screen == "confirmation" or state.screen == "panel" and state.old_popup:
                return state.confirmation
            return full

        def find_elements(self, *args):
            return [Element()] if state.screen == "panel" else []

        def execute_script(self, command, args):
            assert command == "mobile: tap" and args["y"] == 427
            state.copies += 1
            events.append(f"copy{state.copies}")
            if state.copies == 1 or state.second_copy:
                state.clipboard = "https://www.goofish.com/item?id=1091531543430"
            state.screen = "confirmation" if state.native else "panel"

    driver = Driver()
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_candidate_title",
                        lambda *args: SimpleNamespace(click=lambda: setattr(state, "screen", "detail")))
    monkeypatch.setattr(collector, "_find", lambda *args: Element())
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    monkeypatch.setattr(collector, "_close_share_sheet", lambda *args: setattr(state, "screen", "detail"))
    monkeypatch.setattr(collector, "_return_to_results", lambda *args: events.append("returned"))
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source",
                        lambda *args, **kwargs: wait(*args, **(kwargs | {"timeout": 0})))

    def prepare(*args, **kwargs):
        assert state.screen == "detail"
        events.append("marker")
        state.clipboard = "marker"
        state.switched = state.change_on_marker
        return "marker"

    def read(*args, **kwargs):
        assert state.copies == 2 and "marker" in events
        events.append("read")
        return state.clipboard

    monkeypatch.setattr("app_monitor.iphone_collector._prepare_pasteboard", prepare)
    monkeypatch.setattr("app_monitor.iphone_collector._pasteboard", read)
    return collector, driver, card, state, events


def test_native_confirmation_never_prepares_or_reads_clipboard(share_phone):
    collector, driver, card, _, events = share_phone
    assert collector._share_item_id(driver, "test", card) == "1091531543430"
    assert events == ["copy1", "returned"]


def test_new_url_without_fixed_copy_wording_uses_fast_path(share_phone):
    collector, driver, card, state, events = share_phone
    state.confirmation = '''<XCUIElementTypeApplication>
      <XCUIElementTypeStaticText name="https://www.goofish.com/item?id=1091531543430" visible="true" />
      </XCUIElementTypeApplication>'''
    assert collector._share_item_id(driver, "test", card) == "1091531543430"
    assert events == ["copy1", "returned"]


def test_clipboard_fallback_recopies_after_marker(share_phone):
    collector, driver, card, state, events = share_phone
    state.native = False
    assert collector._share_item_id(driver, "test", card) == "1091531543430"
    assert events == ["copy1", "marker", "copy2", "read", "returned"]


def test_summary_difference_and_old_list_price_do_not_block_detail_or_clipboard(share_phone):
    collector, driver, card, state, events = share_phone
    state.native = False
    summary = PhoneCard("苹果12Pro原装128G成色95新性价比高便宜出", 1600, card.region, card.x, card.y)
    observed = {}
    checked = []
    assert collector._share_item_id(driver, "test", summary, seller_info=observed,
                                   check_detail=lambda item: checked.append(item)) == "1091531543430"
    assert observed["price"] == checked[0]["price"] == 1350
    assert "新原装电芯" in checked[0]["description"]
    assert observed["title"] != summary.title
    assert events == ["copy1", "marker", "copy2", "read", "returned"]


def test_current_detail_reads_split_body_and_uses_one_actual_price():
    source = '''<XCUIElementTypeApplication>
      <XCUIElementTypeOther name="分享按钮" visible="true" />
      <XCUIElementTypeOther name="用户头像" y="99" height="32" visible="true" />
      <XCUIElementTypeOther name="999999" y="108" visible="true" />
      <XCUIElementTypeStaticText name="￥2,180.00" y="157" visible="true" />
      <XCUIElementTypeOther name="2180元" y="160" visible="true" />
      <XCUIElementTypeStaticText name="2400" y="157" visible="false" />
      <XCUIElementTypeTextView name="苹果 iPhone 13 Pro Max 128G 白色 国行" visible="true" />
      <XCUIElementTypeTextView name="苹果 iPhone 13 Pro Max 128G 白色 国行" visible="true" />
      <XCUIElementTypeTextView name="电池健康81%，边框有磕碰，具体看图，适合日常使用或备用机" visible="true" />
    </XCUIElementTypeApplication>'''
    item = current_detail(source)
    assert item["price"] == 2180 and "具体看图" in item["description"]
    assert item["title"].startswith("苹果 iPhone 13 Pro Max")
    assert detail_matches(source, PhoneCard(item["description"], item["price"], "", 0, 0))
    with pytest.raises(ValueError, match="多个数值"):
        current_detail(source.replace('name="2400" y="157" visible="false"', 'name="2400" y="157" visible="true"'))


@pytest.mark.parametrize("missing", ["body", "price", "detail_page"])
def test_detail_readiness_does_not_substitute_list_content_for_missing_fields(missing):
    source = '''<XCUIElementTypeApplication>
      <XCUIElementTypeOther name="分享按钮" visible="true" />
      <XCUIElementTypeStaticText name="1350" y="157" visible="true" />
      <XCUIElementTypeTextView name="iPhone 12 Pro 128G 实际详情" visible="true" />
    </XCUIElementTypeApplication>'''
    token = {"body": 'name="iPhone 12 Pro 128G 实际详情"', "price": 'name="1350"', "detail_page": 'name="分享按钮"'}[missing]
    with pytest.raises(ValueError):
        current_detail(source.replace(token, 'name=""'))


def test_clipboard_fallback_rejects_first_copy_when_second_copy_fails(share_phone):
    collector, driver, card, state, events = share_phone
    state.native = state.second_copy = False
    with pytest.raises(RuntimeError, match="未产生新链接"):
        collector._share_item_id(driver, "test", card)
    assert events[:3] == ["copy1", "marker", "copy2"] and events[-1] == "returned"


def test_clipboard_app_switch_rechecks_detail_before_recopy(share_phone):
    collector, driver, card, state, events = share_phone
    state.native = False
    state.change_on_marker = True
    with pytest.raises(_PageWaitTimeout, match="详情身份发生变化"):
        collector._share_item_id(driver, "test", card)
    assert events == ["copy1", "marker", "returned"]


def test_old_native_confirmation_is_rejected_before_tap(share_phone):
    collector, driver, card, state, events = share_phone
    state.old_popup = True
    with pytest.raises(RuntimeError, match="旧的复制确认"):
        collector._share_item_id(driver, "test", card)
    assert events == ["returned"]


def test_rejected_detail_returns_without_seller_or_share_work(share_phone, monkeypatch):
    collector, driver, card, _, events = share_phone
    rejection = {"reason": "扣费后价格不符合", "matched": False}
    seller = {}
    monkeypatch.setattr(collector, "_find", lambda *args: pytest.fail("判价排除前不需要查找分享入口"))

    def check(item):
        assert "新原装电芯" in item["description"] and item["price"] == 1350
        return rejection

    assert collector._share_item_id(driver, "test", card, seller_info=seller, check_detail=check) == ""
    assert seller["detail_rejection"] == rejection and events == ["returned"]


def test_current_detail_price_reaches_gate_even_when_list_price_differs(share_phone):
    collector, driver, card, state, events = share_phone
    checked = []
    assert collector._share_item_id(driver, "test", card,
                                   check_detail=lambda item: checked.append(item)) == "1091531543430"
    assert len(checked) == 1
    state.switched = True
    def reject_current(item):
        assert item['price'] == 1500
        return {'matched': False, 'code': 'price_high', 'reason': '详情价超出'}
    assert collector._share_item_id(driver, "test", card, check_detail=reject_current) == ""
    assert events.count("copy1") == 1


def list_source(y=400):
    return f'''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeButton name="search more" y="63" visible="true" />
      <XCUIElementTypeStaticText name="新发" y="150" visible="false" />
      <XCUIElementTypeStaticText name="筛选" y="150" visible="false" />
      <XCUIElementTypeCell>
        <XCUIElementTypeStaticText name="iPhone 12 Pro 128G 自用" x="10" y="{y}" visible="true" />
        <XCUIElementTypeStaticText name="¥1350" x="10" y="{y + 70}" visible="true" />
      </XCUIElementTypeCell>
    </XCUIElementTypeApplication>'''


@pytest.mark.parametrize("fallback", [False, True])
def test_scroll_checks_progress_and_reuses_the_verified_snapshot(monkeypatch, fallback):
    before, after = list_source(), list_source(200)
    calls = []

    class Driver:
        source = before

        @property
        def page_source(self):
            return self.source

        def execute_script(self, command, args):
            calls.append(command)
            if command == "mobile: dragFromToWithVelocity":
                # The drag stays inside content and preserves viewport overlap.
                assert 844 * .22 <= args["toY"] < args["fromY"] <= 844 * .86
                if not fallback:
                    self.source = after
            else:
                assert command == "mobile: swipe"
                self.source = after

    collector = IPhoneCollector(Settings())
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source",
                        lambda *args, **kwargs: wait(*args, **(kwargs | {"timeout": 0})))
    assert collector._scroll_results(Driver(), before) == after
    assert len(calls) == 1 + fallback
    assert parse_cards(after)[0].y == 200


def test_scroll_does_not_fall_back_to_swiping_a_detail_page(monkeypatch):
    calls = []

    class Driver:
        page_source = '<XCUIElementTypeApplication><XCUIElementTypeButton name="返回按钮" visible="true" /></XCUIElementTypeApplication>'

        def execute_script(self, command, args):
            calls.append(command)

    collector = IPhoneCollector(Settings())
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source",
                        lambda *args, **kwargs: wait(*args, **(kwargs | {"timeout": 0})))
    with pytest.raises(RuntimeError, match="离开搜索结果页"):
        collector._scroll_results(Driver(), list_source())
    assert calls == ["mobile: dragFromToWithVelocity"]


def test_scroll_recovers_unsupported_drag_and_preserves_bottom_boundary(monkeypatch):
    calls = []

    class Driver:
        page_source = list_source()

        def execute_script(self, command, args):
            calls.append(command)
            if command == "mobile: dragFromToWithVelocity":
                raise RuntimeError("unsupported gesture")

    collector = IPhoneCollector(Settings())
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, "_wait_for_source",
                        lambda *args, **kwargs: wait(*args, **(kwargs | {"timeout": 0})))
    assert collector._scroll_results(Driver(), list_source()) == list_source()
    assert calls == ["mobile: dragFromToWithVelocity", "mobile: swipe"]


@pytest.mark.parametrize("visit_detail", [False, True])
def test_list_snapshots_are_reused_until_a_detail_visit_invalidates_them(monkeypatch, visit_detail):
    class Driver:
        reads = 0

        @property
        def page_source(self):
            self.reads += 1
            return list_source()

        def quit(self):
            pass

    driver = Driver()
    collector = IPhoneCollector(Settings(ios_wda_bundle_id="test.wda"))
    monkeypatch.setattr("appium.webdriver.Remote", lambda *args, **kwargs: driver)
    monkeypatch.setattr("app_monitor.iphone_collector.ensure_appium_server", lambda _: None)
    monkeypatch.setattr(collector, "_search", lambda *args: True)
    monkeypatch.setattr(collector, "_apply_model", lambda *args: [])
    monkeypatch.setattr(collector, "_apply_storage", lambda *args: [])
    monkeypatch.setattr(collector, "_sort_new", lambda *args: None)
    viewports = []

    def scroll(d, source):
        viewports.append(source)
        return list_source().replace("自用", f"自用{len(viewports)}")

    def reject(d, udid, card, actions, seller_info, **kwargs):
        seller_info["detail_rejection"] = {"reason": "详情价格不符合"}
        return ""

    monkeypatch.setattr(collector, "_scroll_results", scroll)
    monkeypatch.setattr(collector, "_share_item_id", reject)
    result = collector._collect("test", "", "iPhone 12 Pro", "厦门", [], ["128"], 2, lambda *args: visit_detail)
    assert driver.reads == (3 if visit_detail else 1) and len(viewports) == 2
    assert result.passes[0]["cards"] == 3 and result.passes[0]["error"] is None
