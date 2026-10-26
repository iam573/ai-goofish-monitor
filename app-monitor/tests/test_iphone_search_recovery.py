"""Recover through real navigation controls; preserve region checks and blockers."""
from types import SimpleNamespace

import pytest

from app_monitor.config import Settings
from app_monitor.iphone_collector import IPhoneCollector, _VerificationRequired
from app_monitor.iphone_proxy_collector import IPhoneProxyCollector

UNKNOWN = '<XCUIElementTypeApplication width="390" height="844" />'
HOME = '''<XCUIElementTypeApplication width="390" height="844">
  <XCUIElementTypeButton name="首页" x="8" y="761" width="70" height="50" visible="true" />
  <XCUIElementTypeButton name="同城" x="78" y="761" width="70" height="50" visible="true" />
  <XCUIElementTypeButton name="消息" x="240" y="761" width="70" height="50" visible="true" />
  <XCUIElementTypeButton name="我的" x="310" y="761" width="70" height="50" visible="true" />
</XCUIElementTypeApplication>'''
SEARCH_HOME = HOME.replace('</XCUIElementTypeApplication>', '''
  <XCUIElementTypeButton name="手机" traits="SearchField" x="8" y="97" width="374" height="36" visible="true" />
</XCUIElementTypeApplication>''')


class Driver:
    def __init__(self, source=UNKNOWN, on_home=None, on_restart=None):
        self.page_source = source
        self.on_home = on_home
        self.on_restart = on_restart
        self.taps = []
        self.restarts = []

    def execute_script(self, command, args=None):
        if command == 'mobile: activeAppInfo':
            return {'bundleId': 'com.taobao.fleamarket'}
        assert command == 'mobile: tap'
        self.taps.append(args)
        if self.on_home:
            self.page_source = self.on_home

    def terminate_app(self, bundle):
        self.restarts.append(bundle)

    def activate_app(self, bundle):
        if self.restarts and self.on_restart:
            self.page_source = self.on_restart


def make_collector(monkeypatch, proxy=False):
    collector = IPhoneProxyCollector(Settings(), SimpleNamespace()) if proxy else IPhoneCollector(Settings())
    # Evaluate actual readiness conditions with a deterministic timeout.
    wait = collector._wait_for_source
    monkeypatch.setattr(collector, '_wait_for_source', lambda *a, **kw: wait(*a, **(kw | {'timeout': 0})))
    field = SimpleNamespace(get_attribute=lambda _: 'XCUIElementTypeTextField')
    monkeypatch.setattr(collector, '_search_input', lambda d: field if d.page_source == SEARCH_HOME else None)
    return collector, field


@pytest.mark.parametrize('proxy', [False, True])
def test_recovers_from_another_bottom_tab_without_restarting(monkeypatch, proxy):
    collector, field = make_collector(monkeypatch, proxy)
    driver = Driver(HOME, on_home=SEARCH_HOME)
    actions = []
    assert collector._prepare_search_input(driver, '', actions) == (field, False)
    assert driver.taps == [{'x': 43, 'y': 786}]
    assert not driver.restarts
    assert any(a['action'] == '恢复搜索页面' and a['target'] == '首页' for a in actions)


def test_unknown_page_restarts_once_and_rechecks_region(monkeypatch):
    collector, field = make_collector(monkeypatch)
    driver = Driver(on_restart=SEARCH_HOME)
    seen = []
    def region(d, city, actions):
        seen.append((d.page_source, city))
        return d.page_source == SEARCH_HOME
    monkeypatch.setattr(collector, '_select_home_city', region)
    assert collector._prepare_search_input(driver, '厦门', []) == (field, True)
    assert driver.restarts == ['com.taobao.fleamarket']
    assert seen[-1] == (SEARCH_HOME, '厦门')


def test_persistent_failure_is_bounded_and_never_taps_history(monkeypatch):
    collector, _ = make_collector(monkeypatch)
    history = UNKNOWN.replace('/>', '''>
      <XCUIElementTypeStaticText name="搜索历史 iphone15" x="49" y="99" width="100" height="30" visible="true" />
    </XCUIElementTypeApplication>''')
    driver = Driver(history)
    actions = []
    with pytest.raises(RuntimeError, match='重启闲鱼一次后'):
        collector._prepare_search_input(driver, '', actions)
    assert len(driver.restarts) == 1 and not driver.taps
    assert sum(a['action'] == '搜索入口检查' for a in actions) == 3


