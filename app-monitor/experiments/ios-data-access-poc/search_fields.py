"""Bounded, independent parser for public product fields; no raw payload output."""

import json
import re
from collections import Counter


ITEM_ID = re.compile(r"[1-9][0-9]{7,19}\Z")
KNOWN_KEYS = frozenset({
    "data", "resultList", "list", "result", "item", "main", "exContent",
    "itemId", "item_id", "title", "itemTitle", "price", "priceInfo", "text",
    "area", "areaName", "district", "hasNextPage", "hasNext", "nextPage",
    "pageNumber", "page", "rowsPerPage", "pageSize", "hasMore",
})


def walk(value):
    stack, count = [(value, 0)], 0
    while stack and count < 30_000:
        node, depth = stack.pop()
        count += 1
        if depth > 24:
            continue
        if isinstance(node, str) and node.startswith(("{", "[")):
            try:
                node = json.loads(node)
            except (ValueError, RecursionError):
                continue
        if isinstance(node, dict):
            yield node
            stack.extend((child, depth + 1) for child in reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in reversed(node[:3000]))


def text(value, limit=180):
    return value.strip()[:limit] if isinstance(value, str) else ""


def price_text(value):
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()[:40]
    if isinstance(value, list):
        return "".join(str(part.get("text", "")) for part in value[:12] if isinstance(part, dict))[:40]
    if isinstance(value, dict):
        return price_text(value.get("price", value.get("text")))
    return ""


def search_fields(payload):
    products, shape, pagination = {}, Counter(), {}
    for node in walk(payload):
        shape.update(key for key in node if key in KNOWN_KEYS)
        for key in ("hasNextPage", "hasNext", "hasMore"):
            if isinstance(node.get(key), bool):
                pagination[key] = node[key]
        item_id = node.get("itemId", node.get("item_id"))
        if not isinstance(item_id, (str, int)) or not ITEM_ID.fullmatch(str(item_id)):
            continue
        title = text(node.get("title", node.get("itemTitle")))
        if not title:
            continue
        # Associate only fields from the same explicit product object.
        product = {"item_id": str(item_id), "title": title,
                   "price": price_text(node.get("price", node.get("priceInfo"))),
                   "area": text(node.get("area", node.get("areaName", node.get("district"))), 40)}
        if len(products) < 128 or product["item_id"] in products:
            products.setdefault(product["item_id"], product)
    return {"products": list(products.values()), "shape": dict(shape), "pagination": pagination}


def request_pages(payload):
    """Numeric pagination only; never emit opaque cursor/tracking parameters."""
    if not isinstance(payload, dict):
        return {}
    result = {}
    for key in ("pageNumber", "pageNo", "page", "rowsPerPage", "pageSize"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = str(value)
            if value.isdigit() and 0 < int(value) <= 10_000:
                result[key] = int(value)
    return result
