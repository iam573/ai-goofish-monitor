"""An item's own words must establish its phone model; search context is not evidence."""
from __future__ import annotations

import re

from .matching import expand_shorthand, model_in_title, normalize_search_text
from .model_names import normalize_model

_PHONE_DETAILS = re.compile(r'国行|港版|美版|日版|韩版|电池|面容|插卡|拆修|原装机|账号已退|账户已退|ios\b', re.I)
_ACCESSORY = re.compile(r'手机壳|保护壳|保护套|钢化膜|屏幕总成|主板单卖|电池单卖', re.I)
_PHONE_CONDITION = re.compile(r'电池(?:健康|效率|还有|刚换|\s*\d)|面容正常|账号已退|无拆(?:无)?修|无维修|全原|原装机|插卡即用|整机|功能(?:都|全)?正常', re.I)
_REFERENCE = re.compile(r'(?:适用于|适用|适配|兼容|支持|用于|for|可装|能装|装下)\s*$', re.I)
_OTHER_BRAND = re.compile(r'(?:小米|红米|华为|荣耀|三星|vivo|oppo|一加|realme|红魔|魅族|真我)(?:note|nova)?\s*$', re.I)


def item_text(item: dict) -> str:
    return '\n'.join([str(item.get('title') or ''), str(item.get('description') or ''),
                      *[str(value) for value in item.get('detail_parameters') or []]])


def has_model_evidence(model: str, text: str) -> bool:
    """Keep branded names/shorthand, reject incidental numbers and compatibility references."""
    if not model or not model.strip():
        return False
    text = expand_shorthand(text).casefold()
    target = normalize_model(model)
    pattern = re.escape(target).replace(r'\ ', r'\s*')
    # iPhone Air is the marketed alias of the quote sheet's 17 Air.
    if normalize_search_text(target) == '17air':
        pattern = rf'(?:{pattern}|air)'
    for match in re.finditer(rf'(?<![a-z0-9])(?P<brand>iphone\s*|苹果(?:手机)?\s*)?(?P<model>{pattern})', text, re.I):
        left = re.split(r'[，,。;；\n]', text[:match.start()])[-1][-24:]
        tail = text[match.end():]
        if _REFERENCE.search(left) or _OTHER_BRAND.search(left):
            continue
        if re.match(r'\s*(?:元|块|厘米|cm\b|寸|英寸|岁|个|张|年|%|％|hz\b|mah\b)', tail, re.I):
            continue
        # Reuse exact Pro/Max/Plus and glued capacity boundaries from matching.
        fragment = match.group() + re.split(r'[，,。;；\n]', tail)[0]
        if not model_in_title(target, fragment):
            continue
        # A case/film title must contain actual phone-condition evidence too.
        if _ACCESSORY.search(text) and not _PHONE_CONDITION.search(text):
            continue
        variant = bool(re.search(r'pro|plus|mini|max|air|xr|xs|se', match.group('model'), re.I))
        if not match.group('brand') and not variant and re.search(r'(?:循环|次数|价格|运费|健康度?|电池|高度|宽度|长度|尺寸|编号)\s*(?:为|约|:|：)?\s*$', left):
            continue
        if match.group('brand') or variant or _PHONE_DETAILS.search(text):
            return True
    return False
