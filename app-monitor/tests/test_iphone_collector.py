import pytest

from app_monitor.iphone_collector import (
    IPhoneCollector, PhoneCard, checked_storages, detail_matches, detail_seller, home_search_city,
    home_tabs, item_id_from_share, parse_cards, result_bar_label, result_region_chip,
    active_storage_chip,
    is_search_results,
    search_entry_points,
    recognize_seller_badges,
)
from app_monitor.config import Settings


def test_card_parser_keeps_title_and_price_inside_same_card():
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeOther>
        <XCUIElementTypeCell x="0" y="400" width="210" height="300">
          <XCUIElementTypeStaticText value="iPhone 17 256G 黑色 自用" x="8" y="500" />
          <XCUIElementTypeStaticText value="¥3980" x="8" y="600" />
          <XCUIElementTypeStaticText value="思明区" x="8" y="650" />
        </XCUIElementTypeCell>
        <XCUIElementTypeCell x="215" y="400" width="210" height="300">
          <XCUIElementTypeStaticText value="iPhone 17 512G 紫色 国行" x="220" y="510" />
          <XCUIElementTypeStaticText value="¥4700" x="220" y="610" />
          <XCUIElementTypeStaticText value="湖里区" x="220" y="660" />
        </XCUIElementTypeCell>
      </XCUIElementTypeOther>
    </XCUIElementTypeApplication>"""
    cards = parse_cards(source, "厦门")
    assert [(card.title, card.price, card.region) for card in cards] == [
        ("iPhone 17 256G 黑色 自用", 3980, "思明区"),
        ("iPhone 17 512G 紫色 国行", 4700, "湖里区"),
    ]


def test_share_parser_uses_goofish_item_id_not_tracking_id():
    share = "来闲鱼看看 https://m.tb.cn/h.ABCD123"
    page = 'trackingId=9999999999999&amp;url=https://h5.m.goofish.com/item?id=1091531543430&amp;source=share'
    assert item_id_from_share(share, page) == "1091531543430"
    assert item_id_from_share(share, "trackingId=9999999999999") == ""


def test_card_region_can_be_sibling_of_title_and_price_group():
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeStaticText value="厦门" x="30" y="55" />
      <XCUIElementTypeCell x="0" y="300">
        <XCUIElementTypeOther>
          <XCUIElementTypeStaticText value="iPhone 17 256G 自用黑色" x="15" y="400" />
          <XCUIElementTypeStaticText value="¥3980" x="15" y="500" />
        </XCUIElementTypeOther>
        <XCUIElementTypeStaticText value="思明区" x="15" y="550" />
      </XCUIElementTypeCell>
    </XCUIElementTypeApplication>"""
    cards = parse_cards(source, "厦门")
    assert len(cards) == 1 and cards[0].region == "思明区"


def test_real_device_price_other_and_hidden_card():
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeCell>
        <XCUIElementTypeStaticText name="iPhone 17 256G 自用黑色" x="12" y="700" />
        <XCUIElementTypeOther name="3980元" x="12" y="750" visible="true" />
        <XCUIElementTypeStaticText name="思明区" x="12" y="790" />
      </XCUIElementTypeCell>
      <XCUIElementTypeCell>
        <XCUIElementTypeStaticText name="iPhone 17 512G 自用紫色" x="220" y="1100" />
        <XCUIElementTypeOther name="4700元" x="220" y="1150" visible="false" />
      </XCUIElementTypeCell>
    </XCUIElementTypeApplication>"""
    assert [(card.price, card.region) for card in parse_cards(source, "厦门")] == [(3980, "思明区")]


def test_detail_matches_text_view_with_extra_description_and_same_price():
    card = PhoneCard("几乎全新i\u200bPhone 17，蓝色，256G", 4819, "郑州", 20, 700)
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeStaticText name="4819" y="157" />
      <XCUIElementTypeTextView name="几乎全新iPhone 17，蓝色，256G。保修到明年，支持自提。" />
    </XCUIElementTypeApplication>"""
    assert detail_matches(source, card)
    assert not detail_matches(source.replace('name="4819"', 'name="4919"'), card)


