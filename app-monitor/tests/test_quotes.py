from app_monitor.quotes import OCRToken, QuoteImageParser, normalize_model, normalize_storage


def test_normalizers_handle_common_ocr_variants():
    assert normalize_storage("512GB") == "512"
    assert normalize_storage("IT") == "1T"
    assert normalize_model("15PRO MAX") == "15 Pro Max"
    assert normalize_model("17 AIR") == "17 Air"


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
