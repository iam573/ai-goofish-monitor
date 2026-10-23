import io

import pytest
from PIL import Image, ImageDraw

from app_monitor.config import Settings
from app_monitor.iphone_collector import (
    IPhoneCollector, checked_models, model_filter_chip, model_picker_option,
)


def result_source(chip="品牌"):
    return f'''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeStaticText name="iPhone 16" x="52" y="60" width="64" height="18" visible="true" />
      <XCUIElementTypeStaticText name="新发" x="216" y="370" width="29" height="18" visible="true" />
      <XCUIElementTypeStaticText name="{chip}" x="153" y="411" width="93" height="18" visible="true" />
      <XCUIElementTypeStaticText name="iPhone 16" x="10" y="500" width="100" height="18" visible="true" />
    </XCUIElementTypeApplication>'''


def picker_source():
    return '''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeStaticText name="iPhone 16" x="52" y="60" width="64" height="18" visible="true" />
      <XCUIElementTypeStaticText name="苹果/Apple" x="5" y="218" width="93" height="45" visible="true" />
      <XCUIElementTypeButton name="全部" x="120" y="218" width="258" height="45" visible="true" />
      <XCUIElementTypeCell x="252" y="310" width="126" height="41" visible="true">
        <XCUIElementTypeStaticText name="iPhone 16 Pro" x="260" y="325" width="110" height="16" visible="true" />
      </XCUIElementTypeCell>
      <XCUIElementTypeCell x="252" y="362" width="126" height="41" visible="true">
        <XCUIElementTypeStaticText name="iPhone 16" x="260" y="373" width="110" height="19" visible="true" />
      </XCUIElementTypeCell>
      <XCUIElementTypeButton name="确定" x="148" y="647" width="230" height="45" visible="true" />
    </XCUIElementTypeApplication>'''


def screenshot(selected, scale=1):
    image = Image.new("RGB", (390 * scale, 844 * scale), "#f3f3f3")
    draw = ImageDraw.Draw(image)
    # 品牌列和确认按钮的黄色必须排除在机型判定之外。
    draw.rectangle((0, 218 * scale, 98 * scale, 263 * scale), fill="#ffe500")
    draw.rectangle((148 * scale, 647 * scale, 378 * scale, 692 * scale), fill="#ffe500")
    for name, y in [("iPhone 16 Pro", 310), ("iPhone 16", 362)]:
        if name in selected:
            draw.rectangle((252 * scale, y * scale, 378 * scale, (y + 41) * scale), fill="#ffe500")
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


def test_model_filter_targets_picker_cell_instead_of_same_search_title():
    assert model_filter_chip(result_source()).get("name") == "品牌"
    assert model_filter_chip(result_source("iPhone 16 Pro")).get("name") == "iPhone 16 Pro"
    option = model_picker_option(picker_source(), "iPhone 16")
    assert option.get("x") == "260" and option.get("y") == "373"
    assert model_picker_option(result_source(), "iPhone 16") is None
    assert model_picker_option(picker_source(), "iPhone 16 Plus") is None
    assert model_picker_option(picker_source().replace('height="41"', 'height="400"'), "iPhone 16") is None


@pytest.mark.parametrize("scale", [1, 3])
def test_selected_models_require_highlight_inside_bounded_model_cells(scale):
    assert checked_models(picker_source(), screenshot(set(), scale)) == set()
    assert checked_models(picker_source(), screenshot({"iPhone 16"}, scale)) == {"iPhone 16"}
    assert checked_models(picker_source(), screenshot({"iPhone 16", "iPhone 16 Pro"}, scale)) == {"iPhone 16", "iPhone 16 Pro"}
    assert checked_models(result_source(), screenshot({"iPhone 16"}, scale)) == set()