def seller_source(badge="", seller="测试卖家"):
    return f'''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeOther name="分享按钮" x="354" y="53" width="24" height="24" visible="true" />
      <XCUIElementTypeOther name="用户头像" x="12" y="99" width="32" height="32" visible="true" />
      <XCUIElementTypeOther name="{seller}" x="52" y="99" width="80" height="32" visible="true" />
      {badge}
      <XCUIElementTypeStaticText name="刚刚来过" x="260" y="108" width="50" height="15" visible="true" />
      <XCUIElementTypeStaticText name="厦门" x="340" y="108" width="28" height="15" visible="true" />
      <XCUIElementTypeTextView name="描述里提到鱼小铺不代表卖家开店" x="12" y="300" width="360" height="80" visible="true" />
    </XCUIElementTypeApplication>'''


@pytest.mark.parametrize("badge, expected", [
    ('<XCUIElementTypeImage name="鱼小铺" x="136" y="108" width="40" height="15" visible="true" />', True),
    ('<XCUIElementTypeStaticText name="鱼小铺" x="136" y="108" width="40" height="15" visible="true" />', True),
    ("", False),
    ('<XCUIElementTypeImage x="136" y="108" width="40" height="15" visible="true" />', None),
    ('<XCUIElementTypeImage name="鱼小铺" x="136" y="108" width="40" height="15" visible="false" />', False),
    ('<XCUIElementTypeStaticText name="鱼小铺" x="136" y="600" width="40" height="15" visible="true" />', False),
])
def test_seller_is_bound_to_detail_avatar_and_adjacent_badge(badge, expected):
    assert detail_seller(seller_source(badge)) == {"seller_name": "测试卖家", "is_fish_shop": expected}


def test_missing_seller_header_is_unknown_instead_of_non_shop():
    source = seller_source().replace('name="用户头像"', 'name="其他图片"')
    assert detail_seller(source) == {"seller_name": "", "is_fish_shop": None}
    # A result list must not provide seller detail metadata even if matching text is present.
    assert detail_seller(seller_source().replace('name="分享按钮"', 'name="图片搜索"'))["seller_name"] == ""


@pytest.mark.parametrize("text, shop", [("鱼 小 铺", True), ("减碳榜样", False), ("鱼小辅", None), ("", None)])
def test_unnamed_seller_badge_needs_ocr_evidence(text, shop):
    source = seller_source('<XCUIElementTypeImage x="136" y="108" width="40" height="15" visible="true" />')
    assert detail_seller(source, {(136, 108, 40, 15): text})["is_fish_shop"] is shop


def test_badge_ocr_crops_logical_bounds_and_removes_temporary_image(monkeypatch):
    import io
    from pathlib import Path
    from types import SimpleNamespace
    import xml.etree.ElementTree as ET
    from PIL import Image

    source = seller_source('<XCUIElementTypeImage x="136" y="108" width="40" height="15" visible="true" />')
    image = Image.new("RGB", (780, 1688), "white")  # Screenshot scale is independent of screen points.
    png = io.BytesIO()
    image.save(png, format="PNG")
    files = []

    def recognize(command, **kwargs):
        path = Path(command[-1])
        files.append(path)
        with Image.open(path) as crop:
            assert crop.size == (264, 114)  # Badge + two points padding, scaled to screenshot, then enlarged.
        return SimpleNamespace(returncode=0, stdout="0\t0\t0\t1\t1\t鱼小铺\n")

    monkeypatch.setattr("app_monitor.iphone_collector.subprocess.run", recognize)
    badges = list(ET.fromstring(source).iter("XCUIElementTypeImage"))
    assert recognize_seller_badges(png.getvalue(), source, badges) == {(136, 108, 40, 15): "鱼小铺"}
    assert files and all(not path.exists() for path in files)


def test_picker_checkmark_and_result_bar_are_checked_independently():
    picker = """<XCUIElementTypeApplication>
      <XCUIElementTypeOther><XCUIElementTypeStaticText name="256GB" />
        <XCUIElementTypeImage name="search_checkbox_sel" /></XCUIElementTypeOther>
      <XCUIElementTypeOther><XCUIElementTypeStaticText name="512GB" /></XCUIElementTypeOther>
    </XCUIElementTypeApplication>"""
    assert checked_storages(picker) == {"256GB"}
    result = """<XCUIElementTypeApplication>
      <XCUIElementTypeStaticText name="新发" x="240" y="405" visible="true" />
      <XCUIElementTypeStaticText name="厦门" x="304" y="405" visible="true" />
      <XCUIElementTypeStaticText name="筛选" x="383" y="405" visible="true" />
      <XCUIElementTypeStaticText name="512GB" x="373" y="446" visible="true" />
      <XCUIElementTypeStaticText name="256GB" x="45" y="280" visible="true" />
    </XCUIElementTypeApplication>"""
    assert result_bar_label(result, "厦门")
    assert result_bar_label(result, "512GB")
    assert not result_bar_label(result, "256GB")


