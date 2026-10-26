"""Alternate detail controls are unnamed; require the full surrounding layout."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app_monitor.config import Settings
from app_monitor.iphone_collector import IPhoneCollector, _VerificationRequired, detail_back_rect
from app_monitor.iphone_proxy_collector import IPhoneProxyCollector

DETAIL = '''<XCUIElementTypeApplication width="390" height="844">
 <XCUIElementTypeOther x="0" y="47" width="40" height="44" visible="true" />
 <XCUIElementTypeOther x="48" y="53" width="256" height="32" visible="true" />
 <XCUIElementTypeOther x="304" y="47" width="40" height="44" visible="true" />
 <XCUIElementTypeOther x="344" y="47" width="40" height="44" visible="true" />
 <XCUIElementTypeStaticText name="更多" x="344" y="47" width="40" height="44" visible="true" />
 <XCUIElementTypeStaticText name="留言按钮" x="4" y="762" width="48" height="42" visible="true" />
 <XCUIElementTypeStaticText name="收藏" x="52" y="762" width="48" height="42" visible="true" />
</XCUIElementTypeApplication>'''
LIST = '''<XCUIElementTypeApplication width="390" height="844">
 <XCUIElementTypeButton name="search more" y="63" visible="true" />
 <XCUIElementTypeStaticText name="新发" x="240" y="155" visible="false" />
 <XCUIElementTypeStaticText name="筛选" x="343" y="155" visible="false" />
</XCUIElementTypeApplication>'''


def test_unnamed_detail_back_is_the_observed_container():
    assert detail_back_rect(DETAIL) == {'x': 0, 'y': 47, 'width': 40, 'height': 44}


@pytest.mark.parametrize('before, after', [
    ('name="留言按钮"', 'name="其他"'), ('name="收藏"', 'name="其他"'),
    ('name="更多"', 'name="其他"'), ('width="256"', 'width="80"'),
    ('x="0" y="47"', 'x="200" y="47"'), ('height="44"', 'height="0"'),
])
def test_incomplete_or_wrong_page_never_supplies_a_back_coordinate(before, after):
    assert detail_back_rect(DETAIL.replace(before, after)) is None


def test_hidden_ancestors_and_covering_pickers_are_rejected():
    hidden = DETAIL.replace('<XCUIElementTypeOther x="0"', '<XCUIElementTypeOther visible="false"><XCUIElementTypeOther x="0"', 1).replace('height="44" visible="true" />', 'height="44" visible="true" /></XCUIElementTypeOther>', 1)
    assert detail_back_rect(hidden) is None
    for name in ('确定', '复制链接'):
        source = DETAIL.replace('</XCUIElementTypeApplication>', f'<XCUIElementTypeButton name="{name}" visible="true" /></XCUIElementTypeApplication>')
        assert detail_back_rect(source) is None


@pytest.mark.parametrize('proxy', [False, True])
def test_return_uses_real_unnamed_back_once_and_accepts_the_original_list(monkeypatch, proxy):
    collector = IPhoneProxyCollector(Settings(), None) if proxy else IPhoneCollector(Settings())
    class Driver:
        page_source = DETAIL
        taps = []
        def execute_script(self, command, args):
            assert command == 'mobile: tap'
            self.taps.append(args)
            self.page_source = LIST
    driver = Driver()
    monkeypatch.setattr(collector, '_ensure_foreground', lambda *a: None)
    monkeypatch.setattr(collector, '_close_share_sheet', lambda *a: None)
    monkeypatch.setattr(collector, '_find', lambda *a: None)
    monkeypatch.setattr('app_monitor.iphone_collector.time.sleep', lambda _: None)
    actions = []
    collector._return_to_results(driver, actions)
    assert driver.taps == [{'x': 20, 'y': 69}]
    assert actions[-1]['action'] == '确认搜索列表' and actions[-1]['success']


def test_unknown_page_is_not_tapped_and_reports_no_return_control(monkeypatch):
    collector = IPhoneCollector(Settings())
    driver = SimpleNamespace(page_source='<XCUIElementTypeApplication width="390" height="844" />')
    monkeypatch.setattr(collector, '_ensure_foreground', lambda *a: None)
    monkeypatch.setattr(collector, '_close_share_sheet', lambda *a: None)
    monkeypatch.setattr('app_monitor.iphone_collector.time.sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='未识别到可安全点击'):
        collector._return_to_results(driver, [])


def test_search_home_recovery_can_leave_the_unnamed_detail(monkeypatch):
    collector = IPhoneCollector(Settings())
    class Driver:
        page_source = DETAIL
        taps = []
        def execute_script(self, command, args):
            self.taps.append(args)
            self.page_source = LIST
    driver = Driver()
    monkeypatch.setattr(collector, '_ensure_foreground', lambda *a: None)
    monkeypatch.setattr(collector, '_search_input', lambda *a: object())
    collector._recover_search_home(driver, [])
    assert driver.taps == [{'x': 20, 'y': 69}]


@pytest.mark.parametrize('failed_return, verification', [(False, False), (True, False), (True, True)])
def test_proxy_preserves_detail_error_and_returns_before_the_next_candidate(monkeypatch, failed_return, verification):
    proxy = SimpleNamespace(wait_ready=lambda: None, session=lambda _: nullcontext())
    collector = IPhoneProxyCollector(Settings(), proxy)
    driver = SimpleNamespace(quit=lambda: None)
    monkeypatch.setattr(collector, '_connect', lambda *a: driver)
    monkeypatch.setattr(collector, '_search', lambda *a: True)
    monkeypatch.setattr(collector, '_apply_model', lambda *a: None)
    monkeypatch.setattr(collector, '_apply_storage', lambda *a: None)
    monkeypatch.setattr(collector, '_sort_new', lambda *a: None)
    monkeypatch.setattr(collector, '_pages', lambda *a: ([{'item_id': '1096147900172'}, {'item_id': '1096147900173'}], 1))
    details, returns = [], []
    def detail(*args):
        details.append(args[1]['item_id'])
        if verification:
            raise _VerificationRequired('闲鱼要求安全验证')
        raise RuntimeError('未收到该商品本次详情返回')
    def return_to_list(*args):
        returns.append(True)
        if failed_return:
            raise RuntimeError('未识别到可安全点击的详情返回入口')
    monkeypatch.setattr(collector, '_detail', detail)
    monkeypatch.setattr(collector, '_return_to_results', return_to_list)
    result = collector._collect_proxy('phone', '17', 'iPhone 13 Pro', '厦门', [], ['128'], 0,
                                      lambda *a: True, None, None, '192.168.1.20')
    entry = result.passes[0]
    if verification:
        assert '安全验证' in entry['error'] and not returns
    elif failed_return:
        assert '本次详情返回' in entry['error'] and '返回入口' in entry['error']
        assert len(details) == len(returns) == 1
    else:
        assert entry['error'] is None and len(details) == len(returns) == 2
        assert len([a for a in entry['actions'] if not a['success']]) == 2


def test_proxy_reads_matching_body_and_price_with_unnamed_navigation():
    from app_monitor.iphone_proxy_collector import detail_view
    body = 'iPhone 13 Pro 128G 电池刚换，屏幕轻微划痕'
    source = DETAIL.replace('</XCUIElementTypeApplication>', f'''
      <XCUIElementTypeStaticText name="1850" y="190" visible="true" />
      <XCUIElementTypeStaticText name="{body}" y="380" visible="true" />
    </XCUIElementTypeApplication>''')
    assert detail_view(source, body) == {'price': 1850, 'body_matched': True}
    with pytest.raises(ValueError, match='正文或售价'):
        detail_view(source, '另一件商品正文', 1999)
