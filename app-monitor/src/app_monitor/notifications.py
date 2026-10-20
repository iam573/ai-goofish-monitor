from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx

from .config import Settings
from .matching import display_storage


class NotificationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured_channels(self) -> list[str]:
        channels = []
        if self.settings.ntfy_topic_url:
            channels.append("ntfy")
        if self.settings.bark_url:
            channels.append("bark")
        if self.settings.webhook_url:
            channels.append("webhook")
        return channels

    async def send(self, item: dict[str, Any], reason: str) -> dict[str, dict[str, Any]]:
        item_url = str(item.get("item_url") or "").strip()
        if not re.search(r"(?:[?&])id=\d{8,}(?:&|$)", item_url):
            item_url = ""
        image_urls = self._image_urls(item)
        mobile_url = self._mobile_link(item_url)
        notification_item = {**item, "item_url": item_url, "image_urls": image_urls}
        notification_title, body = self._format_message(
            notification_item, reason, mobile_url, image_urls
        )
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            results: dict[str, dict[str, Any]] = {}
            if self.settings.ntfy_topic_url:
                results["ntfy"] = await self._call(
                    "ntfy",
                    client.post(
                        self.settings.ntfy_topic_url,
                        content=body.encode("utf-8"),
                        headers={
                            "Title": notification_title,
                            **({"Click": mobile_url} if mobile_url else {}),
                            **({"Attach": image_urls[0]} if image_urls else {}),
                            "Tags": "moneybag,iphone",
                        },
                    ),
                )
            if self.settings.bark_url:
                base = self.settings.bark_url.rstrip("/")
                results["bark"] = await self._call(
                    "bark",
                    client.get(
                        f"{base}/{quote(notification_title, safe='')}/{quote(body, safe='')}",
                        params={**({"url": mobile_url} if mobile_url else {}), **({"icon": image_urls[0]} if image_urls else {})},
                    ),
                )
            if self.settings.webhook_url:
                try:
                    response = await self._send_webhook(client, notification_item, reason, notification_title, body)
                    results["webhook"] = await self._call("webhook", response)
                except Exception as exc:
                    results["webhook"] = {
                        "success": False,
                        "error": str(exc),
                        "channel": "webhook",
                    }
        return results

    @staticmethod
    def _format_message(
        item: dict[str, Any], reason: str, mobile_url: str, image_urls: list[str]
    ) -> tuple[str, str]:
        product_title = str(item["title"]).strip()
        model = str(item.get("model") or "").strip()
        storage = str(item.get("storage") or "").strip()
        category = f"iPhone {model} {display_storage(storage)}" if model and storage else product_title
        price = float(item["price"])
        lines = [product_title, "", f"挂牌价：¥{price:g}"]
        quote = item.get("quote_price")
        if quote is not None:
            quote_price = float(quote)
            grade = str(item.get("quote_grade") or "").strip()
            lines.append(f"{grade}回收报价：¥{quote_price:g}")
            difference = price - quote_price
            if difference > 0:
                lines.append(f"挂牌价高于回收报价：¥{difference:g}")
            elif difference < 0:
                lines.append(f"挂牌价低于回收报价：¥{-difference:g}")
            else:
                lines.append("挂牌价与回收报价持平")
        elif reason:
            lines.append(f"原因：{reason}")
        region = str(item.get("display_region") or item.get("region") or "").strip()
        if region:
            lines.append(f"地区：{region}")
        item_url = str(item.get("item_url") or "")
        if mobile_url or item_url:
            lines.extend(("", f"打开商品：{mobile_url or item_url}"))
        if item_url and mobile_url and item_url != mobile_url:
            lines.append(f"网页链接：{item_url}")
        if image_urls:
            lines.extend(("", f"![商品图片]({image_urls[0]})"))
        return category, "\n".join(lines)

    async def _send_webhook(
        self,
        client: httpx.AsyncClient,
        item: dict[str, Any],
        reason: str,
        title: str,
        content: str,
    ) -> Any:
        headers = self._parse_json(
            self.settings.webhook_headers, "WEBHOOK_HEADERS", expect_dict=True
        ) or {}
        params = self._parse_json(
            self.settings.webhook_query_parameters,
            "WEBHOOK_QUERY_PARAMETERS",
            expect_dict=True,
        ) or {}
        replacements = {
            "title": title,
            "notification_title": title,
            "item_title": str(item.get("title") or ""),
            "keyword": str(item.get("keyword") or ""),
            "content": content,
            "price": f"{float(item['price']):g}",
            "reason": reason,
            "desktop_link": str(item.get("item_url") or ""),
            "mobile_link": self._mobile_link(str(item.get("item_url") or "")),
            "image_url": self._image_urls(item)[0] if self._image_urls(item) else "",
            "image_urls": "\n".join(self._image_urls(item)),
            "image_gallery": "\n".join(f"![商品图片]({url})" for url in self._image_urls(item)[:6]),
            "image_carousel": "\n".join(f"![商品图片]({url})" for url in self._image_urls(item)[:6]),
            "xianyu_card": f"**{title}**\n\n{content}",
        }
        rendered_params = self._render_template(params, replacements)
        parsed_url = list(urlparse(self.settings.webhook_url))
        query = dict(parse_qsl(parsed_url[4]))
        query.update({str(key): str(value) for key, value in rendered_params.items()})
        parsed_url[4] = urlencode(query)
        final_url = urlunparse(parsed_url)

        if self.settings.webhook_method == "GET":
            return client.get(final_url, headers=headers)
        if self.settings.webhook_method != "POST":
            raise ValueError("WEBHOOK_METHOD 仅支持 GET 或 POST")

        if self.settings.webhook_body:
            template = self._parse_json(self.settings.webhook_body, "WEBHOOK_BODY")
            payload = self._render_template(template, replacements)
        else:
            payload = {"title": title, "body": content, "item": item, "reason": reason}

        if self.settings.webhook_content_type == "JSON":
            return client.post(final_url, headers=headers, json=payload)
        if self.settings.webhook_content_type == "FORM":
            if not isinstance(payload, dict):
                raise ValueError("WEBHOOK_BODY 在 FORM 模式下必须是 JSON 对象")
            return client.post(final_url, headers=headers, data=payload)
        raise ValueError("WEBHOOK_CONTENT_TYPE 仅支持 JSON 或 FORM")

    @staticmethod
    def _parse_json(raw: str, field: str, expect_dict: bool = False) -> Any:
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} 不是合法 JSON：{exc.msg}") from exc
        if expect_dict and not isinstance(value, dict):
            raise ValueError(f"{field} 必须是 JSON 对象")
        return value

    @staticmethod
    def _image_urls(item: dict[str, Any]) -> list[str]:
        values = item.get("image_urls") if isinstance(item.get("image_urls"), list) else []
        if item.get("image_url"):
            values = [item["image_url"], *values]
        return list(dict.fromkeys(
            value.strip() for value in values
            if isinstance(value, str) and value.strip().startswith(("https://", "http://"))
        ))

    @staticmethod
    def _mobile_link(item_url: str) -> str:
        match = re.search(r"(?:[?&])id=(\d+)", item_url)
        if not match or len(match.group(1)) < 8:
            return item_url
        item_id = match.group(1)
        item_data = quote(json.dumps({"id": int(item_id)}, separators=(",", ":")))
        return (
            "https://pages.goofish.com/sharexy?loadingVisible=false&bft=item&bfs=idlepc.item"
            f"&spm=a21ybx.item.0.0&bfp={item_data}"
        )

    @classmethod
    def _render_template(cls, value: Any, replacements: dict[str, str]) -> Any:
        if isinstance(value, str):
            rendered = value
            for key, replacement in replacements.items():
                rendered = rendered.replace(f"${{{key}}}", replacement)
                rendered = rendered.replace(f"{{{{{key}}}}}", replacement)
            return rendered
        if isinstance(value, list):
            return [cls._render_template(item, replacements) for item in value]
        if isinstance(value, dict):
            return {
                key: cls._render_template(item, replacements)
                for key, item in value.items()
            }
        return value

    async def test(self) -> dict[str, dict[str, Any]]:
        return await self.send(
            {
                "title": "[测试] App Monitor 推送",
                "price": 0,
                "item_url": "https://www.goofish.com/",
            },
            "推送渠道配置有效。",
        )

    @staticmethod
    async def _call(channel: str, awaitable: Any) -> dict[str, Any]:
        try:
            response = await awaitable
            response.raise_for_status()
            return {"success": True, "status_code": response.status_code}
        except Exception as exc:
            return {"success": False, "error": str(exc), "channel": channel}


def notification_result_text(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)