def home_source(second="厦门", header=False):
    return f'''<XCUIElementTypeApplication height="932" width="430">
      <XCUIElementTypeButton name="首页" x="8" y="849" width="79" visible="true" />
      <XCUIElementTypeButton name="{second}" label="{second}" value="1" x="86" y="849" width="80" visible="true" />
      <XCUIElementTypeButton name="消息" x="264" y="849" width="80" visible="true" />
      <XCUIElementTypeButton name="我的" x="343" y="849" width="79" visible="true" />
      <XCUIElementTypeStaticText name="厦门" x="11" y="{'67' if header else '650'}" width="33" visible="true" />
      <XCUIElementTypeButton name="搜索词" x="70" y="61" width="348" visible="true" />
    </XCUIElementTypeApplication>'''


def test_home_city_requires_second_bottom_tab_and_city_beside_search():
    assert home_tabs(home_source())[1].get("name") == "厦门"
    assert home_tabs(home_source("深圳"))[1].get("name") != "厦门"
    assert not home_search_city(home_source(), "厦门")  # Card text is not header evidence.
    assert home_search_city(home_source(header=True), "厦门")


def test_select_home_city_returns_via_known_back_buttons_then_checks_search_header(monkeypatch):
    class Driver:
        position = 0

        @property
        def page_source(self):
            return '<XCUIElementTypeApplication />' if self.position < 2 else home_source(header=self.position == 3)

    driver = Driver()
    clicked = []

    class Element:
        rect = {"y": 849}

        def __init__(self, name):
            self.name = name

        def click(self):
            clicked.append(self.name)
            driver.position += 1

    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_find", lambda d, name, types=():
                        Element(name) if (name == "返回" and d.position < 2)
                        or (name == "厦门" and d.position == 2) else None)
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    actions = []
    assert collector._select_home_city(driver, "厦门", actions)
    assert clicked == ["返回", "返回", "厦门"]
    assert actions[-1]["action"] == "搜索栏地区确认"


@pytest.mark.parametrize("inherited", [True, False])
def test_collection_skips_region_picker_only_after_search_confirmation(monkeypatch, inherited):
    class Driver:
        page_source = '<XCUIElementTypeApplication />'

        def quit(self):
            pass

    monkeypatch.setattr("appium.webdriver.Remote", lambda *args, **kwargs: Driver())
    monkeypatch.setattr("app_monitor.iphone_collector.ensure_appium_server", lambda _: None)
    collector = IPhoneCollector(Settings(ios_wda_bundle_id="test.wda"))
    monkeypatch.setattr(collector, "_search", lambda *args: inherited)
    region_calls = []
    monkeypatch.setattr(collector, "_apply_region", lambda *args: region_calls.append(args) or ["区域", "厦门", "查看"])
    monkeypatch.setattr(collector, "_apply_model", lambda *args: ["iPhone 17", "确定"])
    monkeypatch.setattr(collector, "_apply_storage", lambda *args: ["256GB", "确定"])
    monkeypatch.setattr(collector, "_sort_new", lambda *args: None)
    result = collector._collect("test", "", "iPhone 17", "厦门", ["区域", "厦门", "查看"],
                                ["256"], 0, lambda *args: False)
    assert result.region_applied
    assert len(region_calls) == (0 if inherited else 1)
    assert any(action["action"] == ("跳过地区筛选" if inherited else "地区筛选")
               for action in result.passes[0]["actions"])


def test_result_region_chip_reads_current_city_for_filter_fallback():
    source = '''<XCUIElementTypeApplication>
      <XCUIElementTypeStaticText name="新发" x="240" y="405" width="29" visible="true" />
      <XCUIElementTypeStaticText name="深圳" x="304" y="405" width="29" visible="true" />
      <XCUIElementTypeStaticText name="筛选" x="383" y="405" width="29" visible="true" />
      <XCUIElementTypeStaticText name="厦门" x="304" y="700" width="29" visible="true" />
    </XCUIElementTypeApplication>'''
    assert result_region_chip(source) == "深圳"


