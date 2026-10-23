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
    # 前面是商品本身：标题、挂牌价 / 回收价 / 差价、地区、打开链接；参考价（各成色、扣费项、说明）放最后。

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
    def _reference_subject(item: dict[str, Any]) -> str:
        """参考价对应的机型容量，例如「iPhone 15 Pro 128G 」；没有识别出机型时为空。"""
        if item.get("model") and item.get("storage"):
            return f"{NotificationService._category(item)} "
        return ""

    @staticmethod
    def _detail_lines(item: dict[str, Any], reason: str, markdown: bool = False) -> list[str]:
        if item.get("smart_assessment"):
            lines = NotificationService._smart_lines(item, markdown)
        else:
            price = float(item["price"])
            lines = [f"挂牌价：¥{price:g}"]
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
        smart = bool(item.get("smart_assessment"))
        if region and not smart:
            lines.append(f"地区：{region}")
        seller = str(item.get("seller_name") or "").strip()
        if seller or "is_fish_shop" in item:
            if markdown:
                seller = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", seller)
            suffix = "（鱼小铺）" if item.get("is_fish_shop") is True else ""
            seller_line = f"卖家：{seller or '未识别'}{suffix}"
            lines.append(f"{region} · {seller_line}" if smart and region else seller_line)
        elif smart and region:
            lines.append(f"地区：{region}")
        return lines

    @staticmethod
    def _smart_lines(item, markdown=False):
        value = item["smart_assessment"]
        escape = lambda text: re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", str(text)) if markdown else str(text)
        grade = value.get("grade")
        lines = [f"挂牌价：¥{float(item['price']):g} · {escape(grade)}（按描述）" if grade
                 else f"挂牌价：¥{float(item['price']):g} · 成色待确认"]
        low, high = value.get("value_low"), value.get("value_high")
        if low is not None and grade and value.get("status") != "pending":
            price = f"¥{low:g}" if high is None or low == high else f"¥{low:g}～{high:g}"
            difference = float(item['price']) - low
            comparison = (f"需议价 ¥{difference:g}" if difference > 0 else
                          f"低于{'保守' if high is not None and low != high else ''}估值 ¥{-difference:g}" if difference < 0 else "与估值持平")
            lines.append(f"回收估值：{price} · {comparison}")
        elif high is not None:
            lines.append(f"回收参考：≤ ¥{high:g}（候选上限，待确认）")
        else:
            lines.append("回收估值：待确认")
        if value.get("deductions"):
            deductions = []
            for row in value["deductions"]:
                amount = f"¥{row['min']:g}" if row['min'] == row['max'] else f"¥{row['min']:g}～{row['max']:g}"
                deductions.append(f"{escape(row['name'])} −{amount}{'起' if row.get('open_ended') else ''}")
            base = f"基础 ¥{value['base_price']:g} · " if value.get("base_price") is not None else ""
            lines.append(f"扣费：{base}{'；'.join(deductions)}")
        uncertainties = []
        for reason in value.get("uncertainties", []):
            if not grade and reason in {"成色无法确定", "未取得确定成色的报价，使用候选上限"}:
                continue  # Already explicit in the heading and candidate ceiling.
            if reason == "未取得确定成色的报价，使用候选上限":
                reason = "该成色暂无对应报价"
            if reason not in uncertainties:
                uncertainties.append(reason)
        if uncertainties:
            lines.append("待确认：" + escape("；".join(uncertainties)))
        return lines

    @staticmethod
    def _deduction_rows(item: dict[str, Any]) -> list[tuple[str, str, str]]:
        """扣费项：(名称, 扣多少, 扣后回收价)。扣后回收价 = 参考成色回收价 − 该项扣费。"""
        reference = item.get("quote_reference") or {}
        money = NotificationService._money
        base = (item.get("smart_assessment") or {}).get("base_price", item.get("quote_price"))
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
        """纯文本（ntfy / Bark）。"""
        money = NotificationService._money
        lines = [str(item["title"]).strip(), "", *NotificationService._detail_lines(item, reason)]
        item_url = str(item.get("item_url") or "")
        if mobile_url or item_url:
            lines.extend(("", f"打开商品：{mobile_url or item_url}"))
        if item_url and mobile_url and item_url != mobile_url:
            lines.append(f"网页链接：{item_url}")
        if image_urls:
            lines.extend(("", f"![商品图片]({image_urls[0]})"))

        # 参考价放在最后；标题写明是哪个机型容量的参考价
        reference = item.get("quote_reference") or {}
        grades = reference.get("grades") or []
        selected_grade = item["smart_assessment"].get("grade") if item.get("smart_assessment") else reference.get("grade")
        subject = NotificationService._reference_subject(item)
        if grades:
            date = str(reference.get("quote_date") or "")[5:]
            lines.extend(("", f"📋 {subject}各成色回收价（{reference.get('category_name', '')}{f' {date}' if date else ''}）"))
            lines.append(" ｜ ".join(
                f"{value['grade']} {money(value['price'])}{' ✓' if value['grade'] == selected_grade else ''}"
                for value in grades
            ))
            if reference.get("other_price") is not None:
                other = str(reference.get("other_category_name") or "").replace("苹果", "")
                lines.append(f"{other}·{reference.get('grade', '')} {money(reference['other_price'])}")
        deductions = NotificationService._deduction_rows(item)
        if deductions:
            lines.extend(("", f"🔧 {subject}扣费项（→ 扣后回收价）"))
            lines.extend(f"· {name}  {amount}{f' → {after}' if after else ''}" for name, amount, after in deductions)
        elif grades:
            lines.extend(("", f"🔧 {subject}扣费项：报价单未识别到"))
        notes = reference.get("notes") or []
        if notes:
            lines.append(f"💬 {'；'.join(notes)}")
        return (("【待确认】" if (item.get("smart_assessment") or {}).get("status") == "pending" else "") + NotificationService._category(item)), "\n".join(lines)

    @staticmethod
    def _format_webhook_content(
        item: dict[str, Any], reason: str, mobile_url: str, image_urls: list[str]
    ) -> str:
        """Markdown（PushPlus 等）：标题、价格和地区、打开链接、图片；分隔线下面是参考价表格。"""
        money = NotificationService._money

        def cell(value: Any) -> str:
            return str(value).replace("|", "/")

        # 两个尾随空格让价格等字段真正换行
        product_title = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", str(item["title"]).strip())
        item_url = str(item.get("item_url") or "")
        main_url = mobile_url or item_url
        title_line = f"[**{product_title}**]({main_url})" if main_url else f"**{product_title}**"
        details = NotificationService._detail_lines(item, reason, markdown=True)
        details[0] = f"**{details[0]}**"
        blocks = [title_line, "  \n".join(details)]
        links = []
        if main_url:
            label = "📱 手机打开" if mobile_url and mobile_url != item_url else "🔗 打开商品"
            links.append(f"[{label}]({main_url})")
        if item_url and item_url != main_url:
            links.append(f"[💻 网页打开]({item_url})")
        if links:
            blocks.append(" ｜ ".join(links))
        if image_urls:
            blocks.append(f"![商品主图]({image_urls[0]})")

        # 参考价放在最后，只有这里用表格
        reference = item.get("quote_reference") or {}
        grades = reference.get("grades") or []
        selected_grade = item["smart_assessment"].get("grade") if item.get("smart_assessment") else reference.get("grade")
        subject = NotificationService._reference_subject(item)
        tail: list[str] = []
        if grades:
            date = str(reference.get("quote_date") or "")[5:]
            tail.append(f"**📋 {subject}各成色回收价**（{reference.get('category_name', '')}{f' {date}' if date else ''}）")
            headers = [value["grade"] for value in grades]
            values = [
                f"**{money(value['price'])}**" if value["grade"] == selected_grade else money(value["price"])
                for value in grades
            ]
            if reference.get("other_price") is not None:
                headers.append(f"{str(reference.get('other_category_name') or '').replace('苹果', '')}·{reference.get('grade', '')}")
                values.append(money(reference["other_price"]))
            tail.append(
                f"| {' | '.join(map(cell, headers))} |\n| {' | '.join(':-:' for _ in headers)} |\n| {' | '.join(values)} |"
            )
        deductions = NotificationService._deduction_rows(item)
        if deductions:
            rows = "\n".join(f"| {cell(name)} | {amount} | {after} |" for name, amount, after in deductions)
            tail.append(f"**🔧 {subject}扣费项**\n\n| 项目 | 扣费 | 扣后回收价 |\n| :-- | --: | --: |\n{rows}")
        elif grades:
            tail.append(f"**🔧 {subject}扣费项**：报价单未识别到")
        notes = reference.get("notes") or []
        if notes:
            tail.append(f"> 💬 {'；'.join(cell(note) for note in notes)}")
        if tail:
            blocks.extend(("---", *tail))
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
