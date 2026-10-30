"""Minimal public product contract for the optional iOS proxy worker (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

SEARCH_API = "mtop.taobao.idlemtopsearch.search"
DETAIL_API = "mtop.taobao.idle.awesome.detail.unit"
ITEM_ID = re.compile(r"[1-9][0-9]{7,19}\Z")
HOSTS = frozenset({"acs.m.goofish.com", "g-acs.m.goofish.com", "api.goofish.com",
                   "acs.m.taobao.com", "guide-acs.m.taobao.com", "h5.m.goofish.com", "pages.goofish.com"})


def unpack(value):
    if isinstance(value, str) and value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def text(value, limit=20_000):
    # Never silently assess a truncated description.
    return value.strip() if isinstance(value, str) and len(value) <= limit else ""


def item_id(value):
    return str(value) if isinstance(value, (str, int)) and ITEM_ID.fullmatch(str(value)) else ""


def api_name(path):
    parsed = urlsplit(path)
    match = re.search(r"/(?:gw|h5)/([^/]+)/", parsed.path)
    value = (match.group(1) if match else parse_qs(parsed.query).get("api", [""])[0]).lower()
    return value if value in {SEARCH_API, DETAIL_API} else ""


def price(value):
    if isinstance(value, list):
        value = "".join(str(part.get("text", "")) for part in value[:12] if isinstance(part, dict))
    if isinstance(value, dict):
        return price(value.get("price", value.get("value", value.get("text"))))
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    match = re.fullmatch(r"\s*[¥￥]?\s*(\d{1,8}(?:\.\d{1,2})?)\s*(?:元)?\s*", str(value))
    amount = float(match.group(1)) if match else None
    return amount if amount is not None and math.isfinite(amount) and amount > 0 else None


def image_url(value):
    value = text(value, 2048)
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host.endswith(".alicdn.com") or host.endswith(".taobaocdn.com")):
        return ""
    if parsed.username or parsed.password or any(key.lower() in {"token", "sign", "signature", "auth", "authorization"}
                                               for key in parse_qs(parsed.query)):
        return ""
    return value


def request_meta(payload):
    payload = payload if isinstance(payload, dict) else {}
    page = payload.get("pageNumber")
    page = int(page) if isinstance(page, (str, int)) and str(page).isdigit() and 1 <= int(page) <= 10_000 else None
    # Only the digest leaves the worker, never the original filter/tracking data.
    filters = {key: unpack(value) for key, value in payload.items()
               if re.search(r"filter|condition|region|province|district|city|area|prop|division|storage|memory|brand|model|category|leaf|sort", key, re.I)
               and not re.search(r"token|sign|track|cursor|page|spm|device", key, re.I)}
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"keyword": text(payload.get("keyword"), 200), "page": page,
            "sort_field": text(payload.get("sortField"), 40), "sort_value": text(payload.get("sortValue"), 40),
            "filter_fingerprint": fingerprint, "item_id": item_id(payload.get("itemId", payload.get("item_id")))}


def verification(payload, status=200):
    if status in {419, 420}:
        return True
    codes = payload.get("ret", []) if isinstance(payload, dict) else []
    return isinstance(codes, list) and any(isinstance(value, str) and any(code in value for code in
        ("FAIL_SYS_USER_VALIDATE", "FAIL_SYS_TRAFFIC_LIMIT", "FAIL_SYS_FLOW_LIMIT", "FAIL_SYS_ANTI")) for value in codes)


def parse_search(payload):
    data = unpack(payload.get("data", {})) if isinstance(payload, dict) else {}
    if not isinstance(data, dict) or not isinstance(data.get("resultList"), list):
        return None
    result = {}
    for card in data["resultList"][:200]:
        main = card
        for key in ("data", "item", "main"):
            main = unpack(main.get(key, {})) if isinstance(main, dict) else {}
        ex = unpack(main.get("exContent", {})) if isinstance(main, dict) else {}
        if not isinstance(ex, dict):
            continue
        key, title, amount = item_id(ex.get("itemId")), text(ex.get("title")), price(ex.get("price"))
        if not key or not title or amount is None:
            continue
        click = main.get("clickParam", {})
        args = click.get("args", {}) if isinstance(click, dict) else {}
        stamp = args.get("publishTime") if isinstance(args, dict) else None
        published = None
        if isinstance(stamp, (str, int)) and str(stamp).isdigit() and 1_000_000_000_000 <= int(stamp) < 10_000_000_000_000:
            published = datetime.fromtimestamp(int(stamp) / 1000, timezone.utc).isoformat()
        image = image_url(ex.get("picUrl"))
        item = {"item_id": key, "title": title, "price": amount, "display_region": text(ex.get("area"), 80),
                "publish_time": published, "item_url": f"https://www.goofish.com/item?id={key}",
                "image_urls": [image] if image else [], "seller_name": text(ex.get("userNickName"), 120)}
        if isinstance(ex.get("isFishShop"), bool):
            item["is_fish_shop"] = ex["isFishShop"]
        result.setdefault(key, item)
    more = data.get("hasNextPage", (data.get("resultInfo") or {}).get("hasNextPage") if isinstance(data.get("resultInfo"), dict) else None)
    if more in ("true", "false"):
        more = more == "true"
    return {"items": list(result.values()), "has_more": more if isinstance(more, bool) else None}


def owned_nodes(payload, target):
    stack, visited = [(payload, None, 0)], 0
    while stack and visited < 30_000:
        node, scope, depth = stack.pop()
        visited += 1
        if depth > 24:
            continue
        node = unpack(node)
        if isinstance(node, dict):
            scope = item_id(node.get("itemId", node.get("item_id"))) or scope
            if scope and scope != target:
                continue
            if scope == target:
                yield node
            for key, child in node.items():
                if not re.search(r"seller|user|recommend|comment|track|log|share", key, re.I):
                    stack.append((child, scope, depth + 1))
        elif isinstance(node, list):
            stack.extend((child, scope, depth + 1) for child in node[:1000])


def parse_detail(payload, target):
    if not item_id(target):
        return None
    bodies, amounts, images, parameters = [], set(), [], []
    for node in owned_nodes(payload, target):
        body = next((text(node.get(key)) for key in ("desc", "description", "itemDesc") if text(node.get(key))), "")
        if body:
            bodies.append(body)
        for key in ("price", "soldPrice", "itemPrice", "priceInfo"):
            amount = price(node.get(key))
            if amount is not None:
                amounts.add(amount)
        for key in ("attributes", "properties", "itemParams", "itemProperties", "propertyList", "itemAttributes"):
            values = unpack(node.get(key, []))
            for entry in values[:60] if isinstance(values, list) else []:
                if isinstance(entry, dict):
                    label = text(entry.get("name", entry.get("propertyName")), 100)
                    value = text(entry.get("value", entry.get("propertyValue")), 500)
                    if label and value:
                        parameters.append(f"{label}：{value}")
        for key in ("picUrl", "imageUrl", "url"):
            url = image_url(node.get(key))
            if url and len(images) < 12 and url not in images:
                images.append(url)
    if not bodies:
        return None
    body = max(bodies, key=len)
    normalize = lambda value: re.sub(r"[\s\u200b\u200c\u200d\u2060\ufeff]+", "", value)
    if any(not normalize(body).startswith(normalize(value)) for value in bodies):
        return None
    return {"item_id": target, "description": body, "title": body,
            "price": next(iter(amounts)) if len(amounts) == 1 else None,
            "detail_parameters": list(dict.fromkeys(parameters))[:100], "image_urls": images}