def storage_bar_source(visible="true", chip_x=373):
    return f'''<XCUIElementTypeApplication>
      <XCUIElementTypeStaticText name="新发" x="240" y="155" visible="{visible}" />
      <XCUIElementTypeStaticText name="筛选" x="383" y="155" visible="{visible}" />
      <XCUIElementTypeStaticText name="256GB" x="{chip_x}" y="196" visible="{visible}" />
      <XCUIElementTypeStaticText name="512GB" x="220" y="700" visible="true" />
    </XCUIElementTypeApplication>'''


def test_storage_chip_uses_filter_row_not_fixed_horizontal_position_or_card_text():
    source = storage_bar_source(chip_x=120)
    assert active_storage_chip(source) == "256GB"
    assert result_bar_label(source, "256GB")
    assert not result_bar_label(source, "512GB")
    assert active_storage_chip(storage_bar_source(visible="false")) is None


def test_storage_switch_reveals_collapsed_filter_bar_before_locating_chip(monkeypatch):
    class Driver:
        page_source = storage_bar_source(visible="false")
        gestures = []

        def execute_script(self, command, payload):
            self.gestures.append((command, payload))
            self.page_source = storage_bar_source()

    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = Driver()
    current, restored = IPhoneCollector(Settings())._reveal_storage_chip(driver)
    assert current == "256GB" and restored == ["下滑恢复筛选栏"]
    assert driver.gestures[0][1]["direction"] == "down"


def test_storage_recovery_stops_on_detail_page_without_swiping_or_clicking():
    class Driver:
        page_source = '<XCUIElementTypeApplication><XCUIElementTypeStaticText name="512GB" y="196" visible="true" /></XCUIElementTypeApplication>'

        def execute_script(self, *args):
            raise AssertionError("Must not operate on an unrecognized page")

    with pytest.raises(RuntimeError, match="缺少搜索结果排序栏"):
        IPhoneCollector(Settings())._reveal_storage_chip(Driver())


def scrolled_result_source():
    return storage_bar_source(visible="false").replace(
        '</XCUIElementTypeApplication>',
        '<XCUIElementTypeButton name="search more" y="63" visible="true" /></XCUIElementTypeApplication>',
    )


def test_collapsed_sort_bar_still_identifies_list_but_not_detail_or_modal():
    source = scrolled_result_source()
    assert is_search_results(source)
    for control in ("分享按钮", "返回按钮", "确定"):
        assert not is_search_results(source.replace(
            '</XCUIElementTypeApplication>',
            f'<XCUIElementTypeButton name="{control}" visible="true" /></XCUIElementTypeApplication>',
        ))


def test_return_clicks_detail_back_once_and_accepts_collapsed_result_bar(monkeypatch):
    class Driver:
        page_source = '<XCUIElementTypeApplication />'
        backs = 0

    driver = Driver()

    class Back:
        def click(self):
            driver.backs += 1
            driver.page_source = scrolled_result_source()

    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_close_share_sheet", lambda _: None)
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    monkeypatch.setattr(collector, "_find", lambda d, name, types=():
                        (Back() if name == "返回按钮" else object())
                        if name in {"分享按钮", "返回按钮"} and not driver.backs else None)
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    actions = []
    collector._return_to_results(driver, actions)
    assert driver.backs == 1
    assert actions[-1]["action"] == "确认搜索列表" and actions[-1]["success"]
    assert "收起" in actions[-1]["method"]


