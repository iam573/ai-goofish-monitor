from datetime import datetime

from app_monitor.regions import default_region_path, region_status
from app_monitor.rules import (
    Rules,
    eligible_quotes,
    in_search_window,
    load_rules,
    save_rules,
    search_keyword,
    search_targets,
    search_units,
)
from conftest import add_quotes


def test_targets_include_all_latest_quote_models(database):
    add_quotes(database, [
        ("XR", "64", "小花", 500), ("11 Pro", "64", "小花", 900), ("15 Pro", "256", "小花", 4000),
        ("15 Pro", "512", "小花", 4500), ("15", "128", "小花", 2600), ("16e", "128", "小花", 2400),
    ])
    rules = Rules(disabled_models=["15"])
    targets = search_targets(database, rules)
    assert [t["model"] for t in targets] == ["16e", "15", "15 Pro", "11 Pro", "XR"]
    assert targets[2]["storages"] == ["256", "512"]
    assert targets[1]["enabled"] is False
    assert {q["model"] for q in eligible_quotes(database, Rules())} == {"XR", "11 Pro", "15 Pro", "15", "16e"}


def test_search_units_are_per_model_and_series_can_be_disabled(database):
    add_quotes(database, [("17", "256", "小花", 4000), ("17 Pro", "256", "小花", 5000),
                         ("17 Pro", "512", "小花", 5500)])
    rules = Rules(disabled_models=["17 Pro|512"])
    units = search_units(database, rules)
    assert [(unit["model"], unit["storage"], unit["storages"]) for unit in units] == [
        ("17", None, ["256"]), ("17 Pro", None, ["256"]),
    ]
    rules.disabled_models.append("@series:17")
    assert search_units(database, rules) == []
    assert all(not target["series_enabled"] for target in search_targets(database, rules))


def test_rules_round_trip_and_defaults(database):
    rules = load_rules(database)
    assert rules.quote_grade == "小花"
    assert Rules.from_dict({"min_series": 17}).quote_grade == "小花"  # 旧筛选设置被忽略
    assert rules.passes == ["新发"]
    rules.region_city = "厦门市"
    rules.quote_grade = "大花"
    save_rules(database, rules)
    stored = load_rules(database)
    assert stored.region_city == "厦门" and stored.quote_grade == "大花"
    assert stored.region_steps == ["区域", "福建", "厦门", "查看"]
    assert stored.passes == ["新发"]


def test_legacy_sort_passes_are_ignored():
    # 旧配置中的降价排序不再参与采集。
    assert Rules.from_dict({"sort_passes": "新发, 降价"}).passes == ["新发"]


def test_region_path_can_be_customised():
    rules = Rules(region_city="厦门", region_path="区域 > 同城")
    assert rules.region_steps == ["区域", "同城"]
    assert Rules(region_city="").region_steps == []
    assert default_region_path("某某") == "区域 > 某某 > 查看"


def test_search_window_supports_overnight_ranges():
    day = Rules(search_start="09:00", search_end="23:00")
    assert in_search_window(day, datetime(2026, 10, 19, 9, 0))
    assert not in_search_window(day, datetime(2026, 10, 19, 23, 30))
    night = Rules(search_start="22:00", search_end="02:00")
    assert in_search_window(night, datetime(2026, 10, 19, 1, 0))
    assert not in_search_window(night, datetime(2026, 10, 19, 12, 0))


def test_search_keyword_overrides():
    assert search_keyword("15 Pro Max") == "iPhone 15 Pro Max"
    assert search_keyword("17 Air") == "iPhone Air"


async def test_quote_checks_skip_categories_successfully_synced_today(database):
    from app_monitor.config import settings
    from app_monitor.quotes import QuoteService, quote_status

    add_quotes(database, [("15", "128", "小花", 2600)], quote_date="2026-10-18")  # URL 日期滞后，但今天抓到
    service = QuoteService(database, settings)
    synced = []

    async def fake_sync(category_id, category_name=None):
        synced.append(category_id)
        return {"updated": False}

    service.sync = fake_sync
    await service.sync_if_stale(datetime.now().astimezone().date().isoformat())
    assert synced == ["100027"]
    assert quote_status(database)[0]["fresh"] is True


def test_region_status_understands_districts_and_other_places():
    assert region_status("福建厦门", "厦门") == "match"
    assert region_status("思明区", "厦门") == "match"
    assert region_status("福州", "厦门") == "other"
    assert region_status("浙江杭州", "厦门") == "other"
    assert region_status("广东", "厦门") == "other"
    assert region_status("福建", "厦门") == "unknown"
    assert region_status("某某镇", "厦门") == "unknown"


def test_storage_filter_steps_and_passes():
    rules = Rules()
    assert rules.storage_steps("256") == ["筛选", "存储容量", "256G", "确定"]
    assert rules.storage_steps("1T") == ["筛选", "存储容量", "1T", "确定"]
    assert rules.scroll_pages == 3
    assert Rules.from_dict({"storage_path": "筛选 > {容量} > ?确定"}).storage_steps("128") == ["筛选", "存储容量", "128G", "确定"]
    assert Rules.from_dict({"storage_path": "筛选 > {容量} > 确定"}).storage_steps("256") == ["筛选", "存储容量", "256G", "确定"]
    assert Rules(storage_path="筛选 > 存储容量 > {容量}").storage_steps("512") == ["筛选", "存储容量", "512G"]
    assert Rules().storage_steps("256") == ["筛选", "存储容量", "256G", "确定"]
    assert rules.passes == ["新发"]