@pytest.mark.parametrize('node, error', [
    ('<XCUIElementTypeOther name="验证码拦截" visible="true" />', '安全验证'),
    ('<XCUIElementTypeAlert name="允许粘贴" visible="true" />', '系统弹窗'),
    ('<XCUIElementTypeButton name="立即登录" visible="true" />', '需要登录'),
])
@pytest.mark.parametrize('after_restart', [False, True])
def test_blockers_stop_recovery_even_after_restart(monkeypatch, node, error, after_restart):
    collector, _ = make_collector(monkeypatch)
    blocked = SEARCH_HOME.replace('</XCUIElementTypeApplication>', node + '</XCUIElementTypeApplication>')
    driver = Driver(UNKNOWN if after_restart else blocked, on_restart=blocked)
    with pytest.raises(RuntimeError, match=error):
        collector._prepare_search_input(driver, '', [])
    assert len(driver.restarts) == int(after_restart)
    assert not driver.taps


def test_back_control_is_used_but_product_text_is_never_tapped(monkeypatch):
    collector, field = make_collector(monkeypatch)
    source = UNKNOWN.replace('/>', '''>
      <XCUIElementTypeButton name="返回按钮" x="0" y="50" width="40" height="40" visible="true" />
      <XCUIElementTypeStaticText name="首页" x="80" y="200" width="100" height="40" visible="true" />
    </XCUIElementTypeApplication>''')
    driver = Driver(source, on_home=SEARCH_HOME)
    assert collector._prepare_search_input(driver, '', []) == (field, False)
    assert driver.taps == [{'x': 20, 'y': 70}, {'x': 43, 'y': 786}]
    assert not driver.restarts


def test_already_ready_entry_needs_no_navigation(monkeypatch):
    collector, field = make_collector(monkeypatch)
    driver = Driver(SEARCH_HOME)
    assert collector._prepare_search_input(driver, '', []) == (field, False)
    assert not driver.taps and not driver.restarts


def test_home_navigation_wait_does_not_swallow_security_verification(monkeypatch):
    collector, _ = make_collector(monkeypatch)
    blocked = UNKNOWN.replace('/>', '><XCUIElementTypeOther name="验证码拦截" visible="true" /></XCUIElementTypeApplication>')
    driver = Driver(HOME, on_home=blocked)
    with pytest.raises(_VerificationRequired):
        collector._prepare_search_input(driver, '', [])
    assert len(driver.taps) == 1 and not driver.restarts


def test_startup_wait_uses_page_condition(monkeypatch):
    collector, field = make_collector(monkeypatch)
    driver = Driver()
    wait = collector._wait_for_source
    def appeared(d, ready, failure, **kw):
        if failure == '重启后页面尚未就绪':
            assert not ready(UNKNOWN)
            d.page_source = SEARCH_HOME
            assert ready(SEARCH_HOME)
        return wait(d, ready, failure, **kw)
    monkeypatch.setattr(collector, '_wait_for_source', appeared)
    assert collector._prepare_search_input(driver, '', []) == (field, False)
    assert len(driver.restarts) == 1


def test_recovered_search_enters_original_keyword_and_submits(monkeypatch):
    collector, _ = make_collector(monkeypatch)
    driver = Driver(on_restart=SEARCH_HOME)
    submitted = []
    class Field:
        value = '旧的搜索词'
        def click(self):
            pass
        def clear(self):
            self.value = ''
        def send_keys(self, value):
            self.value += value
        def get_attribute(self, _):
            return self.value
    field = Field()
    submit = SimpleNamespace(click=lambda: submitted.append(field.value))
    monkeypatch.setattr(collector, '_search_input', lambda d: field if d.page_source == SEARCH_HOME else None)
    monkeypatch.setattr(collector, '_find', lambda d, name, *args: submit if name in {'Search', '新发'} else None)
    monkeypatch.setattr(collector, '_close_share_sheet', lambda d: None)
    actions = []
    assert collector._search(driver, 'iPhone 16 Pro', actions=actions) is False
    assert submitted == ['iPhone 16 Pro']
    assert len(driver.restarts) == 1
    assert actions[-1] == {'action': '搜索', 'target': 'iPhone 16 Pro', 'success': True}