@pytest.mark.parametrize("clipboard", ["https://www.goofish.com/item?id=1091531543430", "copy-marker"])
def test_share_keeps_copied_id_when_returned_sort_bar_is_hidden(monkeypatch, clipboard):
    card = PhoneCard("iPhone 17 512G 个人自用", 3200, "湖里区", 220, 700)

    class Element:
        rect = {"x": 220, "y": 700, "width": 100, "height": 30}

        def is_displayed(self):
            return True

        def get_attribute(self, key):
            return card.title if key == "name" else ""

        def click(self):
            pass

    class Driver:
        page_source = seller_source('<XCUIElementTypeImage name="鱼小铺" x="136" y="108" width="40" height="15" visible="true" />')

        def find_elements(self, *args):
            return [Element()]

        def execute_script(self, *args):
            pass

    driver = Driver()
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_find", lambda *args: Element())
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    monkeypatch.setattr(collector, "_close_share_sheet", lambda d: setattr(d, "page_source", scrolled_result_source()))
    monkeypatch.setattr("app_monitor.iphone_collector.detail_matches", lambda *args: True)
    monkeypatch.setattr("app_monitor.iphone_collector._prepare_pasteboard", lambda _: "copy-marker")
    monkeypatch.setattr("app_monitor.iphone_collector._pasteboard", lambda _: clipboard)
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    actions = []
    if clipboard == "copy-marker":
        with pytest.raises(RuntimeError, match="剪贴板未产生新链接"):
            collector._share_item_id(driver, "test", card, actions)
    else:
        seller = {}
        assert collector._share_item_id(driver, "test", card, actions, seller) == "1091531543430"
        assert seller["seller_name"] == "测试卖家" and seller["is_fish_shop"] is True
        assert "description" in seller
        assert any(action["action"] == "取得商品链接" for action in actions)
    assert actions[-1]["action"] == "确认搜索列表"


def small_phone_home_source():
    return '''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeButton name="首页" x="8" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="同城" x="78" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="消息" x="240" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="我的" x="310" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="ipad pro2018" traits="SearchField" x="8" y="97" width="374" height="36" visible="true" />
      <XCUIElementTypeStaticText name="ipad pro2018" x="49" y="99" width="92" height="32" visible="true" />
      <XCUIElementTypeStaticText name="集市补贴中" x="71" y="52" width="80" height="40" visible="true" />
    </XCUIElementTypeApplication>'''


def test_search_entry_prefers_actual_hotword_center_on_smaller_phone():
    points = search_entry_points(small_phone_home_source())
    assert (points[0]["x"], points[0]["y"]) == (95, 115)
    assert all(point["target"] == "ipad pro2018" for point in points)
    assert all(point["x"] <= 141 for point in points)
    rotating = small_phone_home_source().replace('Button name="ipad pro2018"', 'Button name="轮播中的下一个词"')
    assert search_entry_points(rotating)[0] == points[0]


def test_search_input_relocates_and_retries_when_first_tap_does_nothing(monkeypatch):
    class Field:
        def get_attribute(self, _):
            return "XCUIElementTypeTextField"

    class Driver:
        page_source = small_phone_home_source()

        def __init__(self):
            self.taps = []

        def execute_script(self, name, args):
            self.taps.append(args)

    driver = Driver()
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    field = Field()
    monkeypatch.setattr(collector, "_search_input", lambda d: field if len(d.taps) == 2 else None)
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    actions = []
    assert collector._open_search_input(driver, actions) is field
    assert len(driver.taps) == 2 and driver.taps[0] != driver.taps[1]
    assert actions[-1]["action"] == "搜索输入框确认"


def test_search_retry_does_not_click_history_when_page_changed(monkeypatch):
    class Driver:
        page_source = small_phone_home_source()
        taps = 0

        def execute_script(self, *args):
            self.taps += 1
            self.page_source = '''<XCUIElementTypeApplication width="390" height="844">
              <XCUIElementTypeStaticText name="搜索历史 iphone15" x="49" y="99" width="100" height="30" visible="true" />
            </XCUIElementTypeApplication>'''

    driver = Driver()
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    monkeypatch.setattr(collector, "_search_input", lambda _: None)
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="没有可确认的搜索入口"):
        collector._open_search_input(driver, [])
    assert driver.taps == 1


def test_interrupted_foreground_is_restored_before_clicking(monkeypatch):
    class Driver:
        restored = False

        def execute_script(self, _):
            return {"bundleId": "com.taobao.fleamarket" if self.restored else "com.apple.springboard"}

        def activate_app(self, bundle):
            assert bundle == "com.taobao.fleamarket"
            self.restored = True

    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = Driver()
    actions = []
    IPhoneCollector(Settings())._ensure_foreground(driver, actions)
    assert driver.restored and actions[0]["action"] == "恢复闲鱼前台"


