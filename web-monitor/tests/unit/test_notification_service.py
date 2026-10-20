import asyncio

from src.infrastructure.external.notification_clients.base import NotificationClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.services.notification_service import NotificationService


class _OkClient(NotificationClient):
    channel_key = "ok"
    display_name = "OK"

    async def send(self, product_data, reason):
        return None


class _FailClient(NotificationClient):
    channel_key = "fail"
    display_name = "FAIL"

    async def send(self, product_data, reason):
        raise RuntimeError("boom")


def test_notification_service_collects_success_and_failure_results():
    service = NotificationService([_OkClient(enabled=True), _FailClient(enabled=True)])

    results = asyncio.run(
        service.send_notification({"商品标题": "Sony A7M4"}, "价格合适")
    )

    assert results["ok"]["success"] is True
    assert results["ok"]["message"] == "发送成功"
    assert results["fail"]["success"] is False
    assert results["fail"]["message"] == "boom"


def test_webhook_client_renders_json_templates(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, headers=None, json=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["data"] = data
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = WebhookClient(
        webhook_url="https://hooks.example.com/notify",
        webhook_method="POST",
        webhook_headers='{"Authorization":"Bearer token"}',
        webhook_content_type="JSON",
        webhook_query_parameters='{"task":"{{title}}","keyword":"{{keyword}}"}',
        webhook_body='{"message":"{{content}}","keyword":"{{keyword}}","short_title":"{{notification_title}}","link":"{{desktop_link}}","image":"{{image_url}}","images":"{{image_urls}}","gallery":"{{image_gallery}}","carousel":"{{image_carousel}}","card":"{{xianyu_card}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "搜索关键字": "sony a7m4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item/123",
                "商品主图链接": "https://img.example.com/item.jpg",
                "商品图片列表": [
                    "https://img.example.com/item.jpg",
                    "https://img.example.com/item-2.jpg",
                    "https://img.example.com/item-3.jpg",
                ],
            },
            "价格合适",
        )
    )

    assert "task=Sony+A7M4" in captured["url"]
    assert "keyword=sony+a7m4" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["message"].startswith("价格: 9999")
    assert captured["json"]["keyword"] == "sony a7m4"
    assert captured["json"]["short_title"] == "Sony A7M4"
    assert captured["json"]["link"] == "https://www.goofish.com/item/123"
    assert captured["json"]["image"] == "https://img.example.com/item.jpg"
    assert captured["json"]["images"] == "\n".join([
        "https://img.example.com/item.jpg",
        "https://img.example.com/item-2.jpg",
        "https://img.example.com/item-3.jpg",
    ])
    assert captured["json"]["gallery"].count("<img") == 3
    assert "https://img.example.com/item-2.jpg" in captured["json"]["gallery"]
    assert captured["json"]["carousel"].count("<img") == 3
    assert "overflow-x:auto" in captured["json"]["carousel"]
    assert "左右滑动查看 3 张图片" in captured["json"]["carousel"]
    assert "描述不符包邮退" in captured["json"]["card"]
    assert "¥ 9999" in captured["json"]["card"]
    assert "overflow-x:auto" in captured["json"]["card"]
    assert "📱 手机端打开" in captured["json"]["card"]
    assert captured["data"] is None


def test_webhook_title_placeholder_uses_full_item_title(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, headers=None, json=None, data=None, timeout=None):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    long_title = "Sony A7M4 全画幅相机 成色很好 快门很低 带包装和配件"
    client = WebhookClient(
        webhook_url="https://hooks.example.com/notify",
        webhook_body='{"title":"{{title}}","short":"{{notification_title}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": long_title,
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item/123",
            },
            "价格合适",
        )
    )

    assert captured["json"]["title"] == long_title
    assert captured["json"]["short"] != long_title
    assert captured["json"]["short"].endswith("...")
