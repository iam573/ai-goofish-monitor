import asyncio
from types import SimpleNamespace

from src import phone_scraper
from src.infrastructure.external.iphone.devices import IPhoneDevice
from src.infrastructure.external.iphone.page_source import PhoneCard
from src.infrastructure.external.iphone.search_client import IPhoneListing, IPhoneSearchSummary
from src.utils import get_link_unique_key


def test_card_rejection_applies_price_keywords_and_known_items():
    card = PhoneCard("Sony A7M4 单机身 自用", 9800, "厦门", 0, 0)
    assert phone_scraper.card_rejection_reason(card) is None
    assert phone_scraper.card_rejection_reason(card, min_price="10000") == "价格低于下限"
    assert phone_scraper.card_rejection_reason(card, max_price="9000") == "价格高于上限"
    assert phone_scraper.card_rejection_reason(card, exclude_keyword_rules=["自用"]) == "命中忽略关键词"
    assert phone_scraper.card_rejection_reason(
        card, decision_mode="keyword", keyword_rules=["套机"]) == "未命中关键词"
    assert phone_scraper.card_rejection_reason(card, keyword_rules=["套机"]) is None
    known = {phone_scraper.card_fingerprint("Sony A7M4 单机身 自用 送电池", 9800)}
    assert phone_scraper.card_rejection_reason(card, known_fingerprints=known) is None
    known = {phone_scraper.card_fingerprint("sony a7m4单机身自用", 9800.0)}
    assert phone_scraper.card_rejection_reason(card, known_fingerprints=known) == "已处理过"


def test_item_data_matches_web_result_contract():
    listing = IPhoneListing(PhoneCard("Sony A7M4", 9800, "", 0, 0), "1091531543430", "正文", ["品牌 Sony"], "")
    item = phone_scraper.build_item_data(listing, "主力机")
    assert get_link_unique_key(item["商品链接"]) == "https://www.goofish.com/item?id=1091531543430"
    assert item["当前售价"] == "¥9800" and item["商品ID"] == "1091531543430"
    assert item["发货地区"] == "地区未知" and item["卖家昵称"] == "未识别"
    assert item["商品描述"] == "正文" and item["商品参数"] == ["品牌 Sony"]


class FakeGuard:
    threshold = 3

    def __init__(self):
        self.successes = []

    def should_skip_start(self, task_name, cookie_path=None):
        return SimpleNamespace(skip=False)

    def record_success(self, task_name):
        self.successes.append(task_name)


def patch_runtime(monkeypatch, client_factory):
    guard = FakeGuard()
    saved, notified, failures = [], [], []
    device = IPhoneDevice("UDID-1", "主力机", "18.1", True, "已连接")

    async def save(record, keyword):
        saved.append(record)
        return True

    async def notify(item, reason):
        notified.append(item["商品ID"])

    async def notify_failure(task_config, reason, cookie_path=None):
        failures.append(reason)

    monkeypatch.setattr(phone_scraper, "FAILURE_GUARD", guard)
    monkeypatch.setattr(phone_scraper, "discover_devices", lambda: [device])
    monkeypatch.setattr(phone_scraper, "IPhoneSearchClient", client_factory)
    monkeypatch.setattr(phone_scraper, "save_to_jsonl", save)
    monkeypatch.setattr(phone_scraper, "send_ntfy_notification", notify)
    monkeypatch.setattr(phone_scraper, "_notify_task_failure", notify_failure)
    monkeypatch.setattr(phone_scraper, "cleanup_task_images", lambda task_name: None)
    monkeypatch.setattr(phone_scraper, "load_processed_link_keys",
                        lambda keyword: {"https://www.goofish.com/item?id=222"})
    monkeypatch.setattr(phone_scraper, "load_processed_title_prices",
                        lambda keyword: [("Sony A7M4 已处理", 9000.0)])
    monkeypatch.setattr(phone_scraper, "load_price_snapshots", lambda keyword: [])
    monkeypatch.setattr(phone_scraper, "record_market_snapshots", lambda **kwargs: [])
    monkeypatch.setattr(phone_scraper.IPhoneSettings, "from_env",
                        classmethod(lambda cls: SimpleNamespace(udid="", screens_per_page=2, lock_timeout_seconds=0)))
    monkeypatch.setattr(phone_scraper, "load_selected_iphone_udids", lambda: [])
    monkeypatch.setattr(phone_scraper, "any_iphone_device_lock", lambda udids, *args, **kwargs: _FirstFreeLock(udids))
    monkeypatch.setattr(phone_scraper, "iphone_device_channel", lambda udid: {"channel": "ui", "client_ip": ""})
    return guard, saved, notified, failures


class _FirstFreeLock:
    def __init__(self, udids, busy=()):
        self.udid = next(udid for udid in udids if udid not in busy)

    def __enter__(self):
        return self.udid

    def __exit__(self, *args):
        return False


TASK = {
    "task_name": "A7M4",
    "keyword": "sony a7m4",
    "max_pages": 2,
    "max_price": "10000",
    "decision_mode": "keyword",
    "keyword_rules": ["A7M4"],
    "search_source": "iphone",
}


