import json

import pytest

from app_monitor.proxy_protocol import parse_search, parse_detail, request_meta, verification, price

ID = "1093306353441"
OTHER = "1092690649226"


def card(key=ID, title="iPhone 15 Pro 256G", amount="2799"):
    return {"data": {"item": {"main": {"exContent": {"itemId": key, "title": title,
        "price": [{"text": "¥"}, {"text": amount}], "area": "湖里区",
        "picUrl": "https://img.alicdn.com/product.jpg", "userNickName": "测试卖家"},
        "clickParam": {"args": {"publishTime": "1792981200000"}}}}}}


def test_search_uses_main_cards_not_recommendations_or_non_product_numbers():
    first = card()
    first["data"]["item"]["main"]["recommendations"] = [{"itemId": OTHER, "title": "推荐", "price": "1"}]
    payload = {"data": {"resultList": [first, card(), {"data": {"title": "广告", "id": OTHER}}],
                        "recommendations": [card(OTHER)], "hasNextPage": "true"}, "token": "SECRET"}
    parsed = parse_search(payload)
    assert [item["item_id"] for item in parsed["items"]] == [ID]
    item = parsed["items"][0]
    assert item["price"] == 2799 and item["display_region"] == "湖里区"
    assert item["image_urls"] == ["https://img.alicdn.com/product.jpg"]
    assert item["publish_time"] and parsed["has_more"] is True
    assert "SECRET" not in json.dumps(parsed)


def test_search_preserves_long_description_and_refuses_silent_clipping():
    body = "正常使用，" * 80 + "换过电池"
    assert parse_search({"data": {"resultList": [card(title=body)]}})["items"][0]["title"] == body
    assert parse_search({"data": {"resultList": [card(title="字" * 20_001)]}})["items"] == []
    assert parse_search({"data": {"otherList": [card()]}}) is None


@pytest.mark.parametrize("value", ["1000～2000", "定金200", "0", -1, True, "inf", {"trackingId": ID}])
def test_price_does_not_guess_ranges_deposits_or_arbitrary_numbers(value):
    assert price(value) is None


def test_detail_binds_body_to_requested_id_and_excludes_other_products_and_seller():
    body = "iPhone 15 Pro 256G，" + "细微划痕，" * 40 + "电池78%，换过后玻璃"
    payload = {"data": {"globalData": {"item": {"itemId": ID, "desc": body, "soldPrice": "2799",
        "attributes": [{"name": "容量", "value": "256G", "token": "SECRET"}],
        "images": [{"url": "https://img.alicdn.com/a.jpg"}, {"url": "https://img.alicdn.com/b.jpg?token=SECRET"}],
        "seller": {"desc": "卖家介绍"}, "recommend": {"desc": "推荐介绍"},
        "related": {"itemId": OTHER, "desc": "另一件商品"}}}}, "token": "SECRET"}
    parsed = parse_detail(payload, ID)
    assert parsed["description"] == body and parsed["price"] == 2799
    assert parsed["detail_parameters"] == ["容量：256G"]
    assert parsed["image_urls"] == ["https://img.alicdn.com/a.jpg"]
    assert "SECRET" not in json.dumps(parsed)
    assert parse_detail(payload, OTHER) is None
    assert parse_detail({"data": {"itemId": OTHER, "desc": body}}, ID) is None


def test_conflicting_own_descriptions_are_not_silently_selected():
    payload = {"data": {"itemId": ID, "desc": "原装无维修", "nested": {"description": "更换第三方屏"}}}
    assert parse_detail(payload, ID) is None


def test_request_meta_keeps_context_but_no_credentials_or_opaque_cursors():
    meta = request_meta({"keyword": "iPhone 15 Pro", "pageNumber": 2, "page": 1, "city": "厦门",
                         "sortField": "create", "sortValue": "desc", "token": "SECRET", "cursor": "SECRET"})
    assert meta["page"] == 2 and meta["sort_field"] == "create"
    assert "SECRET" not in json.dumps(meta) and "city" not in meta
    assert request_meta({"city": "福州"})["filter_fingerprint"] != meta["filter_fingerprint"]
    assert verification({"ret": ["FAIL_SYS_USER_VALIDATE::验证"]})
    assert verification({}, 420)
    assert not verification({"ret": ["SUCCESS::成功"]})