def test_region_confirmation_uses_sort_row_on_390_point_screen():
    source = '''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeStaticText name="新发" x="218" y="393" width="26" visible="true" />
      <XCUIElementTypeStaticText name="厦门" x="275" y="393" width="29" visible="true" />
      <XCUIElementTypeStaticText name="筛选" x="343" y="393" width="29" visible="true" />
    </XCUIElementTypeApplication>'''
    assert result_bar_label(source, "厦门")


def test_region_option_is_scrolled_with_native_table_element_not_page_coordinates(monkeypatch):
    class Option:
        id = "province-fujian"

    class Driver:
        visible = False
        page_source = '''<XCUIElementTypeApplication>
          <XCUIElementTypeButton name="查看999+宝贝" visible="true" />
        </XCUIElementTypeApplication>'''

        def find_elements(self, strategy, query):
            assert query.startswith("//XCUIElementTypeTable//")
            return [Option()]

        def execute_script(self, command, args):
            assert command == "mobile: scrollToElement" and args == {"elementId": "province-fujian"}
            self.visible = True

    driver = Driver()
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_find", lambda d, *args: Option() if d.visible else None)
    clicked = []
    assert collector._region_option(driver, "福建", clicked).id == "province-fujian"
    assert clicked == ["滚动至 福建"]


def test_candidate_rejection_continues_scrolling_when_returned_sort_bar_is_hidden(monkeypatch):
    class Driver:
        page_source = scrolled_result_source()
        swipes = 0

        def execute_script(self, command, args):
            assert command == "mobile: swipe" and args["direction"] == "up"
            self.swipes += 1

        def quit(self):
            pass

    driver = Driver()
    monkeypatch.setattr("appium.webdriver.Remote", lambda *args, **kwargs: driver)
    monkeypatch.setattr("app_monitor.iphone_collector.ensure_appium_server", lambda _: None)
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    monkeypatch.setattr("app_monitor.iphone_collector.parse_cards", lambda *args: [
        PhoneCard("iPhone 17 512G 个人闲置", 3200, "湖里区", 220, 700),
    ])
    collector = IPhoneCollector(Settings(ios_wda_bundle_id="test.wda"))
    monkeypatch.setattr(collector, "_search", lambda *args: True)
    monkeypatch.setattr(collector, "_apply_model", lambda *args: ["iPhone 17", "确定"])
    monkeypatch.setattr(collector, "_apply_storage", lambda *args: ["512GB", "确定"])
    monkeypatch.setattr(collector, "_sort_new", lambda *args: None)

    def reject(*args):
        raise RuntimeError("详情页标题或价格与候选卡片不一致，已跳过复制")

    monkeypatch.setattr(collector, "_share_item_id", reject)
    result = collector._collect("test", "", "iPhone 17", "厦门", [], ["512"], 1, lambda *args: True)
    assert driver.swipes == 1
    assert result.passes[0]["error"] is None and result.passes[0]["candidates"] == 1
    assert any(not action["success"] and action["action"] == "复制候选链接"
               for action in result.passes[0]["actions"])


@pytest.mark.parametrize("chip_x, visibility", [(427, "false"), (365, "true")])
def test_capacity_strip_is_revealed_horizontally_on_narrow_phone(monkeypatch, chip_x, visibility):
    class Driver:
        page_source = f'''<XCUIElementTypeApplication width="390" height="844">
          <XCUIElementTypeStaticText name="新发" x="216" y="146" visible="true" />
          <XCUIElementTypeStaticText name="筛选" x="346" y="146" visible="true" />
          <XCUIElementTypeStaticText name="存储容量" x="{chip_x}" y="187" width="54" height="17" visible="{visibility}" />
        </XCUIElementTypeApplication>'''

        def execute_script(self, command, args):
            assert command == "mobile: dragFromToForDuration"
            assert args["fromY"] == args["toY"] == 195
            assert 0 < args["toX"] < args["fromX"] < 390
            self.page_source = self.page_source.replace(f'x="{chip_x}"', 'x="193"').replace('visible="false"', 'visible="true"')

    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    label, restored = IPhoneCollector(Settings())._reveal_storage_chip(Driver())
    assert label == "存储容量" and restored == ["横向显示容量筛选"]


