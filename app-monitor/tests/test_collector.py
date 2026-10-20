import json
import pytest

from app_monitor.collector import (
    _image_urls,
    _real_item_id,
    extract_expdata,
    guess_region,
    plan_region_steps,
    region_context_matches,
    region_slot,
    search_bar_city,
    visible_cards_from_nodes,
    _sort_bar_labels,
    VisibleCard,
)


def test_exposure_prefers_item_id_and_preserves_explicit_image_urls():
    payload = {"id": "1", "item_id": "1094279560524", "picInfo": {"picUrl": "https://img.example/item.jpg"}}

    assert _real_item_id(payload) == "1094279560524"
    assert _image_urls(payload) == ["https://img.example/item.jpg"]


def test_extract_expdata_reads_balanced_json_with_embedded_commas():
    payload = [
        {
            "exargs": {
                "id": "123",
                "keyword": "iPhone 15",
                "displayPrice": "3200",
                "label": "a,b,c",
            }
        }
    ]
    raw = f"timezone=Asia/Shanghai,expdata={json.dumps(payload)},after=value"

    assert extract_expdata(raw)[0]["id"] == "123"
    assert extract_expdata(raw)[0]["label"] == "a,b,c"


def test_visible_cards_pairs_nearby_title_and_price():
    nodes = [
        {"role": "AXStaticText", "value": "苹果 iPhone 15 Pro 256G 国行", "position": {"x": 100, "y": 200}},
        {"role": "AXStaticText", "value": "¥3200元", "position": {"x": 100, "y": 245}},
    ]

    cards = visible_cards_from_nodes(nodes, "iPhone 15 Pro")

    assert len(cards) == 1
    assert cards[0].price == 3200
    assert "15 Pro" in cards[0].title



def test_visible_cards_read_region_near_the_price():
    nodes = [
        {"role": "AXStaticText", "value": "iPhone 15 Pro 256G 国行 自用", "position": {"x": 100, "y": 200}},
        {"role": "AXStaticText", "value": "¥3200", "position": {"x": 100, "y": 245}},
        {"role": "AXStaticText", "value": "包邮", "position": {"x": 160, "y": 245}},
        {"role": "AXStaticText", "value": "福建厦门", "position": {"x": 120, "y": 290}},
        {"role": "AXStaticText", "value": "iPhone 15 Pro 512G 美版", "position": {"x": 500, "y": 200}},
        {"role": "AXStaticText", "value": "¥2800", "position": {"x": 500, "y": 245}},
        {"role": "AXStaticText", "value": "上海", "position": {"x": 520, "y": 290}},
    ]

    cards = {card.price: card for card in visible_cards_from_nodes(nodes, "iPhone 15 Pro", "厦门")}

    assert cards[3200].region == "福建厦门"
    assert cards[2800].region == "上海"


def test_guess_region_prefers_hint_and_skips_tags():
    assert guess_region(["包邮", "思明区"], "厦门") == "思明区"
    assert guess_region(["包邮", "湖里"], "厦门") == "湖里"
    assert guess_region(["包邮", "厦门市"], "厦门") == "厦门市"
    assert guess_region(["发货极快", "验货宝"]) == ""


def node(text, x, y, role="AXStaticText"):
    return {"role": role, "value": text, "position": {"x": x, "y": y}}


# 2026-10-19 在 Mac 版闲鱼搜索「iPhone 15 Pro」后的实际界面（节选，坐标为屏幕坐标）
REAL_SEARCH_PAGE = [
    node("iPhone 15 Pro Max", 102, 202, "AXGenericElement"),
    node("256GB", 154, 365), node("3649", 152, 393), node("95新·功能A", 128, 430),
    node("存储容量", 392, 519), node("品牌", 461, 519),
    node("综合", 132, 546), node("价格", 257, 546), node("降价", 373, 546),
    node("新发", 478, 546), node("厦门", 588, 546), node("筛选", 709, 546),
    node("25.2km", 466, 894),
    node("15p\u200b256黑色 国行 全原 屏幕 边框没有磕碰划痕 电池78有烧屏", 437, 932),
    node("25.7km", 123, 991),
    node("3000元", 437, 996, "AXGenericElement"),
    node("3850元", 93, 1022, "AXGenericElement"),
    node("i\u200bP\u200bh\u200bo\u200bn\u200be\u200b15 p\u200br\u200bo\u200b 256G 原色 国行无拆无修 成色都在图里啦", 93, 1029),
    node("卖家信用极好", 214, 1075), node("同安区", 143, 1083),
    node("集美区", 487, 1085), node("百分百好评", 557, 1085),
]


