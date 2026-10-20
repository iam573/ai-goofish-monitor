from app_monitor.matching import (
    evaluate,
    extract_storage,
    model_in_title,
    normalize_search_text,
    recognize_apple_product,
)
from app_monitor.rules import Rules

QUOTES = [
    {"id": 1, "model": "15 Pro", "storage": "256", "grade": "小花", "price": 4000},
    {"id": 2, "model": "15 Pro Max", "storage": "256", "grade": "小花", "price": 4800},
    {"id": 3, "model": "15", "storage": "128", "grade": "小花", "price": 2600},
    {"id": 4, "model": "17 Air", "storage": "256", "grade": "小花", "price": 4200},
]


def item(title, price, region=""):
    return {"item_id": "x", "title": title, "price": price, "display_region": region}


def test_extract_storage_requires_explicit_capacity_unit():
    assert extract_storage("iPhone 15 Pro 256G 国行") == "256"
    assert extract_storage("iPhone 15 Pro 1TB") == "1T"
    assert extract_storage("ｉＰｈｏｎｅ 15 ２５６ＧＢ") == "256"
    assert extract_storage("iPhone 15 Pro 九成新") is None
    assert extract_storage("16promax 256 沙漠色") == "256"
    assert extract_storage("iPhone 15 Pro 电池 100% 九成新") is None
    assert extract_storage("iPhone 15 Pro ¥256 急出") is None
    assert extract_storage("iPhone 15 Pro 5128") is None


def test_search_normalization_removes_zero_width_characters_and_spaces():
    title = "A​p​p​l​e i​P​h​o​n​e 15 Pro 256GB"
    assert normalize_search_text("iPhone 15 Pro 256G") in normalize_search_text(title)


def test_model_in_title_respects_variant_and_digit_boundaries():
    assert model_in_title("15", "iPhone 15 国行 99新 256G")
    assert model_in_title("15", "iPhone15 128G")
    assert model_in_title("16 Pro Max", "16promax 256 沙漠色")
    assert not model_in_title("15", "iPhone 1599")
    assert not model_in_title("15", "iPhone 15 Pro 256G")
    assert not model_in_title("15 Pro", "苹果15promax 256")
    assert not model_in_title("12", "iPhone 15 512G")
    assert not model_in_title("16", "iPhone 16e 128G")
    assert model_in_title("17 Air", "iPhone Air 256G 国行")


def test_seller_shorthand_and_zero_width_titles():
    products = [{"model": m, "storage": s} for m in ["15", "15 Pro", "15 Pro Max", "15 Plus"] for s in ["256", "512"]]
    assert recognize_apple_product("15p\u200b256黑色 国行 全原", products) == ("15 Pro", "256")
    assert recognize_apple_product("15pm 512 原色", products) == ("15 Pro Max", "512")
    assert recognize_apple_product("iPhone15plus 256", products) == ("15 Plus", "256")
    zero_width = "i\u200bP\u200bh\u200bo\u200bn\u200be\u200b15 p\u200br\u200bo\u200b 256G 原色 国行无拆无修"
    assert recognize_apple_product(zero_width, products) == ("15 Pro", "256")


def test_recognize_apple_product_prefers_the_most_specific_model():
    products = [{"model": m, "storage": "256"} for m in ["13 Pro", "13 Pro Max"]]
    assert recognize_apple_product("苹果13pro 256g", products) == ("13 Pro", "256")
    assert recognize_apple_product("iPhone 13 Pro Max 256GB", products) == ("13 Pro Max", "256")
    assert recognize_apple_product("苹果13pro", products) is None


def test_listing_price_can_exceed_recycler_quote_by_configured_amount():
    quotes = [{"id": 10, "model": "17", "storage": "512", "grade": "小花", "price": 4900}]
    rules = Rules(max_over_quote=200)
    title = "苹果 iPhone 17 512G 国行"

    at_limit = evaluate(item(title, 5100), rules, quotes)
    assert at_limit.matched is True
    assert at_limit.threshold_price == 5100
    assert at_limit.expected_margin == -200
    assert "高于回收报价 ¥200" in at_limit.reason

    above_limit = evaluate(item(title, 5101), rules, quotes)
    assert above_limit.matched is False
    assert above_limit.code == "price_high"
    assert above_limit.threshold_price == 5100

    below_quote = evaluate(item(title, 4800), rules, quotes)
    assert below_quote.matched is True
    assert "低于回收报价 ¥100" in below_quote.reason


