"""
通用 Webhook 通知客户端
"""
import asyncio
import json
from html import escape
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from .base import NotificationClient, NotificationMessage


class WebhookClient(NotificationClient):
    """通用 Webhook 通知客户端"""

    channel_key = "webhook"
    display_name = "Webhook"

    def __init__(
        self,
        webhook_url: str | None = None,
        webhook_method: str = "POST",
        webhook_headers: str | None = None,
        webhook_content_type: str = "JSON",
        webhook_query_parameters: str | None = None,
        webhook_body: str | None = None,
        pcurl_to_mobile: bool = True,
    ):
        super().__init__(enabled=bool(webhook_url), pcurl_to_mobile=pcurl_to_mobile)
        self.webhook_url = webhook_url
        self.webhook_method = (webhook_method or "POST").upper()
        self.webhook_headers = webhook_headers
        self.webhook_content_type = (webhook_content_type or "JSON").upper()
        self.webhook_query_parameters = webhook_query_parameters
        self.webhook_body = webhook_body

    async def send(self, product_data: Dict, reason: str) -> None:
        if not self.is_enabled():
            raise RuntimeError("Webhook 未启用")

        message = self._build_message(product_data, reason)
        headers = self._parse_json(self.webhook_headers, "WEBHOOK_HEADERS", expect_dict=True) or {}
        final_url = self._build_url(message)
        loop = asyncio.get_running_loop()

        if self.webhook_method == "GET":
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(final_url, headers=headers, timeout=15),
            )
            response.raise_for_status()
            return

        json_payload, form_payload = self._build_body(message, headers)
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                final_url,
                headers=headers,
                json=json_payload,
                data=form_payload,
                timeout=15,
            ),
        )
        response.raise_for_status()

    def _build_url(self, message: NotificationMessage) -> str:
        params = self._parse_json(
            self.webhook_query_parameters,
            "WEBHOOK_QUERY_PARAMETERS",
            expect_dict=True,
        ) or {}
        rendered = self._render_template(params, message)
        parsed_url = list(urlparse(self.webhook_url))
        query = dict(parse_qsl(parsed_url[4]))
        query.update(rendered)
        parsed_url[4] = urlencode(query)
        return urlunparse(parsed_url)

    def _build_body(
        self,
        message: NotificationMessage,
        headers: Dict[str, str],
    ) -> tuple[Any | None, Any | None]:
        if not self.webhook_body:
            return None, None

        body_template = self._parse_json(self.webhook_body, "WEBHOOK_BODY")
        rendered_body = self._render_template(body_template, message)

        if self.webhook_content_type == "JSON":
            if "Content-Type" not in headers and "content-type" not in headers:
                headers["Content-Type"] = "application/json; charset=utf-8"
            return rendered_body, None

        if self.webhook_content_type == "FORM":
            if not isinstance(rendered_body, dict):
                raise ValueError("WEBHOOK_BODY 在 FORM 模式下必须是 JSON 对象")
            if "Content-Type" not in headers and "content-type" not in headers:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            return None, rendered_body

        raise ValueError(f"不支持的 WEBHOOK_CONTENT_TYPE: {self.webhook_content_type}")

    def _parse_json(
        self,
        raw_value: str | None,
        field_name: str,
        expect_dict: bool = False,
    ) -> Any | None:
        if not raw_value:
            return None
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} 不是合法 JSON: {exc.msg}") from exc
        if expect_dict and not isinstance(parsed, dict):
            raise ValueError(f"{field_name} 必须是 JSON 对象")
        return parsed

    def _render_template(self, value: Any, message: NotificationMessage) -> Any:
        if isinstance(value, str):
            return self._replace_placeholders(value, message)
        if isinstance(value, list):
            return [self._render_template(item, message) for item in value]
        if isinstance(value, dict):
            return {
                key: self._render_template(item, message)
                for key, item in value.items()
            }
        return value

    def _replace_placeholders(self, value: str, message: NotificationMessage) -> str:
        image_gallery = self._build_image_gallery(message)
        image_carousel = self._build_image_carousel(message)
        replacements = {
            "title": message.title,
            "notification_title": message.notification_title,
            "keyword": message.keyword,
            "content": message.content,
            "price": message.price,
            "reason": message.reason,
            "desktop_link": message.desktop_link,
            "mobile_link": message.mobile_link or message.desktop_link,
            "image_url": message.image_url or "",
            "image_urls": "\n".join(message.image_urls),
            "image_gallery": image_gallery,
            "image_carousel": image_carousel,
            "xianyu_card": self._build_xianyu_card(message, image_carousel),
        }
        rendered = value
        for key, replacement in replacements.items():
            rendered = rendered.replace(f"${{{key}}}", replacement)
            rendered = rendered.replace(f"{{{{{key}}}}}", replacement)
        return rendered

    def _build_image_gallery(self, message: NotificationMessage) -> str:
        if not message.image_urls:
            return ""

        images = []
        multiple_images = len(message.image_urls) > 1
        for index, image_url in enumerate(message.image_urls[:6], start=1):
            safe_url = self._html_attr(image_url)
            if multiple_images:
                style = (
                    "width:31%;max-width:120px;height:auto;border-radius:8px;"
                    "margin:0 5px 6px 0;vertical-align:top;background:#f6f6f6;"
                )
            else:
                style = (
                    "width:100%;max-width:420px;height:auto;border-radius:12px;"
                    "display:block;background:#f6f6f6;"
                )
            images.append(
                f'<img src="{safe_url}" alt="商品图片{index}" style="{style}" />'
            )

        return f'<div style="margin:12px 0 4px 0;">{"".join(images)}</div>'

    def _build_image_carousel(self, message: NotificationMessage) -> str:
        if not message.image_urls:
            return ""

        slides = []
        for index, image_url in enumerate(message.image_urls[:6], start=1):
            safe_url = self._html_attr(image_url)
            slides.append(
                '<div style="display:inline-block;width:100%;vertical-align:top;'
                'scroll-snap-align:start;">'
                f'<img src="{safe_url}" alt="商品图片{index}" '
                'style="display:block;width:100%;max-width:420px;height:auto;'
                'border-radius:14px;background:#f6f6f6;" />'
                "</div>"
            )

        hint = (
            '<div style="font-size:12px;color:#9ca3af;margin-top:6px;">'
            f"左右滑动查看 {len(message.image_urls[:6])} 张图片</div>"
            if len(message.image_urls) > 1
            else ""
        )
        return (
            '<div style="margin:12px 0 4px 0;">'
            '<div style="overflow-x:auto;white-space:nowrap;scroll-snap-type:x mandatory;'
            '-webkit-overflow-scrolling:touch;border-radius:14px;">'
            f'{"".join(slides)}'
            "</div>"
            f"{hint}"
            "</div>"
        )

    def _build_xianyu_card(
        self,
        message: NotificationMessage,
        image_block: str,
    ) -> str:
        title = self._html_text(message.title)
        price = self._html_text(self._format_price(message.price))
        reason = self._html_text(message.reason).replace("\n", "<br/>")
        keyword = self._html_text(message.keyword)
        mobile_link = self._html_attr(message.mobile_link or message.desktop_link)
        desktop_link = self._html_attr(message.desktop_link)
        keyword_block = (
            '<div style="font-size:13px;color:#6b7280;margin-top:10px;">'
            f"🔎 关键词：{keyword}</div>"
            if keyword
            else ""
        )

        return (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,'
            'sans-serif;color:#1f2933;line-height:1.55;background:#ffffff;">'
            '<div style="border:1px solid #eeeeee;border-radius:16px;padding:14px;'
            'background:#ffffff;">'
            f'<div style="font-size:26px;font-weight:800;color:#ff5000;'
            f'margin-bottom:8px;">{price}</div>'
            '<div style="font-size:13px;color:#9ca3af;background:#f6f6f6;'
            'border-radius:10px;padding:8px 10px;margin-bottom:12px;">'
            "描述不符包邮退　满足条件时，买家可退货且运费由卖家承担"
            "</div>"
            '<div style="font-size:17px;font-weight:700;color:#1f2933;'
            'background:#d9f5df;padding:4px 6px;line-height:1.55;'
            f'margin-bottom:12px;">{title}</div>'
            f"{image_block}"
            '<div style="margin-top:12px;padding:10px 12px;background:#fff8e5;'
            'border-radius:12px;font-size:14px;color:#374151;">'
            f'<strong>✨ 推荐原因</strong><br/>{reason}</div>'
            f"{keyword_block}"
            '<div style="margin-top:14px;">'
            f'<a href="{mobile_link}" style="display:inline-block;background:#ffe100;'
            'color:#222222;text-decoration:none;border-radius:22px;padding:10px 18px;'
            'margin:0 6px 8px 0;font-weight:700;">📱 手机端打开</a>'
            f'<a href="{desktop_link}" style="display:inline-block;background:#333333;'
            'color:#ffffff;text-decoration:none;border-radius:22px;padding:10px 18px;'
            'margin:0 0 8px 0;font-weight:700;">💻 电脑端打开</a>'
            "</div>"
            "</div>"
            "</div>"
        )

    def _format_price(self, price: str) -> str:
        normalized = price.strip()
        if not normalized or normalized.startswith(("¥", "￥")):
            return normalized
        return f"¥ {normalized}"

    def _html_text(self, value: str) -> str:
        return escape(value or "", quote=False)

    def _html_attr(self, value: str) -> str:
        return escape(value or "", quote=True)
