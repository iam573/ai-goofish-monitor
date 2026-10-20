from dataclasses import replace

import httpx

from app_monitor.config import settings
from app_monitor.notifications import NotificationService


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
    assert request.content.decode() == '{"title":"iPhone 15 Pro","content":"价格：¥3000"}'