def test_legacy_price_fields_do_not_restore_old_discount_rule():
    rules = Rules.from_dict({"min_margin": 300, "max_quote_ratio": 1.0})
    assert rules.max_over_quote == 200
    assert "min_margin" not in rules.to_dict()


def test_model_is_recognised_from_title_not_search_keyword():
    decision = evaluate(item("15 Pro Max 256GB 国行", 4300), Rules(), QUOTES)
    assert decision.matched is True
    assert decision.model == "15 Pro Max"
    assert decision.quote_price == 4800


def test_search_target_does_not_accept_other_model_variants():
    quotes = [
        {"id": 20, "model": "17", "storage": "256", "grade": "小花", "price": 4000},
        {"id": 21, "model": "17 Pro", "storage": "256", "grade": "小花", "price": 5000},
    ]
    decision = evaluate(
        item("iPhone 17 Pro 256G 国行", 3000), Rules(), quotes, expected_model="17"
    )
    assert not decision.matched
    assert decision.model == "17 Pro"
    assert "与当前搜索目标 17 不一致" in decision.reason

    base = evaluate(item("iPhone 17 256G 国行", 3000), Rules(), quotes, expected_model="17")
    assert base.matched and base.model == "17"


def test_alias_model_matches_seller_wording():
    decision = evaluate(item("iPhone Air 256G 国行 在保", 3800), Rules(), QUOTES)
    assert decision.model == "17 Air"
    assert decision.matched is True


def test_excluded_keywords_and_too_cheap_listings_are_rejected():
    rules = Rules()
    assert "回收" in evaluate(item("高价回收 iPhone 15 Pro 256G", 100), rules, QUOTES).reason
    assert "ID锁" in evaluate(item("iPhone 15 Pro 256G id锁 便宜出", 800), rules, QUOTES).reason
    cheap = evaluate(item("iPhone 15 Pro 256G 国行", 999), rules, QUOTES)
    assert cheap.matched is False and "疑似引流" in cheap.reason


def test_normal_descriptions_are_not_excluded():
    title = "iPhone15 pro 256G 原色 国行无拆无修 边框轻微手机壳轻微磨损 配件齐全 主板无修 无维修"
    assert evaluate(item(title, 2900), Rules(), QUOTES).matched is True


def test_old_default_exclusions_are_upgraded():
    from app_monitor.rules import _OLD_DEFAULT_EXCLUDE_KEYWORDS, DEFAULT_EXCLUDE_KEYWORDS
    assert Rules.from_dict({"exclude_keywords": _OLD_DEFAULT_EXCLUDE_KEYWORDS}).exclude_keywords == DEFAULT_EXCLUDE_KEYWORDS
    assert Rules.from_dict({"exclude_keywords": "回收,自定义"}).exclude_keywords == "回收,自定义"


def test_unknown_storage_or_model_is_not_pushed():
    assert evaluate(item("iPhone 15 Pro 九成新", 100), Rules(), QUOTES).reason == "标题中无法识别容量"
    assert "无法对应" in evaluate(item("iPhone 14 256G", 2000), Rules(), QUOTES).reason


def test_region_rule_checks_card_region_and_in_app_filter():
    rules = Rules(region_city="厦门")
    listing = "iPhone 15 Pro 256G 国行"
    assert evaluate(item(listing, 3500, "福建厦门"), rules, QUOTES).matched is True
    other = evaluate(item(listing, 3500, "上海"), rules, QUOTES)
    assert other.matched is False and "不是厦门" in other.reason
    assert evaluate(item(listing, 3500, "思明区"), rules, QUOTES).matched is True
    assert evaluate(item(listing, 3500, "福州"), rules, QUOTES).matched is False
    assert evaluate(item(listing, 3500, "福建"), rules, QUOTES, region_applied=True).matched is True
    unknown = evaluate(item(listing, 3500), rules, QUOTES)
    assert unknown.matched is False and "无法确认" in unknown.reason
    trusted = evaluate(item(listing, 3500), rules, QUOTES, region_applied=True)
    assert trusted.matched is True and trusted.region == "厦门"