def test_real_page_cards_pair_title_price_and_district_by_column():
    cards = {card.price: card for card in visible_cards_from_nodes(REAL_SEARCH_PAGE, "iPhone 15 Pro", "厦门")}
    assert cards[3850].title.startswith("i\u200bP") and cards[3850].region == "同安区"
    assert cards[3000].title.startswith("15p") and cards[3000].region == "集美区"


def test_region_slot_detects_remembered_city():
    assert region_slot(REAL_SEARCH_PAGE) == "厦门"
    steps = ["区域", "福建", "厦门", "查看"]
    assert plan_region_steps(REAL_SEARCH_PAGE, "厦门", steps) is None
    # 排序栏显示的是别的城市时，从该按钮开始重新选
    other = [dict(n, value="泉州") if n["value"] == "厦门" else n for n in REAL_SEARCH_PAGE]
    assert plan_region_steps(other, "厦门", steps) == ["泉州", "福建", "厦门", "查看"]
    fresh = [dict(n, value="区域") if n["value"] == "厦门" else n for n in REAL_SEARCH_PAGE]
    assert plan_region_steps(fresh, "厦门", steps) == steps


def test_region_slot_uses_actual_sort_row_when_duplicate_controls_exist():
    duplicate = [
        node("综合", 10, 100), node("新发", 100, 100), node("行情", 180, 100),
        node("筛选", 260, 100), node("降价", 140, 100),
    ]
    assert region_slot(duplicate + REAL_SEARCH_PAGE) == "厦门"


def test_search_bar_city_confirmation_only_accepts_city_chip_above_sort_row():
    nodes = REAL_SEARCH_PAGE + [node("厦门", 50, 100)]
    assert search_bar_city(nodes) == "厦门"
    assert search_bar_city(REAL_SEARCH_PAGE) is None
    confirmed, search_city, slot = region_context_matches(nodes, "厦门市")
    assert confirmed and search_city == "厦门" and slot == "厦门"

    mismatch = REAL_SEARCH_PAGE + [node("泉州", 50, 100)]
    assert region_context_matches(mismatch, "厦门")[0] is False


def test_sort_bar_wait_targets_control_row_not_duplicate_panel_labels():
    nodes = REAL_SEARCH_PAGE + [node("筛选", 700, 620), node("新发", 100, 620)]
    assert {"新发", "降价", "筛选"}.issubset(_sort_bar_labels(nodes))


def test_correlate_uses_exposure_index_to_disambiguate_same_price_cards():
    from app_monitor.collector import GoofishCollector

    cards = [VisibleCard("iPhone 15 Pro 256G 黑色", 3200), VisibleCard("iPhone 15 Pro 256G 白色", 3200)]
    exposures = [
        {"item_id": "100000001", "price": 3200, "index": 1, "tags": []},
        {"item_id": "100000002", "price": 3200, "index": 0, "tags": []},
    ]

    items = GoofishCollector._correlate(exposures, cards)

    assert [(item["item_id"], item["title"]) for item in items] == [
        ("100000002", "iPhone 15 Pro 256G 黑色"),
        ("100000001", "iPhone 15 Pro 256G 白色"),
    ]


