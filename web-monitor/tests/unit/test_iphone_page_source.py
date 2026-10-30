from src.infrastructure.external.iphone.page_source import (
    PhoneCard,
    detail_content,
    detail_matches,
    detail_mismatch_reason,
    detail_seller_name,
    is_search_results,
    item_id_from_share,
    parse_cards,
    search_entry_points,
)


def test_card_parser_keeps_title_and_price_inside_same_card():
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeOther>
        <XCUIElementTypeCell x="0" y="400" width="210" height="300">
          <XCUIElementTypeStaticText value="Sony A7M4 单机身 自用" x="8" y="500" />
          <XCUIElementTypeStaticText value="¥9800" x="8" y="600" />
          <XCUIElementTypeStaticText value="思明区" x="8" y="650" />
        </XCUIElementTypeCell>
        <XCUIElementTypeCell x="215" y="400" width="210" height="300">
          <XCUIElementTypeStaticText value="Sony A7M4 套机 国行" x="220" y="510" />
          <XCUIElementTypeStaticText value="¥12500" x="220" y="610" />
          <XCUIElementTypeStaticText value="广东" x="220" y="660" />
        </XCUIElementTypeCell>
      </XCUIElementTypeOther>
    </XCUIElementTypeApplication>"""
    assert [(card.title, card.price, card.region) for card in parse_cards(source)] == [
        ("Sony A7M4 单机身 自用", 9800, "思明区"),
        ("Sony A7M4 套机 国行", 12500, "广东"),
    ]


def test_card_parser_reads_price_other_node_and_skips_hidden_cards():
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeCell>
        <XCUIElementTypeStaticText name="Sony A7M4 单机身 自用" x="12" y="700" />
        <XCUIElementTypeOther name="9800元" x="12" y="750" visible="true" />
      </XCUIElementTypeCell>
      <XCUIElementTypeCell>
        <XCUIElementTypeStaticText name="Sony A7M4 套机 国行" x="220" y="1100" />
        <XCUIElementTypeOther name="12500元" x="220" y="1150" visible="false" />
      </XCUIElementTypeCell>
    </XCUIElementTypeApplication>"""
    assert [(card.title, card.price) for card in parse_cards(source)] == [("Sony A7M4 单机身 自用", 9800)]


def test_card_parser_never_pairs_title_and_price_across_cards():
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeOther>
        <XCUIElementTypeStaticText value="¥100" x="8" y="600" />
        <XCUIElementTypeStaticText value="¥200" x="8" y="620" />
        <XCUIElementTypeStaticText value="只有一个标题的分组" x="8" y="500" />
      </XCUIElementTypeOther>
    </XCUIElementTypeApplication>"""
    assert parse_cards(source) == []


def test_share_parser_uses_goofish_item_id_not_tracking_id():
    share = "来闲鱼看看 https://m.tb.cn/h.ABCD123"
    page = 'trackingId=9999999999999&amp;url=https://h5.m.goofish.com/item?id=1091531543430&amp;source=share'
    assert item_id_from_share(share, page) == "1091531543430"
    assert item_id_from_share(share, "trackingId=9999999999999") == ""
    assert item_id_from_share("https://www.goofish.com/item?id=1091531543430", "") == "1091531543430"


def test_detail_identity_requires_title_prefix_and_same_price():
    card = PhoneCard("几乎全新 Sony A7M4，快门 3000", 9800, "厦门", 20, 700)
    source = """<XCUIElementTypeApplication>
      <XCUIElementTypeStaticText name="9800" y="157" />
      <XCUIElementTypeTextView name="几乎全新Sony A7M4，快门3000。送两块电池，支持自提。" />
    </XCUIElementTypeApplication>"""
    assert detail_matches(source, card)
    assert "价格不匹配" in detail_mismatch_reason(source.replace('name="9800"', 'name="9900"'), card)
    other = source.replace("几乎全新Sony A7M4", "尼康 Z6II")
    assert "标题不匹配" in detail_mismatch_reason(other, card)


def detail_source(seller="测试卖家"):
    return f'''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeOther name="分享按钮" x="354" y="53" width="24" height="24" visible="true" />
      <XCUIElementTypeOther name="用户头像" x="12" y="99" width="32" height="32" visible="true" />
      <XCUIElementTypeOther name="{seller}" x="52" y="99" width="80" height="32" visible="true" />
      <XCUIElementTypeStaticText name="厦门" x="340" y="108" width="28" height="15" visible="true" />
      <XCUIElementTypeTextView name="描述正文，卖家测试卖家" x="12" y="300" width="360" height="80" visible="true" />
      <XCUIElementTypeOther name="商品参数">
        <XCUIElementTypeStaticText name="品牌 Sony" />
        <XCUIElementTypeStaticText name="成色 几乎全新" />
      </XCUIElementTypeOther>
    </XCUIElementTypeApplication>'''


def test_detail_seller_is_read_beside_avatar_only():
    assert detail_seller_name(detail_source()) == "测试卖家"
    assert detail_seller_name(detail_source().replace('name="用户头像"', 'name="其他图片"')) == ""
    assert detail_seller_name(detail_source().replace('name="分享按钮"', 'name="图片搜索"')) == ""


def test_detail_content_collects_description_and_parameters():
    content = detail_content(detail_source())
    assert content["description"] == "描述正文，卖家测试卖家"
    assert content["parameters"] == ["品牌 Sony", "成色 几乎全新"]


def scrolled_result_source():
    return '''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeButton name="图片搜索" x="300" y="60" visible="true" />
      <XCUIElementTypeStaticText name="sony a7m4" x="60" y="62" width="100" visible="true" />
      <XCUIElementTypeStaticText name="新发" x="218" y="-40" visible="false" />
      <XCUIElementTypeStaticText name="筛选" x="343" y="-40" visible="false" />
    </XCUIElementTypeApplication>'''


def test_collapsed_sort_bar_still_identifies_list_but_not_detail_or_modal():
    source = scrolled_result_source()
    assert is_search_results(source)
    for control in ("分享按钮", "返回按钮", "确定"):
        assert not is_search_results(source.replace(
            "</XCUIElementTypeApplication>",
            f'<XCUIElementTypeButton name="{control}" visible="true" /></XCUIElementTypeApplication>',
        ))


def test_search_entry_taps_inside_home_search_bar_only():
    source = '''<XCUIElementTypeApplication width="390" height="844">
      <XCUIElementTypeButton name="首页" x="8" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="同城" x="78" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="消息" x="240" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="我的" x="310" y="761" width="70" visible="true" />
      <XCUIElementTypeButton name="ipad pro2018" traits="SearchField" x="8" y="97" width="374" height="36" visible="true" />
      <XCUIElementTypeStaticText name="ipad pro2018" x="49" y="99" width="92" height="32" visible="true" />
      <XCUIElementTypeStaticText name="集市补贴中" x="71" y="52" width="80" height="40" visible="true" />
    </XCUIElementTypeApplication>'''
    points = search_entry_points(source)
    assert (points[0]["x"], points[0]["y"]) == (95, 115)
    assert all(97 <= point["y"] <= 133 for point in points)


def test_search_entry_uses_result_page_keyword():
    points = search_entry_points(scrolled_result_source())
    assert [point["target"] for point in points] == ["sony a7m4"]
