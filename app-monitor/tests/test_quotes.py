import pytest

from app_monitor.quotes import OCRToken, QuoteImageParser, normalize_model, normalize_storage


def test_normalizers_handle_common_ocr_variants():
    assert normalize_storage("512GB") == "512"
    assert normalize_storage("IT") == "1T"
    assert normalize_model("15PRO MAX") == "15 Pro Max"
    assert normalize_model("17 AIR") == "17 Air"


@pytest.mark.parametrize("name,expected", [
    ("iPhone 15 Plus A3096", "15 Plus"),
    ("15 PLUS（A3096）", "15 Plus"),
    ("15 PlusA3096", "15 Plus"),
    ("15 Plus| A3096", "15 Plus"),
    ("15 Plus|", "15 Plus"),
    ("16 PRO MAX (a3296)", "16 Pro Max"),
    ("17 AIR", "17 Air"),
    ("16e", "16e"),
    ("XR", "XR"),
])
def test_normalize_model_strips_hardware_code(name, expected):
    assert normalize_model(name) == expected


def test_parser_does_not_include_hardware_code_in_quote_or_deduction_model():
    parser = QuoteImageParser()
    tokens = [
        OCRToken("15 Plus A3096", 0.03, 100),
        OCRToken("128", 0.24, 100),
        OCRToken("3000", 0.32, 100),
        OCRToken("港澳版-200", 0.75, 100),
    ]
    assert {entry.model for entry in parser.parse_tokens(tokens)} == {"15 Plus"}
    assert {entry.model for entry in parser.parse_deductions(tokens)} == {"15 Plus"}


def test_quote_parser_maps_capacity_and_grade_columns_to_nearest_model():
    parser = QuoteImageParser()
    tokens = [
        OCRToken("15 PRO", 0.03, 100),
        OCRToken("MAX", 0.04, 120),
        OCRToken("256", 0.24, 90),
        OCRToken("4500", 0.32, 90),
        OCRToken("4300", 0.41, 90),
        OCRToken("3900", 0.50, 90),
        OCRToken("16", 0.04, 300),
        OCRToken("128", 0.24, 300),
        OCRToken("3100", 0.32, 300),
    ]

    entries = parser.parse_tokens(tokens)

    values = {(entry.model, entry.storage, entry.grade): entry.price for entry in entries}
    assert values[("15 Pro Max", "256", "靓机")] == 4500
    assert values[("15 Pro Max", "256", "小花")] == 4300
    assert values[("16", "128", "靓机")] == 3100


# ---------------------------------------------------------------- 版式与扣费项
from app_monitor.quotes import (  # noqa: E402
    NO_WARRANTY_LAYOUT,
    WARRANTY_LAYOUT,
    detect_layout,
    merge_wrapped_lines,
    parse_deduction_text,
)


def line(text, x, y):
    return OCRToken(text, x, y)


def test_parse_deduction_text_handles_amount_range_and_notes():
    assert parse_deduction_text("港澳版-600-800") == ("港澳版", 600, 800, False, "deduct")
    assert parse_deduction_text("电池低于95%-100") == ("电池低于95%", 100, 100, False, "deduct")
    assert parse_deduction_text("水货2T-3000以上") == ("水货2T", 3000, 3000, True, "deduct")
    assert parse_deduction_text("电池低于67%扣50") == ("电池低于67%", 50, 50, False, "deduct")
    assert parse_deduction_text("屏大碎－300—600") == ("屏大碎", 300, 600, False, "deduct")
    assert parse_deduction_text("后盖破当大花收")[4] == "note"
    assert parse_deduction_text("电池低于60%按弹电")[4] == "note"


def test_merge_wrapped_lines_rejoins_cell_wraps_only():
    lines = [
        line("水货2T-3000以上", 0.74, 100), line("256G蓝色-300", 0.75, 119),
        line("512/1T/2T", 0.80, 138), line("银色黑色-", 0.80, 157), line("200", 0.82, 176),
        line("后三等级电池低于", 0.87, 195), line("95%-300", 0.90, 214),
        line("后彩", 0.90, 233), line("900", 0.90, 252),
        line("不分颜色", 0.76, 271), line("面容坏-300", 0.76, 290),
    ]
    assert merge_wrapped_lines(lines) == [
        "水货2T-3000以上", "256G蓝色-300", "512/1T/2T银色黑色-200", "后三等级电池低于95%-300",
        "后彩900", "不分颜色", "面容坏-300",
    ]


