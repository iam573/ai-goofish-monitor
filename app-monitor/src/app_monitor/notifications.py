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
        webhook_content = self._format_webhook_content(
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
                    response = await self._send_webhook(
                        client, notification_item, reason, notification_title, webhook_content
                    )
                    results["webhook"] = await self._call("webhook", response)
                except Exception as exc:
                    results["webhook"] = {
                        "success": False,
                        "error": str(exc),
                        "channel": "webhook",
                    }
        return results

    # ------------------------------------------------------------------ 消息排版

    @staticmethod
    def _money(value: Any) -> str:
        number = float(value)
        return f"{round(number):,}" if abs(number - round(number)) < 0.01 else f"{number:,.2f}"

    @staticmethod
    def _category(item: dict[str, Any]) -> str:
        model = str(item.get("model") or "").strip()
        storage = str(item.get("storage") or "").strip()
        return f"iPhone {model} {display_storage(storage)}" if model and storage else str(item["title"]).strip()

    @staticmethod
    def _difference(item: dict[str, Any]) -> tuple[str, str]:
        """(完整说法, 简短说法)，例如 ("比回收价高 ¥100", "+¥100")。"""
        quote = item.get("quote_price")
        if quote is None:
            return "", ""
        money = NotificationService._money
        difference = float(item["price"]) - float(quote)
        if difference > 0:
            return f"比回收价高 ¥{money(difference)}", f"+¥{money(difference)}"
        if difference < 0:
            return f"比回收价低 ¥{money(-difference)}", f"−¥{money(-difference)}"
        return "与回收价持平", "±0"

    @staticmethod
    def _title(item: dict[str, Any]) -> str:
        """通知标题：机型容量 + 挂牌价 + 与回收价的差，在通知列表里一眼能看出值不值。"""
        if not (item.get("model") and item.get("storage")):
            return str(item["title"]).strip()
        title = f"{NotificationService._category(item)} ¥{NotificationService._money(item['price'])}"
        difference, _ = NotificationService._difference(item)
        return f"{title} · {difference}" if difference else title

    @staticmethod
    def _quote_label(item: dict[str, Any]) -> str:
        reference = item.get("quote_reference") or {}
        source = str(reference.get("category_name") or "").replace("苹果", "")
        grade = str(item.get("quote_grade") or reference.get("grade") or "").strip()
        return "·".join(value for value in (source, grade) if value)

    @staticmethod
    def _deduction_rows(item: dict[str, Any]) -> list[tuple[str, str, str]]:
        """扣费项：(名称, 扣多少, 扣后回收价)。扣后回收价 = 参考成色回收价 − 该项扣费。"""
        reference = item.get("quote_reference") or {}
        money = NotificationService._money
        base = item.get("quote_price")
        rows = []
        for value in reference.get("deductions") or []:
            low = float(value["amount_min"])
            high = float(value.get("amount_max") or low)
            if value.get("open_ended"):
                amount, after = f"−{money(low)} 起", (f"≤ {money(float(base) - low)}" if base is not None else "")
            elif high > low:
                # 全角「～」：避免 Markdown 把成对的 ~ 当成删除线
                amount = f"−{money(low)}～{money(high)}"
                after = f"{money(float(base) - high)}～{money(float(base) - low)}" if base is not None else ""
            else:
                amount, after = f"−{money(low)}", (money(float(base) - low) if base is not None else "")
            rows.append((str(value["name"]), amount, after))
        return rows

    @staticmethod
    def _format_message(
        item: dict[str, Any], reason: str, mobile_url: str, image_urls: list[str]
    ) -> tuple[str, str]:
        """纯文本（ntfy / Bark）：分块、对齐，手机通知里也好读。"""
        money = NotificationService._money
        lines = [str(item["title"]).strip(), ""]
        price = f"¥{money(item['price'])}"
        if item.get("quote_price") is not None:
            difference, _ = NotificationService._difference(item)
            label = NotificationService._quote_label(item)
            lines.append(f"💰 挂牌 {price} ｜ 回收 ¥{money(item['quote_price'])}{f'（{label}）' if label else ''}")
            lines.append(f"📊 {difference}")
        else:
            lines.append(f"💰 挂牌 {price}")
            if reason:
                lines.append(f"ℹ️ {reason}")
        region = str(item.get("display_region") or item.get("region") or "").strip()
        if region:
            lines.append(f"📍 {region}")

        item_url = str(item.get("item_url") or "")
        if mobile_url or item_url:
            lines.extend(("", f"🔗 打开商品：{mobile_url or item_url}"))
        if item_url and mobile_url and item_url != mobile_url:
            lines.append(f"💻 网页链接：{item_url}")
        if image_urls:
            lines.extend(("", f"![商品图片]({image_urls[0]})"))

        # 参考价（各成色、扣费项、说明）放在最后
        reference = item.get("quote_reference") or {}
        grades = reference.get("grades") or []
        if grades:
            date = str(reference.get("quote_date") or "")[5:]
            lines.extend(("", f"📋 各成色回收价（{reference.get('category_name', '')}{f' {date}' if date else ''}）"))
            lines.append(" ｜ ".join(
                f"{value['grade']} {money(value['price'])}{' ✓' if value['grade'] == reference.get('grade') else ''}"
                for value in grades
            ))
            if reference.get("other_price") is not None:
                other = str(reference.get("other_category_name") or "").replace("苹果", "")
                lines.append(f"{other}·{reference.get('grade', '')} {money(reference['other_price'])}")
        deductions = NotificationService._deduction_rows(item)
        if deductions:
            lines.extend(("", "🔧 扣费项（→ 扣后回收价）"))
            lines.extend(f"· {name}  {amount}{f' → {after}' if after else ''}" for name, amount, after in deductions)
        elif grades:
            lines.extend(("", "🔧 扣费项：报价单未识别到"))
        notes = reference.get("notes") or []
        if notes:
            lines.append(f"💬 {'；'.join(notes)}")
        return NotificationService._title(item), "\n".join(lines)

    @staticmethod
    def _format_webhook_content(
        item: dict[str, Any], reason: str, mobile_url: str, image_urls: list[str]
    ) -> str:
        """Markdown（PushPlus 等）：标题行 + 价格表 + 链接 + 图片，最后是参考价（成色表、扣费表、说明）。"""
        money = NotificationService._money

        def escape(value: Any) -> str:
            return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", str(value).strip())

        def cell(value: Any) -> str:
            return str(value).replace("|", "/")

        item_url = str(item.get("item_url") or "")
        main_url = mobile_url or item_url
        product_title = escape(item["title"])
        blocks = [f"### [{product_title}]({main_url})" if main_url else f"### {product_title}"]

        region = str(item.get("display_region") or item.get("region") or "").strip()
        if item.get("quote_price") is not None:
            _, short = NotificationService._difference(item)
            label = NotificationService._quote_label(item) or "参考"
            blocks.append(
                f"| 挂牌价 | 回收价（{cell(label)}） | 差价 |\n| :-: | :-: | :-: |\n"
                f"| **¥{money(item['price'])}** | ¥{money(item['quote_price'])} | **{short}** |"
            )
            if region:
                blocks.append(f"📍 {cell(region)}")
        else:
            lines = [f"**挂牌价：¥{money(item['price'])}**"]
            if reason:
                lines.append(f"原因：{reason}")
            if region:
                lines.append(f"地区：{region}")
            blocks.append("  \n".join(lines))

        links = []
        if main_url:
            links.append(f"[📱 手机打开]({main_url})" if mobile_url and mobile_url != item_url else f"[🔗 打开商品]({main_url})")
        if item_url and item_url != main_url:
            links.append(f"[💻 网页打开]({item_url})")
        if links:
            blocks.append(" ｜ ".join(links))
        if image_urls:
            blocks.append(f"![商品主图]({image_urls[0]})")

        # 参考价（各成色、扣费项、说明）放在最后，用分隔线和上面的商品信息隔开
        reference_start = len(blocks)
        reference = item.get("quote_reference") or {}
        grades = reference.get("grades") or []
        if grades:
            date = str(reference.get("quote_date") or "")[5:]
            blocks.append(f"**📋 各成色回收价**（{reference.get('category_name', '')}{f' {date}' if date else ''}）")
            headers = [value["grade"] for value in grades]
            values = [
                f"**{money(value['price'])}**" if value["grade"] == reference.get("grade") else money(value["price"])
                for value in grades
            ]
            if reference.get("other_price") is not None:
                headers.append(f"{str(reference.get('other_category_name') or '').replace('苹果', '')}·{reference.get('grade', '')}")
                values.append(money(reference["other_price"]))
            blocks.append(
                f"| {' | '.join(map(cell, headers))} |\n| {' | '.join(':-:' for _ in headers)} |\n| {' | '.join(values)} |"
            )
        deductions = NotificationService._deduction_rows(item)
        if deductions:
            rows = "\n".join(f"| {cell(name)} | {amount} | {after} |" for name, amount, after in deductions)
            blocks.append(f"**🔧 扣费项**\n\n| 项目 | 扣费 | 扣后回收价 |\n| :-- | --: | --: |\n{rows}")
        elif grades:
            blocks.append("**🔧 扣费项**：报价单未识别到")
        notes = reference.get("notes") or []
        if notes:
            blocks.append(f"> 💬 {'；'.join(cell(note) for note in notes)}")
        if len(blocks) > reference_start:
            blocks.insert(reference_start, "---")
        return "\n\n".join(blocks)

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
                # 示例参考价，用来预览真实推送里参考报价和扣费项的样子
                "quote_reference": {
                    "category_name": "苹果无保（示例）", "quote_date": "", "grade": "小花",
                    "grades": [{"grade": "靓机", "price": 5100}, {"grade": "小花", "price": 4900},
                               {"grade": "大花", "price": 4600}, {"grade": "外爆", "price": 4300},
                               {"grade": "内爆可测", "price": 3900}],
                    "other_category_name": "苹果有保", "other_price": 5200,
                    "deductions": [
                        {"name": "电池低于85%", "amount_min": 100, "amount_max": 100, "open_ended": False},
                        {"name": "电池低于80%", "amount_min": 200, "amount_max": 300, "open_ended": False},
                        {"name": "换屏", "amount_min": 300, "amount_max": 300, "open_ended": False},
                        {"name": "有锁", "amount_min": 1500, "amount_max": 1500, "open_ended": True},
                    ],
                    "notes": ["后盖破当大花收"],
                },
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
