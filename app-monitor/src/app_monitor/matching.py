"""商品判价：标题识别机型与容量，按全局规则和报价计算是否值得推送。

这里只做纯计算，不读写数据库，方便单测。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .quotes import normalize_model, normalize_storage
from .regions import region_status

if TYPE_CHECKING:  # pragma: no cover
    from .rules import Rules


# 机型后缀：标题中目标机型后面紧跟这些字样时，说明是另一个机型（例如 15 后面跟 Pro）。
_VARIANT_SUFFIXES = ("PROMAX", "PRO", "PLUS", "MAX", "MINI", "AIR", "E")


@dataclass(frozen=True)
class MatchDecision:
    matched: bool
    reason: str
    model: str | None = None
    storage: str | None = None
    quote_entry_id: int | None = None
    quote_price: float | None = None
    threshold_price: float | None = None
    expected_margin: float | None = None
    region: str | None = None
    # 结论分类，供运行记录筛选：matched / price_high / too_cheap / region / unrecognized / excluded / no_quote / other_model
    code: str = ""


def extract_storage(title: str) -> str | None:
    text = unicodedata.normalize("NFKC", title).translate(_ZERO_WIDTH).upper()
    patterns = [
        r"(?<!\d)(64|128|256|512)\s*(?:G|GB)(?![A-Z])",
        r"(?<!\d)(1|2)\s*T(?:B)?(?![A-Z])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1) + ("T" if "T" in match.group(0) else "")
            return normalize_storage(value)
    # 卖家常省略单位，例如「16promax 256 沙漠色」；只接受独立出现、不像价格或百分比的数字。
    bare = re.search(r"(?<![\d.¥￥])(64|128|256|512)(?![\d.%]|\s*(?:元|块|R|W|K|MAH|HZ))", text)
    return bare.group(1) if bare else None


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def expand_shorthand(title: str) -> str:
    """卖家常用简写：15p → 15 Pro，15pm → 15 Pro Max（不影响 15plus / 15pro）。"""
    text = unicodedata.normalize("NFKC", title).translate(_ZERO_WIDTH)
    text = re.sub(r"(?<!\d)(1[1-9])\s*pm(?![a-z])", r"\1 Pro Max", text, flags=re.I)
    return re.sub(r"(?<!\d)(1[1-9])\s*p(?![a-z])", r"\1 Pro", text, flags=re.I)


def _compact(value: str) -> str:
    """大写并去掉空格与标点；中文和数字之间的空格保留为分隔符 ``|``，避免数字粘连。"""
    text = expand_shorthand(value).upper().replace("苹果", "IPHONE")
    text = re.sub(r"(?<=\d)\s+(?=\d)", "|", text)
    text = re.sub(r"[\u3400-\u9fff]+", "|", text)
    return re.sub(r"[^A-Z0-9|]", "", text)


_STORAGE_PREFIX = re.compile(r"(?:64|128|256|512)(?!\d)|[12]T")

# 报价表机型名 → 卖家标题里的常见写法（已去空格大写）。
MODEL_ALIASES = {"17AIR": ("IPHONEAIR",)}


def model_in_title(model: str, title: str) -> bool:
    """标题是否明确提到该机型；15 不会误认 15 Pro，12 不会误认 512G。"""
    target = _compact(normalize_model(model)).replace("|", "")
    text = _compact(title)
    if not target:
        return False
    return any(_contains_model(text, value) for value in (target, *MODEL_ALIASES.get(target, ())))


def _contains_model(text: str, target: str) -> bool:
    start = text.find(target)
    while start >= 0:
        before = text[start - 1] if start else ""
        after = text[start + len(target):]
        digit_glued = after[:1].isdigit() and not _STORAGE_PREFIX.match(after)
        if not before.isdigit() and not digit_glued:
            if not any(after.startswith(suffix) for suffix in _VARIANT_SUFFIXES):
                return True
        start = text.find(target, start + 1)
    return False


def normalize_search_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z㐀-鿿]", "", text)


def explicit_iphone_models(title: str) -> list[str]:
    """Read explicitly branded models, including models absent from the quote sheet.

    Bare numbers (battery health, cycle counts, prices) are never model evidence.
    """
    text = expand_shorthand(title)
    pattern = r"(?:iphone|苹果(?:手机)?)\s*(\d{1,2}|xs|xr|x|se)(?!\d)\s*(pro\s*max|pro|plus|mini|max|air|s\s*plus|s|e)?"
    models = []
    for match in re.finditer(pattern, text, re.I):
        generation, suffix = match.group(1), match.group(2) or ""
        suffix = re.sub(r"\s+", " ", suffix, flags=re.I)
        value = f"{generation}{suffix}" if suffix.casefold().startswith('s') else f"{generation} {suffix}"
        models.append(normalize_model(value))
    return list(dict.fromkeys(models))


def recognize_apple_product(
    title: str, products: list[dict[str, Any]], storage: str | None = None
) -> tuple[str, str] | None:
    """从标题中识别报价表已有的苹果机型和容量；标题没写容量时可用 ``storage`` 兜底。"""
    storage = extract_storage(title) or storage
    if not storage:
        return None
    candidates = [
        product
        for product in products
        if product["storage"] == storage and model_in_title(product["model"], title)
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda product: len(_compact(product["model"]).replace("|", "")))
    return str(selected["model"]), storage


def evaluate(
    item: dict[str, Any],
    rules: "Rules",
    quotes: list[dict[str, Any]],
    region_applied: bool = False,
    storage_hint: str | None = None,
    expected_model: str | None = None,
) -> MatchDecision:
    """按全局规则判断一件商品。

    ``quotes`` 是当前规则下的报价条目（已按参考成色过滤），
    ``region_applied`` 表示本轮搜索已在 App 内成功切换到目标地区；
    ``storage_hint`` 是 App 内已成功筛选的容量，标题没写容量时使用。
    """
    title = str(item.get("title") or "")
    folded = normalize_search_text(title)
    for word in rules.excluded:
        if normalize_search_text(word) and normalize_search_text(word) in folded:
            return MatchDecision(False, f"标题含排除词「{word}」", code="excluded")

    storage = extract_storage(title) or storage_hint
    if not storage:
        return MatchDecision(False, "标题中无法识别容量", code="unrecognized")
    recognized = recognize_apple_product(title, quotes, storage)
    if not recognized:
        return MatchDecision(False, f"标题无法对应报价表中的机型（{storage}）", storage=storage, code="unrecognized")
    model, storage = recognized
    if expected_model and model != normalize_model(expected_model):
        return MatchDecision(
            False,
            f"商品机型为 {model}，与当前搜索目标 {normalize_model(expected_model)} 不一致",
            model=model,
            storage=storage,
            code="other_model",
        )

    region = str(item.get("display_region") or "").strip()
    city = rules.region_city.strip()
    if city:
        status = region_status(region, city)
        if status == "other":
            return MatchDecision(False, f"地区「{region}」不是{city}", model=model, storage=storage, region=region, code="region")
        if status == "unknown" and not region_applied:
            label = f"「{region}」" if region else ""
            return MatchDecision(
                False, f"无法确认商品地区{label}是否在{city}", model=model, storage=storage,
                region=region or None, code="region",
            )
        region = region or city

    quote = next(
        (entry for entry in quotes if entry["model"] == model and entry["storage"] == storage),
        None,
    )
    if not quote:
        return MatchDecision(
            False, f"{model} {storage} 没有{rules.quote_grade}报价", model=model, storage=storage, code="no_quote"
        )

    quote_price = float(quote["price"])
    item_price = float(item["price"])
    threshold = quote_price + rules.max_over_quote
    margin = quote_price - item_price
    common = dict(
        model=model,
        storage=storage,
        quote_entry_id=int(quote["id"]),
        quote_price=quote_price,
        threshold_price=threshold,
        expected_margin=margin,
        region=region or None,
    )
    if item_price < quote_price * rules.min_price_ratio:
        return MatchDecision(
            False,
            f"商品价 ¥{item_price:g} 低于报价的 {rules.min_price_ratio:.0%}，疑似引流或非整机",
            code="too_cheap",
            **common,
        )
    if item_price > threshold:
        return MatchDecision(False, f"挂牌价 ¥{item_price:g} 高于推送上限 ¥{threshold:g}", code="price_high", **common)
    place = f" · {region}" if region else ""
    difference = (
        f"低于回收报价 ¥{margin:g}" if margin > 0 else
        f"高于回收报价 ¥{-margin:g}" if margin < 0 else "与回收报价持平"
    )
    return MatchDecision(
        True,
        f"{model} {display_storage(storage)} · {rules.quote_grade}回收报价 ¥{quote_price:g} · 挂牌价{difference}{place}",
        code="matched",
        **common,
    )


def display_storage(storage: str) -> str:
    return f"{storage}B" if storage.endswith("T") else f"{storage}G"
