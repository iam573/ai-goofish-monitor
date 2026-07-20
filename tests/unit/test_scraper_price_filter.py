from src.scraper import (
    _filter_items_by_keyword_prefilter,
    _filter_items_by_price_range,
    _format_price_bound_for_input,
)
from src.services.price_history_service import parse_price_value


def test_filter_items_by_price_range_removes_items_above_max_price():
    items = [
        {"商品标题": "便宜商品", "当前售价": "¥900"},
        {"商品标题": "超价商品", "当前售价": "¥1200"},
    ]

    filtered = _filter_items_by_price_range(items, max_price="1000")

    assert [item["商品标题"] for item in filtered] == ["便宜商品"]


def test_filter_items_by_price_range_handles_common_display_price_formats():
    items = [
        {"商品标题": "正常后缀", "当前售价": "¥1,200起"},
        {"商品标题": "万元超价", "当前售价": "￥1.2万+"},
        {"商品标题": "人民币文本", "当前售价": "RMB 980 元 包邮"},
    ]

    filtered = _filter_items_by_price_range(items, max_price="1500")

    assert [item["商品标题"] for item in filtered] == ["正常后缀", "人民币文本"]


def test_filter_items_by_price_range_removes_items_below_min_price():
    items = [
        {"商品标题": "低价异常", "当前售价": "¥100"},
        {"商品标题": "正常商品", "当前售价": "¥500"},
    ]

    filtered = _filter_items_by_price_range(items, min_price="300")

    assert [item["商品标题"] for item in filtered] == ["正常商品"]


def test_filter_items_by_price_range_keeps_unparseable_prices():
    items = [
        {"商品标题": "未知价格", "当前售价": "价格异常"},
        {"商品标题": "正常商品", "当前售价": "¥500"},
    ]

    filtered = _filter_items_by_price_range(items, min_price="300", max_price="800")

    assert [item["商品标题"] for item in filtered] == ["未知价格", "正常商品"]


def test_filter_items_by_price_range_returns_original_items_without_bounds():
    items = [{"商品标题": "任意商品", "当前售价": "¥1200"}]

    filtered = _filter_items_by_price_range(items)

    assert filtered is items


def test_keyword_prefilter_removes_keyword_mode_items_without_include_match():
    items = [
        {"商品标题": "Aqara 窗帘伴侣 E1", "卖家昵称": "个人卖家"},
        {"商品标题": "普通窗帘挂钩", "卖家昵称": "个人卖家"},
    ]

    filtered = _filter_items_by_keyword_prefilter(
        items,
        decision_mode="keyword",
        keyword_rules=["窗帘伴侣"],
    )

    assert [item["商品标题"] for item in filtered] == ["Aqara 窗帘伴侣 E1"]


def test_keyword_prefilter_removes_excluded_items_before_detail_fetch():
    items = [
        {"商品标题": "窗帘伴侣 拆修过", "卖家昵称": "个人卖家"},
        {"商品标题": "窗帘伴侣 几乎全新", "卖家昵称": "个人卖家"},
    ]

    filtered = _filter_items_by_keyword_prefilter(
        items,
        decision_mode="keyword",
        keyword_rules=["窗帘伴侣"],
        exclude_keyword_rules=["拆修"],
    )

    assert [item["商品标题"] for item in filtered] == ["窗帘伴侣 几乎全新"]


def test_keyword_prefilter_keeps_ai_mode_items_without_include_match():
    items = [
        {"商品标题": "Aqara 窗帘伴侣 E1", "卖家昵称": "个人卖家"},
        {"商品标题": "普通窗帘挂钩", "卖家昵称": "个人卖家"},
    ]

    filtered = _filter_items_by_keyword_prefilter(
        items,
        decision_mode="ai",
        keyword_rules=["窗帘伴侣"],
    )

    assert filtered == items


def test_parse_price_value_supports_common_display_variants():
    assert parse_price_value("¥1,299起") == 1299.0
    assert parse_price_value("￥1.25万+") == 12500.0
    assert parse_price_value("RMB 980 元 包邮") == 980.0


def test_format_price_bound_for_input_returns_plain_number_text():
    assert _format_price_bound_for_input("¥1,200") == "1200"
    assert _format_price_bound_for_input("1.2万") == "12000"
    assert _format_price_bound_for_input("") is None
