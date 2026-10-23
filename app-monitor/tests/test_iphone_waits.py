import pytest

from app_monitor.config import Settings
from app_monitor.iphone_collector import IPhoneCollector


class Clock:
    now = 0

    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_ready_element_returns_immediately_without_fixed_sleep(monkeypatch):
    class Driver:
        page_source = '<ready />'

    def unexpected_sleep(_):
        pytest.fail('条件已满足时不应等待')
    monkeypatch.setattr('app_monitor.iphone_collector.time.sleep', unexpected_sleep)
    assert IPhoneCollector._wait_for_source(Driver(), lambda source: source == '<ready />', '未就绪') == '<ready />'


def test_wait_polls_until_ready_and_returns_that_snapshot(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('app_monitor.iphone_collector.time.monotonic', lambda: clock.now)
    monkeypatch.setattr('app_monitor.iphone_collector.time.sleep', clock.sleep)
    class Driver:
        reads = 0

        @property
        def page_source(self):
            self.reads += 1
            return '<ready />' if self.reads == 3 else '<loading />'

    driver = Driver()
    source = IPhoneCollector._wait_for_source(driver, lambda xml: xml == '<ready />', '未就绪', timeout=8)
    assert source == '<ready />' and driver.reads == 3
    assert clock.sleeps == [.15, .15] and clock.now == .3


def test_condition_timeout_is_bounded_and_reports_the_step(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('app_monitor.iphone_collector.time.monotonic', lambda: clock.now)
    monkeypatch.setattr('app_monitor.iphone_collector.time.sleep', clock.sleep)
    class Driver:
        page_source = '<loading />'

    with pytest.raises(RuntimeError, match='容量未选中.*超时'):
        IPhoneCollector._wait_for_source(Driver(), lambda _: False, '容量未选中', timeout=.3)
    assert clock.now == .3 and clock.sleeps == [.15, .15]


def test_new_session_reduces_native_idle_and_animation_wait(monkeypatch):
    captured = {}
    class Driver:
        def quit(self):
            pass
    def connect(*args, **kwargs):
        captured.update(kwargs['options'].to_capabilities())
        return Driver()
    monkeypatch.setattr('appium.webdriver.Remote', connect)
    monkeypatch.setattr('app_monitor.iphone_collector.ensure_appium_server', lambda _: None)
    collector = IPhoneCollector(Settings(ios_wda_bundle_id='test.wda'))
    monkeypatch.setattr(collector, '_search', lambda *args: False)
    monkeypatch.setattr(collector, '_apply_model', lambda *args: [])
    collector._collect('test', '', 'iPhone 16', '', [], [], 0, lambda *args: False)
    assert captured['appium:waitForIdleTimeout'] == .5
    assert captured['appium:settings'] == {'waitForIdleTimeout': .5, 'animationCoolOffTimeout': .3}


def test_confirmed_capacity_skips_reopening_picker(monkeypatch):
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, '_reveal_storage_chip', lambda _: ('256GB', []))
    class Driver:
        @property
        def page_source(self):
            pytest.fail('已确认的标签无须再次读取或点击')
    assert collector._apply_storage(Driver(), '256') == ['已确认 256GB']


def test_element_lookup_filters_on_device_in_a_single_command():
    class Element:
        def get_attribute(self, _):
            pytest.fail('不应再次远程查询类型')

        def is_displayed(self):
            pytest.fail('不应再次远程查询可见性')

    element = Element()
    class Driver:
        queries = []

        def find_elements(self, strategy, query):
            self.queries.append(query)
            assert 'visible == true' in query and "type IN {'XCUIElementTypeButton'}" in query
            return [element]

    driver = Driver()
    assert IPhoneCollector._find(driver, '确定', ('XCUIElementTypeButton',)) is element
    assert len(driver.queries) == 1


def test_capacity_waits_for_selection_and_popup_closure(monkeypatch):
    class Element:
        def __init__(self, driver, name):
            self.driver, self.name = driver, name
            self.rect = {'y': 370 if name == '新发' else 411}

        def is_displayed(self):
            return True

        def get_attribute(self, key):
            return 'XCUIElementTypeStaticText'

        def click(self):
            driver = self.driver
            driver.clicks.append(self.name)
            if driver.state == 'results':
                driver.state = 'opening'
                driver.schedule(lambda: setattr(driver, 'state', 'picker'))
            elif self.name == '确定':
                assert driver.selected == {'256GB'}
                driver.state = 'closing'
                driver.current = '256GB'  # 后面的结果标签已变，但面板仍未关闭。
                driver.schedule(lambda: setattr(driver, 'state', 'results'))
            elif self.name == '128GB':
                driver.schedule(lambda: driver.selected.discard('128GB'))
            elif self.name == '256GB':
                driver.schedule(lambda: driver.selected.add('256GB'))

    class Driver:
        state = 'results'
        current = '128GB'
        pending = None
        remaining = 0
        closing_reads = 0

        def __init__(self):
            self.clicks, self.selected = [], {'128GB'}

        def schedule(self, action):
            self.pending, self.remaining = action, 2

        @property
        def page_source(self):
            if self.state == 'closing':
                self.closing_reads += 1
            if self.pending:
                self.remaining -= 1
                if not self.remaining:
                    action, self.pending = self.pending, None
                    action()
            picker = ''
            if self.state in {'picker', 'closing'}:
                picker = '<XCUIElementTypeButton name="确定" visible="true" />'
                for name in ['128GB', '256GB']:
                    check = '<XCUIElementTypeImage name="search_checkbox_sel" />' if name in self.selected else ''
                    picker += f'<XCUIElementTypeOther><XCUIElementTypeStaticText name="{name}" visible="true" />{check}</XCUIElementTypeOther>'
            return f'''<XCUIElementTypeApplication width="390" height="844">
              <XCUIElementTypeStaticText name="新发" y="370" visible="true" />
              <XCUIElementTypeStaticText name="{self.current}" y="411" x="78" width="44" visible="true" />
              {picker}</XCUIElementTypeApplication>'''

        def find_elements(self, *args):
            return [Element(self, '128GB')]

    driver = Driver()
    collector = IPhoneCollector(Settings())
    monkeypatch.setattr(collector, '_reveal_storage_chip', lambda _: ('128GB', []))
    monkeypatch.setattr(collector, '_find', lambda d, name, *args: Element(d, name))
    monkeypatch.setattr('app_monitor.iphone_collector.time.sleep', lambda _: None)
    assert collector._apply_storage(driver, '256') == ['128GB', '取消 128GB', '256GB', '确定']
    assert driver.clicks == ['128GB', '128GB', '256GB', '确定']
    assert driver.state == 'results' and driver.closing_reads == 2
