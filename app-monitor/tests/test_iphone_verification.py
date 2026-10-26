from types import SimpleNamespace

import pytest

from app_monitor.config import Settings
from app_monitor.iphone_collector import IPhoneCollector, _VerificationRequired, _raise_if_verification


CHALLENGE = '''<XCUIElementTypeApplication visible="true">
  <XCUIElementTypeImage name="复制链接" visible="false" x="81" y="502" width="71" height="88" />
  <XCUIElementTypeWebView visible="true">
    <XCUIElementTypeOther name="验证码拦截" visible="true" />
    <XCUIElementTypeStaticText name="请拖动下方滑块还原拼图" visible="true" />
  </XCUIElementTypeWebView>
</XCUIElementTypeApplication>'''


def test_current_challenge_stops_before_querying_or_tapping_underlying_share():
    class Driver:
        page_source = CHALLENGE

        def find_elements(self, *args):
            pytest.fail("验证覆盖层出现后不应再查询底层分享控件")

        def execute_script(self, *args):
            pytest.fail("验证覆盖层出现后不应再点击固定空白位置")

    with pytest.raises(_VerificationRequired, match="闲鱼要求安全验证.*手机完成验证"):
        IPhoneCollector._close_share_sheet(Driver())


@pytest.mark.parametrize("source", [
    '<root><XCUIElementTypeOther name="验证码拦截" visible="false" /></root>',
    '<root visible="false"><XCUIElementTypeOther name="验证码拦截" visible="true" /></root>',
    '<root><XCUIElementTypeTextView name="验证码拦截" visible="true" /></root>',
    '<root><XCUIElementTypeStaticText name="请拖动下方滑块还原拼图" visible="true" /></root>',
    '<root><XCUIElementTypeStaticText name="验证码拦截相关手机说明" visible="true" /></root>',
])
def test_hidden_or_product_text_is_not_a_challenge(source):
    _raise_if_verification(source)


def test_two_actual_challenge_prompts_are_enough_without_container_name():
    source = '''<root>
      <XCUIElementTypeStaticText name="亲，请按照说明进行验证哦" visible="true" />
      <XCUIElementTypeStaticText name="拖动左侧滑块完成上方拼图" visible="true" />
    </root>'''
    with pytest.raises(_VerificationRequired):
        _raise_if_verification(source)


def test_page_wait_does_not_accept_underlying_ready_elements_when_blocked():
    with pytest.raises(_VerificationRequired):
        IPhoneCollector._wait_for_source(SimpleNamespace(page_source=CHALLENGE), lambda _: True, "页面未就绪")


def test_copy_detects_challenge_appearing_after_share_click(monkeypatch):
    class Driver:
        page_source = CHALLENGE

        def execute_script(self, *args):
            pytest.fail("不能点击验证页面底下的复制坐标")

    driver = Driver()
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_find", lambda *args: SimpleNamespace(click=lambda: None))
    monkeypatch.setattr(collector, "_copy_link_rect", lambda _: dict(x=81,y=502,width=71,height=88))
    with pytest.raises(_VerificationRequired):
        collector._copy_from_share(driver, [], detail_card=None)


def test_return_detects_challenge_before_pressing_back(monkeypatch):
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, "_ensure_foreground", lambda *args: None)
    monkeypatch.setattr(collector, "_close_share_sheet", lambda *args: None)
    monkeypatch.setattr(collector, "_find", lambda *args: pytest.fail("不能点击验证覆盖层底下的返回按钮"))
    with pytest.raises(_VerificationRequired):
        collector._return_to_results(SimpleNamespace(page_source=CHALLENGE), [])