def test_unnamed_checkmark_is_scoped_to_picker_option_row():
    source = '''<XCUIElementTypeApplication>
      <XCUIElementTypeButton name="确定" visible="true" />
      <XCUIElementTypeOther x="0" y="218" width="195" height="45">
        <XCUIElementTypeStaticText name="256GB" x="16" y="232" width="139" height="18" visible="true" />
        <XCUIElementTypeImage x="155" y="228" width="24" height="25" visible="true" />
      </XCUIElementTypeOther>
      <XCUIElementTypeOther x="195" y="262" width="195" height="45">
        <XCUIElementTypeStaticText name="512GB" x="211" y="276" width="139" height="18" visible="true" />
      </XCUIElementTypeOther>
    </XCUIElementTypeApplication>'''
    assert checked_storages(source) == {"256GB"}
    assert not checked_storages(source.replace('name="确定"', 'name="其他按钮"'))
    assert not checked_storages(source.replace('width="24" height="25"', 'width="100" height="150"'))
    assert not checked_storages(source.replace('x="155"', 'x="5"'))


def test_detail_identity_diagnostics_distinguish_title_price_and_loading():
    from app_monitor.iphone_collector import detail_mismatch_reason
    card=PhoneCard('iPhone 16 Plus 黑色 128G 美版官换有锁',3260,'湖里区',20,400)
    source=f'''<XCUIElementTypeApplication>
      <XCUIElementTypeTextView name="{card.title}，单机出，无配件" visible="true" />
      <XCUIElementTypeStaticText name="￥3,260.00元" y="157" visible="true" />
    </XCUIElementTypeApplication>'''
    assert detail_mismatch_reason(source,card) is None
    reason=detail_mismatch_reason(source.replace('3,260.00','3,200.00'),card)
    assert '价格不匹配' in reason and '¥3260' in reason and '¥3200' in reason and '标题不匹配' not in reason
    assert '标题不匹配' in detail_mismatch_reason(source.replace(card.title,'iPhone 15 Pro 128G'),card)
    assert '未读到正文' in detail_mismatch_reason('<XCUIElementTypeApplication/>',card)
    assert not detail_matches(source.replace('visible="true"','visible="false"'),card)


@pytest.mark.parametrize('delayed',[True,False])
def test_share_waits_for_identity_and_preserves_failed_comparison(monkeypatch,delayed):
    card=PhoneCard('iPhone 16 Plus 黑色 128G 个人自用',3260,'湖里区',20,400)
    full=f'''<XCUIElementTypeApplication>
      <XCUIElementTypeTextView name="{card.title}" visible="true" />
      <XCUIElementTypeStaticText name="3260" y="157" visible="true" />
    </XCUIElementTypeApplication>'''
    class Element:
        rect={'x':20,'y':400,'width':100,'height':30}
        def is_displayed(self):return True
        def get_attribute(self,key):return card.title
        def click(self):pass
    class Driver:
        reads=0
        def find_elements(self,*args):return [Element()]
        @property
        def page_source(self):
            self.reads+=1
            if not delayed:return full.replace('name="3260"','name="3200"')
            return '<XCUIElementTypeApplication/>' if self.reads<3 else full
        def execute_script(self,*args):pass
    driver=Driver();collector=IPhoneCollector(Settings());returns=[];copies=[]
    monkeypatch.setattr(collector,'_ensure_foreground',lambda *args:None)
    monkeypatch.setattr(collector,'_find',lambda *args:Element())
    monkeypatch.setattr(collector,'_return_to_results',lambda *args:returns.append(True))
    monkeypatch.setattr('app_monitor.iphone_collector.time.sleep',lambda _:None)
    monkeypatch.setattr('app_monitor.iphone_collector._prepare_pasteboard',lambda _:copies.append(True) or 'marker')
    monkeypatch.setattr('app_monitor.iphone_collector._pasteboard',lambda _:'https://www.goofish.com/item?id=1091531543430')
    actions=[]
    if delayed:
        assert collector._share_item_id(driver,'test',card,actions)=='1091531543430'
        assert driver.reads>=3 and copies
    else:
        with pytest.raises(RuntimeError,match='价格不匹配.*3260.*3200'):
            collector._share_item_id(driver,'test',card,actions)
        assert driver.reads==6 and copies==[]
        assert any(a.get('success') is False and '3200' in a.get('error','') for a in actions)
    assert returns==[True]