class FakeAX:
    """模拟面板未关闭：容量筛选失败后，在按 Esc 之前「新发」都点不到。"""

    def __init__(self, nodes, home_search=False, fail_capacity=True, apply_region=True,
                 capacity_chip=False, apply_capacity=True):
        self.nodes_value, self.panel_open, self.log = nodes, False, []
        self.supports_home_search = home_search
        self.fail_capacity = fail_capacity
        self.apply_region = apply_region
        self.capacity_chip = "存储容量" if capacity_chip else None
        self.apply_capacity = apply_capacity

    async def nodes(self):
        return [*self.nodes_value, node(self.capacity_chip, 392, 578)] if self.capacity_chip else self.nodes_value

    async def wait_for(self, labels, timeout=15):
        self.log.append(("wait", tuple(sorted(labels))))
        return self.nodes_value

    async def wait_for_sort_bar(self, labels, timeout=15):
        self.log.append(("wait-sort", tuple(sorted(labels))))
        return await self.nodes()

    async def home_search(self, city, keyword):
        self.log.append(("home-search", city, keyword))
        if not self.supports_home_search:
            raise RuntimeError("首页地区不匹配")
        return {"searched": True, "region": city, "keyword": keyword}

    async def click_path(self, steps):
        self.log.append(("path", steps))
        if steps[0] in {"筛选", "存储容量", "256G", "512G"} and self.fail_capacity:
            self.panel_open = True
            raise RuntimeError("找不到可点击容量选项")
        if self.apply_region and steps[-1] == "查看" and len(steps) >= 3:
            city = steps[-2]
            self.nodes_value = [
                dict(item, value=city) if item.get("value") == steps[0] else item
                for item in self.nodes_value
            ]
        if self.capacity_chip and self.apply_capacity and steps[-1] == "确定":
            self.capacity_chip = steps[-2].replace("G", "GB")
        self.panel_open = False

    async def escape(self):
        self.log.append(("escape",))
        self.panel_open = False

    async def click(self, label):
        self.log.append(("click", label))
        if self.panel_open:
            raise RuntimeError(f"找不到可点击元素：{label}")

    async def scroll(self, screens=1):
        self.log.append(("scroll", screens))


class FakeExposures:
    def max_row_id(self):
        return 0

    def read_after(self, row_id, keyword):
        return [], 0


async def test_collect_skips_sort_when_capacity_filter_fails(monkeypatch):
    from app_monitor import collector as module
    from app_monitor.config import settings

    async def fake_exec(*args, **kwargs):
        class Process:
            async def wait(self):
                return 0
        return Process()

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(REAL_SEARCH_PAGE, capacity_chip=True)
    collector.exposures = FakeExposures()

    with pytest.raises(RuntimeError, match="容量筛选 256失败"):
        await collector.collect(
            "iPhone 15 Pro",
            region_steps=["区域", "福建", "厦门", "查看"],
            region_hint="厦门",
            storage_filters=[("256", ["筛选", "256G", "确定"])],
            scroll_pages=1,
        )
    log = collector.accessibility.log
    assert ("path", ["区域", "福建", "厦门", "查看"]) not in log  # 排序栏已是厦门
    assert ("escape",) in log
    assert not any(entry == ("click", "新发") for entry in log)


async def test_collect_reuses_one_search_for_multiple_capacity_filters(monkeypatch):
    from app_monitor import collector as module
    from app_monitor.config import settings

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(REAL_SEARCH_PAGE, home_search=True, fail_capacity=False,
                                     capacity_chip=True)
    collector.exposures = FakeExposures()

    result = await collector.collect(
        "iPhone 17 Pro", region_hint="厦门",
        storage_filters=[
            ("256", ["筛选", "存储容量", "256G", "确定"]),
            ("512", ["筛选", "存储容量", "512G", "确定"]),
        ],
    )

    actions = collector.accessibility.log
    assert actions.count(("home-search", "厦门", "iPhone 17 Pro")) == 1
    assert actions.count(("click", "新发")) == 2
    assert actions.count(("path", ["存储容量", "256G", "确定"])) == 1
    assert actions.count(("path", ["256GB", "256GB", "512G", "确定"])) == 1
    assert [entry["storage"] for entry in result.passes] == ["256", "512"]


async def test_capacity_click_without_result_chip_is_not_counted_as_filtered(monkeypatch):
    from app_monitor import collector as module
    from app_monitor.config import settings

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(REAL_SEARCH_PAGE, home_search=True, fail_capacity=False,
                                     capacity_chip=True, apply_capacity=False)
    collector.exposures = FakeExposures()

    with pytest.raises(RuntimeError, match="结果页容量标签.*不是「256」"):
        await collector.collect(
            "iPhone 17", region_hint="厦门",
            storage_filters=[("256", ["筛选", "存储容量", "256G", "确定"])],
        )
    assert ("click", "新发") not in collector.accessibility.log