# 按 2026-10-20 无保报价原图中「17 PRO MAX」「17 Pro」两个区块的版面构造（文字按原图）
NO_WARRANTY_PAGE = [
    line("型号", 0.03, 864), line("容量", 0.24, 864), line("靓机", 0.33, 864), line("内爆可测", 0.651, 864),
    line("扣费项", 0.829, 864),
    line("17 PRO", 0.03, 1005), line("MAX", 0.04, 1035),
    line("256", 0.25, 922), line("6650", 0.32, 922), line("4550", 0.67, 922),
    line("512", 0.25, 988), line("6950", 0.32, 988),
    line("1T", 0.25, 1054), line("7950", 0.32, 1054),
    line("2T", 0.25, 1120), line("9450", 0.32, 1120),
    line("港澳版-600-800", 0.749, 963), line("水货三网-1300-1800", 0.741, 982),
    line("水货1T-2300-2500", 0.741, 1001), line("水货2T-3000以上", 0.745, 1020),
    line("256G蓝色-300", 0.757, 1039), line("512/1TG蓝色-500", 0.744, 1058), line("2T蓝色-600", 0.767, 1077),
    line("电池低于95%-100", 0.87, 935), line("电池低于90%-200", 0.87, 954),
    line("后三等级电池低于", 0.874, 973), line("95%-300", 0.902, 992),
    line("屏大碎-300-600", 0.876, 1011), line("水印-100", 0.903, 1030),
    line("后盖破当大花收", 0.88, 1106),
    line("17 Pro", 0.03, 1252),
    line("256", 0.25, 1186), line("5950", 0.32, 1186),
    line("512", 0.25, 1252), line("6550", 0.32, 1252),
    line("1T", 0.25, 1318), line("7350", 0.32, 1318),
    line("港澳版-600-800", 0.749, 1214), line("512G/1T蓝色-450", 0.744, 1290),
    line("电池低于95%-100", 0.87, 1167), line("屏大碎-300-600", 0.88, 1242),
]


def test_parse_deductions_groups_items_by_model_block_and_column():
    parser = QuoteImageParser()
    assert detect_layout(NO_WARRANTY_PAGE) is NO_WARRANTY_LAYOUT
    items = parser.parse_deductions(NO_WARRANTY_PAGE)
    by_model = {}
    for item in items:
        by_model.setdefault(item.model, []).append(item)
    pro_max = {item.name: item for item in by_model["17 Pro Max"]}
    assert pro_max["港澳版"].amount_min == 600 and pro_max["港澳版"].amount_max == 800
    assert pro_max["水货2T"].open_ended is True
    assert pro_max["后三等级电池低于95%"].amount_min == 300
    assert pro_max["后盖破当大花收"].kind == "note"
    assert {item.column for item in by_model["17 Pro Max"]} == {0, 1}
    pro = {item.name for item in by_model["17 Pro"]}
    assert pro == {"港澳版", "512G/1T蓝色", "电池低于95%", "屏大碎"}


def test_warranty_layout_skips_the_extra_grade_column():
    parser = QuoteImageParser()
    page = [
        line("高保充新", 0.297, 808), line("靓机", 0.40, 808), line("颜色差价", 0.793, 808),
        line("18 PRO", 0.03, 905), line("MAX", 0.04, 930),
        line("256", 0.24, 842), line("9550", 0.31, 842), line("9250", 0.40, 842), line("8950", 0.485, 842),
        line("512", 0.24, 892), line("1T", 0.24, 942), line("2T", 0.24, 992),
        line("512/1T/2T", 0.80, 898), line("银色黑色-", 0.80, 917), line("200", 0.82, 936),
        line("要求全套", 0.90, 907), line("要求充电10次内", 0.885, 927),
    ]
    assert detect_layout(page) is WARRANTY_LAYOUT
    prices = {(e.storage, e.grade): e.price for e in parser.parse_tokens(page)}
    assert prices == {("256", "靓机"): 9250, ("256", "小花"): 8950}
    items = {item.name: item for item in parser.parse_deductions(page)}
    assert items["512/1T/2T银色黑色"].amount_min == 200
    assert items["要求全套"].kind == "note"


def test_quote_reference_filters_deductions_by_storage(database):
    from conftest import add_quotes
    from app_monitor.quotes import (
        APPLE_NO_WARRANTY_CATEGORY, APPLE_WARRANTY_CATEGORY, QuoteDeduction, _save_deductions, quote_reference,
    )

    batch_id = add_quotes(database, [("15 Pro", "256", "靓机", 4700), ("15 Pro", "256", "小花", 4500),
                                     ("15 Pro", "512", "小花", 4900)])
    add_quotes(database, [("15 Pro", "256", "小花", 4800)], category_id=APPLE_WARRANTY_CATEGORY)
    with database.connect() as connection:
        _save_deductions(connection, batch_id, APPLE_NO_WARRANTY_CATEGORY, [
            QuoteDeduction("15 Pro", 0, 0, "换屏-300", "换屏", 300, 300),
            QuoteDeduction("15 Pro", 0, 1, "512/1T银色黑色-200", "512/1T银色黑色", 200, 200),
            QuoteDeduction("15 Pro", 1, 0, "后盖破当大花收", "后盖破当大花收", kind="note"),
        ])
    reference = quote_reference(database, "15 Pro", "256", APPLE_NO_WARRANTY_CATEGORY, "小花")
    assert [g["grade"] for g in reference["grades"]] == ["靓机", "小花"]
    assert reference["other_price"] == 4800
    assert [d["name"] for d in reference["deductions"]] == ["换屏"]
    assert reference["notes"] == ["后盖破当大花收"]
    other = quote_reference(database, "15 Pro", "512", APPLE_NO_WARRANTY_CATEGORY, "小花")
    assert [d["name"] for d in other["deductions"]] == ["换屏", "512/1T银色黑色"]
    assert other["other_price"] is None
