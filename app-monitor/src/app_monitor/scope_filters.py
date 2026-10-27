"""Pure acquisition filters; public listing evidence only, never infer missing identity."""
from __future__ import annotations

import html
import re
import unicodedata

VARIANT_LABELS = {
    "美版": "美版", "日版": "日版", "韩版": "韩版", "欧版": "欧版",
    "港澳版": "港澳版", "台版": "台版", "加版": "加版", "海外版": "其他外版",
}
DEFAULT_SELLER_KEYWORDS = [
    "通讯", "通信", "手机专卖", "手机店", "二手机", "二手手机", "手机回收",
    "高价回收", "上门回收", "卖手机", "实体数码", "严选数码", "数码店", "数码科技",
]


def explicit_variants(text: str) -> list[tuple[str, str]]:
    from .local_rules import clean, mentions
    from .quote_rules import VARIANTS

    text = clean(text)
    found = {}
    for variant, patterns in {**VARIANTS, "海外版": [*VARIANTS["海外版"], r"非国行"]}.items():
        for positive, evidence, start, end in mentions(text, patterns):
            if positive is not True:
                continue
            left = re.split(r"[，,。；;！!？?\n]", text[:start])[-1][-24:]
            if re.search(r"(?:非|不收|不卖|不接受|不要|不适用|不支持|适配|支持|改成?|刷成|换成)\s*$", left):
                continue
            # A US replacement screen is not a statement that the phone is US-market.
            if re.match(r"\s*(?:的)?(?:原装|原厂|拆机|官方|国产|进口)?(?:屏幕?|电池|电芯|后盖|中框|主板|配件|零件|包装盒|手机壳)", text[end:]):
                continue
            found.setdefault(variant, evidence)
    if len(found) > 1:
        found.pop("海外版", None)  # A specific market takes precedence over generic overseas wording.
    if "国行" in found and len(found) > 1:
        return []  # Conflicting versions remain subject to normal detail review.
    return list(found.items())


def normalize_seller_name(value: str) -> str:
    """Normalize invisible spacing and case, preserving literal keyword punctuation."""
    value = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    return "".join(char for char in value if not char.isspace() and unicodedata.category(char) != "Cf")


def scope_rejection(item: dict, config, *, detail: bool = False) -> tuple[str, str] | None:
    seller = str(item.get("seller_name") or "").strip()
    if config.exclude_trade_sellers and seller:
        normalized = normalize_seller_name(seller)
        for keyword in config.seller_exclude_keywords:
            normalized_keyword = normalize_seller_name(keyword)
            if normalized_keyword and normalized_keyword in normalized:
                return "seller_filtered", f"卖家昵称命中过滤词「{keyword}」：{seller[:120]}"
        if (config.exclude_digital_shop_names and not normalized.endswith(("...", "…"))
                and re.search(r"数码(?:.{0,5}店)?$", normalized)):
            return "seller_filtered", f"卖家昵称符合数码商家名称规则：{seller[:120]}"
    if config.exclude_overseas:
        content = str(item.get("title") or "")
        if detail:
            content += "\n" + str(item.get("description") or "") + "\n" + "\n".join(item.get("detail_parameters") or [])
        for variant, evidence in explicit_variants(content):
            if variant in config.blocked_variants:
                return "variant_filtered", f"不收该版本：{variant}（{evidence[:120]}）"
    return None