async def test_collect_prefers_matching_home_region_tab(monkeypatch):
    from app_monitor import collector as module
    from app_monitor.config import settings

    async def should_not_open_deep_link(*args, **kwargs):
        raise AssertionError("首页地区搜索成功时不应再打开深链")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", should_not_open_deep_link)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(REAL_SEARCH_PAGE, home_search=True)
    collector.exposures = FakeExposures()

    result = await collector.collect(
        "iPhone 15 Pro",
        region_steps=["区域", "福建", "厦门", "查看"],
        region_hint="厦门",
    )

    assert ("home-search", "厦门", "iPhone 15 Pro") in collector.accessibility.log
    assert not any(entry[0] == "path" for entry in collector.accessibility.log)
    assert result.region_applied is True


async def test_home_city_search_falls_back_to_region_filter_when_city_is_not_confirmed(monkeypatch):
    from app_monitor import collector as module
    from app_monitor.config import settings

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    page = [dict(item) for item in REAL_SEARCH_PAGE]
    for item in page:
        if item.get("value") == "厦门" and item.get("position", {}).get("y") == 546:
            item["value"] = "区域"
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(page, home_search=True)
    collector.exposures = FakeExposures()

    result = await collector.collect(
        "iPhone 15 Pro", region_steps=["区域", "福建", "厦门", "查看"], region_hint="厦门"
    )

    assert result.region_applied is True
    assert ("path", ["区域", "福建", "厦门", "查看"]) in collector.accessibility.log


async def test_region_filter_requires_result_page_confirmation(monkeypatch):
    from app_monitor import collector as module
    from app_monitor.config import settings

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    page = [dict(item, value="区域") if item.get("value") == "厦门" else dict(item) for item in REAL_SEARCH_PAGE]
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(page, home_search=True, apply_region=False)
    collector.exposures = FakeExposures()

    result = await collector.collect(
        "iPhone 15 Pro", region_steps=["区域", "福建", "厦门", "查看"], region_hint="厦门"
    )

    assert result.region_applied is False
    assert "结果页未显示「厦门」" in result.region_error


async def test_search_bar_city_confirms_home_location_and_skips_region_filter(monkeypatch):
    from app_monitor import collector as module
    from app_monitor.config import settings

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    page = [dict(item) for item in REAL_SEARCH_PAGE]
    for item in page:
        if item.get("value") == "厦门" and item.get("position", {}).get("y") == 546:
            item["value"] = "区域"
    page.append(node("厦门", 50, 100))
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(page, home_search=True)
    collector.exposures = FakeExposures()

    result = await collector.collect(
        "iPhone 15 Pro", region_steps=["区域", "福建", "厦门", "查看"], region_hint="厦门"
    )

    assert result.region_applied is True
    assert not any(entry[0] == "path" for entry in collector.accessibility.log)


async def test_collect_run_log_contains_clicks_and_products(monkeypatch, caplog):
    import logging

    from app_monitor import collector as module
    from app_monitor.config import settings

    class Exposures(FakeExposures):
        def read_after(self, row_id, keyword):
            return ([{
                "item_id": "1094279560524", "keyword": keyword, "price": 3649,
                "publish_time": None, "tags": [], "image_urls": [], "index": 0,
                "source_row_id": 1,
            }], 1)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    caplog.set_level(logging.INFO, logger="app_monitor.collector")
    collector = module.GoofishCollector(settings)
    collector.accessibility = FakeAX(REAL_SEARCH_PAGE, home_search=True)
    collector.exposures = Exposures()

    result = await collector.collect("iPhone 15 Pro", region_hint="厦门")

    entry = result.passes[0]
    assert entry["items_total"] == 1
    assert entry["items"][0]["item_id"] == "1094279560524"
    assert any(action["action"] == "按首页地区搜索" for action in entry["actions"])
    assert any(action["action"] == "点击排序" and action["target"] == "新发" for action in entry["actions"])
    assert "采集结果 storage=未筛容量 sort=新发" in caplog.text
    assert "id=1094279560524" in caplog.text
