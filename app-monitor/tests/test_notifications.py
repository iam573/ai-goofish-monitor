from dataclasses import replace
import json

import httpx

from app_monitor.config import settings
from app_monitor import notifications
from app_monitor.notifications import NotificationService
import pytest


async def test_stop_does_not_send_to_remaining_notification_channels(monkeypatch):
    from threading import Event
    from app_monitor.stop_control import stop_scope
    event, requests = Event(), []

    def handler(request):
        requests.append(request)
        event.set()
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, "AsyncClient", lambda *a, **kw:
                        real_client(*a, transport=httpx.MockTransport(handler), **kw))
    service = NotificationService(replace(settings, ntfy_topic_url="https://ntfy.test/topic",
                                          bark_url="https://bark.test/key", webhook_url="https://webhook.test/push"))
    with stop_scope(event):
        result = await service.send({"title": "test", "price": 10}, "test")
    assert result == {"ntfy": {"success": True, "status_code": 200}}
    assert len(requests) == 1


@pytest.mark.parametrize("shop, suffix", [(True, "（鱼小铺）"), (False, ""), (None, "")])
def test_seller_and_shop_are_shown_in_plain_and_markdown_notifications(shop, suffix):
    item = {"title": "iPhone 17 自用", "price": 4500, "seller_name": "小鱼[店铺]", "is_fish_shop": shop}
    _, content = NotificationService._format_message(item, "", "", [])
    markdown = NotificationService._format_webhook_content(item, "", "", [])
    assert f"卖家：小鱼[店铺]{suffix}" in content
    assert f"卖家：小鱼\\[店铺\\]{suffix}" in markdown
    assert "鱼小铺：" not in content + markdown


def test_mobile_link_uses_share_page_only_for_real_item_ids():
    link = NotificationService._mobile_link("https://www.goofish.com/item?id=1094279560524")
    assert link.startswith("https://pages.goofish.com/sharexy?")
    assert "%7B%22id%22%3A1094279560524%7D" in link
    assert NotificationService._mobile_link("https://www.goofish.com/item?id=1") == "https://www.goofish.com/item?id=1"


def test_webhook_template_renders_nested_values():
    rendered = NotificationService._render_template(
        {"title": "{{title}}", "nested": ["${content}"]},
        {"title": "商品", "content": "价格合适"},
    )

    assert rendered == {"title": "商品", "nested": ["价格合适"]}


def test_notification_has_category_heading_and_product_on_next_line():
    title, content = NotificationService._format_message(
        {
            "title": "自用国行 iPhone 17 128G，成色好",
            "model": "17",
            "storage": "128",
            "price": 5100,
            "quote_price": 4900,
            "quote_grade": "小花",
            "display_region": "厦门思明",
            "item_url": "https://www.goofish.com/item?id=1090413599999",
        },
        "",
        "https://pages.goofish.com/sharexy?example=1",
        ["https://example.test/image.jpg"],
    )

    assert title == "iPhone 17 128G"
    assert content.startswith("自用国行 iPhone 17 128G，成色好\n\n挂牌价：¥5100")
    assert "小花回收报价：¥4900\n挂牌价高于回收报价：¥200\n地区：厦门思明" in content
    assert "打开商品：https://pages.goofish.com/sharexy?example=1" in content
    assert "网页链接：https://www.goofish.com/item?id=1090413599999" in content
    assert "![商品图片](https://example.test/image.jpg)" in content


def test_notification_supports_terabyte_and_test_push_without_category():
    title, content = NotificationService._format_message(
        {"title": "17 Pro Max 1T", "model": "17 Pro Max", "storage": "1T", "price": 4500, "quote_price": 4900},
        "",
        "",
        [],
    )
    assert title == "iPhone 17 Pro Max 1TB"
    assert "挂牌价低于回收报价：¥400" in content
    test_title, test_content = NotificationService._format_message(
        {"title": "[测试] App Monitor 推送", "price": 0}, "推送渠道配置有效。", "", []
    )
    assert test_title == "[测试] App Monitor 推送"
    assert "原因：推送渠道配置有效。" in test_content