def test_phone_scrape_dedupes_and_submits_items_to_analysis(monkeypatch):
    searched = {}

    class FakeClient:
        def __init__(self, settings, device, stop_event=None, log=print, port_offset=0):
            pass

        def search(self, keyword, screens, is_candidate, on_listing, on_card=None, capture_screenshot=False):
            searched.update(keyword=keyword, screens=screens, capture=capture_screenshot)
            cards = [
                (PhoneCard("Sony A7M4 单机身", 9800, "厦门", 0, 0), "111"),
                (PhoneCard("Sony A7M4 套机", 15000, "", 0, 0), "333"),
                (PhoneCard("Sony A7M4 已处理", 9000, "", 0, 0), "444"),
                (PhoneCard("Sony A7M4 网页抓过", 9500, "", 0, 0), "222"),
            ]
            summary = IPhoneSearchSummary(device_name=device_name)
            for card, item_id in cards:
                summary.cards_seen += 1
                on_card(card)
                if is_candidate(card):
                    summary.candidates += 1
                    summary.linked += 1
                    on_listing(IPhoneListing(card, item_id, "", [], "卖家"))
            return summary

    device_name = "主力机"
    guard, saved, notified, failures = patch_runtime(monkeypatch, FakeClient)

    processed = asyncio.run(phone_scraper.scrape_xianyu_via_phone(dict(TASK)))

    assert searched == {"keyword": "sony a7m4", "screens": 4, "capture": False}
    assert processed == 1
    assert [record["商品信息"]["商品ID"] for record in saved] == ["111"]
    assert notified == ["111"]
    assert saved[0]["价格参考"]["当前搜索样本"]
    assert failures == [] and guard.successes == ["A7M4"]


def test_phone_scrape_reports_device_failure(monkeypatch):
    guard, saved, notified, failures = patch_runtime(monkeypatch, object)
    monkeypatch.setattr(phone_scraper, "discover_devices", lambda: [])

    assert asyncio.run(phone_scraper.scrape_xianyu_via_phone(dict(TASK))) == 0
    assert len(failures) == 1 and "没有已连接的 iPhone" in failures[0]
    assert guard.successes == []


def test_phone_scrape_runs_on_idle_selected_phone(monkeypatch):
    used = {}

    class FakeClient:
        def __init__(self, settings, device, stop_event=None, log=print, port_offset=0):
            used.update(name=device.name, port_offset=port_offset)

        def search(self, keyword, screens, is_candidate, on_listing, on_card=None, capture_screenshot=False):
            return IPhoneSearchSummary(device_name=used["name"])

    patch_runtime(monkeypatch, FakeClient)
    devices = [
        IPhoneDevice("UDID-A", "主力机", "18.1", True, "已连接"),
        IPhoneDevice("UDID-B", "备用机", "18.1", True, "已连接"),
        IPhoneDevice("UDID-C", "未勾选", "18.1", True, "已连接"),
    ]
    locked = {}

    def lock(udids, *args, **kwargs):
        locked["udids"] = udids
        return _FirstFreeLock(udids, busy={"UDID-A"})

    monkeypatch.setattr(phone_scraper, "discover_devices", lambda: devices)
    monkeypatch.setattr(phone_scraper, "load_selected_iphone_udids", lambda: ["UDID-A", "UDID-B"])
    monkeypatch.setattr(phone_scraper, "any_iphone_device_lock", lock)

    asyncio.run(phone_scraper.scrape_xianyu_via_phone(dict(TASK)))

    assert locked["udids"] == ["UDID-A", "UDID-B"]
    assert used == {"name": "备用机", "port_offset": 1}


def test_proxy_channel_phone_uses_proxy_client_and_keeps_images(monkeypatch):
    used = {}

    class FakeProxyClient:
        def __init__(self, settings, device, *, client_ip, stop_event=None, log=print, port_offset=0):
            used.update(client_ip=client_ip)

        def search(self, keyword, screens, is_candidate, on_listing, on_card=None, capture_screenshot=False):
            summary = IPhoneSearchSummary(device_name="主力机")
            for item_id in ("222", "555"):
                card = PhoneCard(f"Sony A7M4 {item_id}", 9000, "上海", 0, 0, item_id=item_id)
                summary.cards_seen += 1
                if is_candidate(card):
                    used.setdefault("candidates", []).append(item_id)
                    on_listing(IPhoneListing(card, item_id, "正文", [], "卖家",
                                             image_urls=("https://img.alicdn.com/a.jpg",),
                                             publish_time="2026-10-30 09:15", channel="proxy"))
            return summary

    def ui_client(*args, **kwargs):
        raise AssertionError("代理通道不应使用界面采集")

    guard, saved, notified, failures = patch_runtime(monkeypatch, ui_client)
    monkeypatch.setattr(phone_scraper, "IPhoneProxySearchClient", FakeProxyClient)
    monkeypatch.setattr(phone_scraper, "iphone_device_channel",
                        lambda udid: {"channel": "proxy", "client_ip": "192.168.1.23"})

    processed = asyncio.run(phone_scraper.scrape_xianyu_via_phone(dict(TASK)))

    assert processed == 1 and failures == []
    assert used == {"client_ip": "192.168.1.23", "candidates": ["555"]}
    item = saved[0]["商品信息"]
    assert item["商品图片列表"] == ["https://img.alicdn.com/a.jpg"]
    assert item["发布时间"] == "2026-10-30 09:15"
    assert item["采集来源"] == "iPhone App（主力机 · 代理）"