class Driver:
    def __init__(self, correct_selection=True, committed=True):
        self.opened = False
        self.selected = "iPhone 16 Pro"
        self.chip = "iPhone 16 Pro"
        self.correct_selection = correct_selection
        self.committed = committed
        self.taps = []

    @property
    def page_source(self):
        return picker_source() if self.opened else result_source(self.chip)

    def get_screenshot_as_png(self):
        return screenshot({self.selected} if self.selected else set())

    def execute_script(self, command, args):
        assert command == "mobile: tap"
        point = (args["x"], args["y"])
        self.taps.append(point)
        if point == (199.5, 420):
            self.opened = True
        elif point == (249, 240.5):
            self.selected = ""
        elif point == (315, 382.5):
            assert self.selected == ""  # 已选中目标时也要先清除，避免反向取消。
            self.selected = "iPhone 16" if self.correct_selection else "iPhone 16 Pro"
        elif point == (263, 669.5):
            self.opened = False
            self.chip = "苹果/Apple" if self.committed else "品牌"
        else:
            assert point == (51.5, 240.5)


def test_model_selection_is_cleared_checked_and_committed(monkeypatch):
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = Driver()
    collector = IPhoneCollector(Settings())
    assert collector._apply_model(driver, "iPhone 16") == [
        "iPhone 16 Pro", "苹果/Apple", "全部", "iPhone 16", "已校验 iPhone 16", "确定",
    ]
    assert driver.selected == "iPhone 16" and not driver.opened
    collector._apply_model(driver, "iPhone 16")
    assert driver.selected == "iPhone 16" and not driver.opened


@pytest.mark.parametrize("selected,committed,message", [
    (False, True, "机型勾选校验失败"),
    (True, False, "提交后未确认精确机型"),
])
def test_unconfirmed_model_does_not_succeed(monkeypatch, selected, committed, message):
    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match=message):
        IPhoneCollector(Settings())._apply_model(Driver(selected, committed), "iPhone 16")


def test_model_failure_stops_before_capacity_or_candidate_collection(monkeypatch):
    class Session:
        def quit(self):
            pass
    monkeypatch.setattr("appium.webdriver.Remote", lambda *args, **kwargs: Session())
    monkeypatch.setattr("app_monitor.iphone_collector.ensure_appium_server", lambda _: None)
    collector = IPhoneCollector(Settings(ios_wda_bundle_id="test.wda"))
    monkeypatch.setattr(collector, "_search", lambda *args: False)
    def fail(*args):
        raise RuntimeError("品牌面板未确认 iPhone 16")
    monkeypatch.setattr(collector, "_apply_model", fail)
    def forbidden(*args):
        pytest.fail("机型未确认时不能继续容量筛选或采集")
    monkeypatch.setattr(collector, "_apply_storage", forbidden)
    result = collector._collect("test", "", "iPhone 16", "", [], ["512"], 3, forbidden)
    assert not result.items and len(result.passes) == 1
    assert result.passes[0]["model_applied"] is False
    assert "未确认 iPhone 16" in result.passes[0]["error"]


def test_hidden_brand_chip_is_revealed_in_filter_row(monkeypatch):
    class HiddenChipDriver(Driver):
        hidden = True

        @property
        def page_source(self):
            source = super().page_source
            return source.replace('x="153"', 'x="450"') if self.hidden else source

        def execute_script(self, command, args):
            if command == "mobile: dragFromToForDuration":
                assert self.hidden and not self.opened
                assert args["fromY"] == args["toY"] == 415
                self.hidden = False
            else:
                super().execute_script(command, args)

    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = HiddenChipDriver()
    IPhoneCollector(Settings())._apply_model(driver, "iPhone 16")
    assert not driver.hidden and driver.selected == "iPhone 16"


def test_missing_model_only_scrolls_inside_model_panel_then_fails(monkeypatch):
    class MissingModelDriver(Driver):
        scrolls = 0

        def execute_script(self, command, args):
            if command == "mobile: dragFromToForDuration":
                assert self.opened
                assert 98 < args["fromX"] == args["toX"] < 378
                assert 218 < args["toY"] < args["fromY"] < 647
                self.scrolls += 1
            else:
                super().execute_script(command, args)

    monkeypatch.setattr("app_monitor.iphone_collector.time.sleep", lambda _: None)
    driver = MissingModelDriver()
    with pytest.raises(RuntimeError, match="未找到精确机型"):
        IPhoneCollector(Settings())._apply_model(driver, "iPhone 18")
    assert driver.scrolls == 7 and driver.opened
    assert (263, 669.5) not in driver.taps