async def test_send_webhook_preserves_heading_and_line_breaks(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        notifications.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(
            *args, transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    service = NotificationService(replace(
        settings,
        ntfy_topic_url="",
        bark_url="",
        webhook_url="https://push.example.test/hook",
        webhook_headers="",
        webhook_body='{"title":"{{title}}","content":"{{content}}"}',
        webhook_method="POST",
        webhook_content_type="JSON",
        webhook_query_parameters="",
    ))
    result = await service.send(
        {
            "title": "国行 iPhone 17 128G 自用",
            "model": "17",
            "storage": "128",
            "price": 5100,
            "quote_price": 4900,
            "quote_grade": "小花",
            "item_url": "https://www.goofish.com/item?id=1090413599999",
        },
        "命中",
    )

    assert result["webhook"]["success"] is True
    assert captured["payload"]["title"] == "iPhone 17 128G"
    assert captured["payload"]["content"].startswith(
        "[**国行 iPhone 17 128G 自用**](https://pages.goofish.com/sharexy?"
    )
    assert "**挂牌价：¥5100**  \n小花回收报价：¥4900  \n挂牌价高于回收报价：¥200" in captured["payload"]["content"]
    assert "[📱 手机打开](https://pages.goofish.com/sharexy?" in captured["payload"]["content"]
    assert "[💻 网页打开](https://www.goofish.com/item?id=1090413599999)" in captured["payload"]["content"]


def test_markdown_content_escapes_product_title_and_shows_image():
    content = NotificationService._format_webhook_content(
        {"title": "[全新] iPhone *17*", "price": 4500, "item_url": "https://www.goofish.com/item?id=1090413599999"},
        "符合条件",
        "",
        ["https://example.test/image.jpg"],
    )
    assert content.startswith("[**\\[全新\\] iPhone \\*17\\***](https://www.goofish.com/item?id=1090413599999)")
    assert "**挂牌价：¥4500**  \n原因：符合条件" in content
    assert "[🔗 打开商品](https://www.goofish.com/item?id=1090413599999)" in content
    assert content.endswith("![商品主图](https://example.test/image.jpg)")


async def test_custom_webhook_uses_headers_and_body_template():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    service = NotificationService(
        replace(
            settings,
            webhook_url="https://push.example.test/hook",
            webhook_headers='{"X-Token":"secret"}',
            webhook_body='{"title":"{{title}}","content":"{{content}}"}',
            webhook_method="POST",
            webhook_content_type="JSON",
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await service._send_webhook(
            client,
            {
                "title": "iPhone 15 Pro",
                "price": 3000,
                "keyword": "iPhone",
                "item_url": "https://www.goofish.com/item?id=1",
            },
            "价差 ¥500",
            "iPhone 15 Pro",
            "价格：¥3000",
        )
        await response

    request = captured["request"]
    assert request.headers["X-Token"] == "secret"
    assert json.loads(request.content) == {"title": "iPhone 15 Pro", "content": "价格：¥3000"}


REFERENCE_ITEM = {
    "title": "iPhone 15 Pro 256G 自用", "model": "15 Pro", "storage": "256", "price": 4650,
    "quote_price": 4500, "quote_grade": "小花", "display_region": "厦门",
    "item_url": "https://www.goofish.com/item?id=1090413599999",
    "quote_reference": {
        "category_name": "苹果无保", "quote_date": "2026-10-21", "grade": "小花",
        "grades": [{"grade": "靓机", "price": 4700}, {"grade": "小花", "price": 4500}, {"grade": "大花", "price": 4200}],
        "other_category_name": "苹果有保", "other_price": 4800,
        "deductions": [
            {"name": "换屏", "amount_min": 300, "amount_max": 300, "open_ended": False},
            {"name": "电池低于80%", "amount_min": 200, "amount_max": 300, "open_ended": False},
            {"name": "有锁", "amount_min": 1500, "amount_max": 1500, "open_ended": True},
        ],
        "notes": ["后盖破当大花收", "不分颜色"],
    },
}


def test_reference_prices_come_last_in_plain_text():
    title, content = NotificationService._format_message(REFERENCE_ITEM, "", "https://pages.goofish.com/sharexy?x=1", [])
    assert title == "iPhone 15 Pro 256G"
    assert content.startswith(
        "iPhone 15 Pro 256G 自用\n\n挂牌价：¥4650\n小花回收报价：¥4500\n挂牌价高于回收报价：¥150\n地区：厦门\n\n"
        "打开商品：https://pages.goofish.com/sharexy?x=1\n网页链接：https://www.goofish.com/item?id=1090413599999\n\n"
        "📋 iPhone 15 Pro 256G 各成色回收价（苹果无保 10-21）\n"
    )
    assert "靓机 4,700 ｜ 小花 4,500 ✓ ｜ 大花 4,200\n有保·小花 4,800" in content
    assert "🔧 iPhone 15 Pro 256G 扣费项\n· 换屏  −300\n· 电池低于80%  −200～300\n· 有锁  −1,500 起" in content
    assert '扣后回收价' not in content and ' → ' not in content
    assert content.endswith("💬 后盖破当大花收；不分颜色")


def test_markdown_keeps_classic_header_and_puts_reference_tables_last():
    markdown = NotificationService._format_webhook_content(
        REFERENCE_ITEM, "", "https://pages.goofish.com/sharexy?x=1", ["https://example.test/a.jpg"]
    )
    head, tail = markdown.split("\n\n---\n\n")
    assert head == (
        "[**iPhone 15 Pro 256G 自用**](https://pages.goofish.com/sharexy?x=1)\n\n"
        "**挂牌价：¥4650**  \n小花回收报价：¥4500  \n挂牌价高于回收报价：¥150  \n地区：厦门\n\n"
        "[📱 手机打开](https://pages.goofish.com/sharexy?x=1) ｜ [💻 网页打开](https://www.goofish.com/item?id=1090413599999)\n\n"
        "![商品主图](https://example.test/a.jpg)"
    )
    assert "|" not in head
    assert tail.startswith("**📋 iPhone 15 Pro 256G 各成色回收价**（苹果无保 10-21）\n\n| 靓机 | 小花 | 大花 | 有保·小花 |")
    assert "| 4,700 | **4,500** | 4,200 | 4,800 |" in tail
    assert "**🔧 iPhone 15 Pro 256G 扣费项**" in tail
    assert "| 项目 | 扣费 |\n| :-- | --: |\n| 换屏 | −300 |" in tail
    assert "| 电池低于80% | −200～300 |" in tail
    assert "| 有锁 | −1,500 起 |" in tail
    assert '扣后回收价' not in tail
    assert tail.endswith("> 💬 后盖破当大花收；不分颜色")


def test_pending_push_keeps_product_status_and_value_without_review_reasons():
    assessment = {'status': 'pending', 'grade': None, 'reference_grade': '小花',
                  'value_low': None, 'value_high': 4500,
                  'uncertainties': ['成色无法确定', '可能适用的报价备注尚未识别：电池鼓-80']}
    item = {**REFERENCE_ITEM, 'smart_assessment': assessment}
    title, plain = NotificationService._format_message(item, '待确认原因不应重复输出', '', [])
    markdown = NotificationService._format_webhook_content(item, '待确认原因不应重复输出', '', [])
    assert title == '【待确认】iPhone 15 Pro 256G'
    for body in (plain, markdown):
        assert '成色待确认' in body and '回收参考：≤ ¥4500（小花参考，待确认）' in body
        assert item['item_url'] in body
        assert '待确认：' not in body and '待确认原因不应重复输出' not in body
        assert all(reason not in body for reason in assessment['uncertainties'])
        assert '扣后回收价' not in body
    assert assessment['uncertainties'] == ['成色无法确定', '可能适用的报价备注尚未识别：电池鼓-80']


@pytest.mark.parametrize('deductions,summary', [
    ([], '未扣费'),
    ([{'name':'弹电','min':300,'max':300}], '¥300'),
    ([{'name':'弹电','min':100,'max':300}], '¥100（按区间最低值）'),
    ([{'name':'弹电','min':100,'max':300},{'name':'港澳版','min':200,'max':200}], '¥300（按区间最低值）'),
    ([{'name':'轻微老化','min':100,'max':100,'open_ended':True}], '至少 ¥100'),
])
def test_reference_price_and_deduction_basis_use_separate_lines(deductions,summary):
    assessment={'status':'pending','grade':None,'reference_grade':'小花','base_price':1300,
                'value_low':None,'value_high':1000,'deductions':deductions}
    item={**REFERENCE_ITEM,'smart_assessment':assessment}
    for body in [NotificationService._format_message(item,'','',[])[1],
                 NotificationService._format_webhook_content(item,'','',[])]:
        assert '回收参考：≤ ¥1000（小花参考，待确认）' in body
        assert f'本次扣费：{summary}' in body
        if deductions:assert '基础回收价：¥1300' in body and '扣费明细：' in body
        assert '待确认：' not in body


def test_confirmed_value_keeps_its_deduction_on_a_separate_line():
    item={**REFERENCE_ITEM,'smart_assessment':{'status':'matched','grade':'小花','base_price':4500,
          'value_low':4200,'value_high':4200,'deductions':[{'name':'弹电','min':300,'max':300}]}}
    _,plain=NotificationService._format_message(item,'','',[])
    markdown=NotificationService._format_webhook_content(item,'','',[])
    for body in (plain,markdown):
        assert '回收估值：¥4200' in body and '本次扣费：¥300' in body
        assert '需议价 ¥450' in body


PENDING_WITH_RANGED_FEES = {
    **REFERENCE_ITEM, 'price': 2200,
    'smart_assessment': {
        'status': 'pending', 'grade': '靓机', 'reference_grade': '靓机',
        'base_price': 2550, 'value_low': None, 'value_high': 2150,
        'deductions': [
            {'name': '电池低于90%', 'min': 50, 'max': 50},
            {'name': '靓机小花弹电/弹屏', 'min': 350, 'max': 700},
        ],
        'uncertainties': ['部件类型尚未确定'],
    },
}


def test_ranged_fee_reference_and_each_deduction_render_as_individual_lines():
    import copy
    item=copy.deepcopy(PENDING_WITH_RANGED_FEES)
    original=copy.deepcopy(item)
    expected=(
        '回收参考：≤ ¥2150（靓机参考，待确认）\n\n'
        '基础回收价：¥2550\n'
        '扣费明细：\n'
        '· 电池低于90%：−¥50\n'
        '· 靓机小花弹电/弹屏：−¥350～700\n'
        '本次扣费：¥400（按区间最低值）\n\n'
        '地区：厦门'
    )
    for body in (NotificationService._format_message(item,'','',[])[1],
                 NotificationService._format_webhook_content(item,'','',[])):
        assert expected in body.replace('  \n','\n')
        assert '部件类型尚未确定' not in body
        assert '按最低扣费 ¥400参考' not in body and '；靓机小花' not in body
    assert item==original


async def test_ranged_fee_line_breaks_survive_plain_and_markdown_transports(monkeypatch):
    from urllib.parse import unquote
    captured={}
    def handler(request):
        host=request.url.host
        if host=='bark.example.test':captured['bark']=unquote(request.url.raw_path.decode())
        elif host=='push.example.test':captured['webhook']=json.loads(request.content)['content']
        else:pytest.fail('Unexpected destination')
        return httpx.Response(200,json={'ok':True})
    real=httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx,'AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(handler),**kwargs))
    service=NotificationService(replace(settings,ntfy_topic_url='',
        bark_url='https://bark.example.test/key',webhook_url='https://push.example.test/hook',
        webhook_headers='',webhook_body='{"title":"{{title}}","content":"{{content}}"}',
        webhook_method='POST',webhook_content_type='JSON',webhook_query_parameters=''))
    result=await service.send(PENDING_WITH_RANGED_FEES,'')
    assert all(channel['success'] for channel in result.values()),result
    assert set(captured)=={'bark','webhook'}
    for body in captured.values():
        lines=[line.rstrip() for line in body.splitlines()]
        assert '· 电池低于90%：−¥50' in lines
        assert '· 靓机小花弹电/弹屏：−¥350～700' in lines
        assert '本次扣费：¥400（按区间最低值）' in lines
        assert not any('\\n' in line for line in lines)
