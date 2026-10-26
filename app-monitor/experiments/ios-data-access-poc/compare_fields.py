"""Compare only whitelisted public product fields, never entire flows/payloads."""

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from search_fields import ITEM_ID, price_text

TEXT_KEYS = frozenset({"title", "itemTitle", "desc", "description", "itemDesc", "itemDescription"})
BODY_SUFFIXES = (".desc", ".description", ".itemDesc", ".itemDescription")
PARAM_KEYS = frozenset({"attributes", "properties", "itemParams", "itemProperties", "propertyList", "itemAttributes", "attrs"})
MEDIA_KEYS = frozenset({"imageInfos", "images", "imageUrls", "imgUrls", "pics", "picInfo", "picUrl"})
PATH_KEYS = TEXT_KEYS | PARAM_KEYS | MEDIA_KEYS | frozenset({
    "data", "item", "itemDO", "itemInfo", "itemInfoDO", "result", "resultList",
    "main", "exContent", "fields", "props", "components", "componentData", "model",
    "itemId", "item_id", "price", "priceInfo", "soldPrice", "reservePrice", "sold",
})


def unpack(value):
    if isinstance(value, str) and value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def public_text(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return {"text": value[:20_000], "length": len(value), "truncated": len(value) > 20_000,
            "sha256": hashlib.sha256(value.encode()).hexdigest()}


def walk(value, target):
    stack, count = [(value, "root", None, 0)], 0
    while stack and count < 30_000:
        node, path, scope, depth = stack.pop()
        count += 1
        if depth > 24:
            continue
        node = unpack(node)
        if isinstance(node, dict):
            explicit = node.get("itemId", node.get("item_id"))
            if isinstance(explicit, (str, int)) and ITEM_ID.fullmatch(str(explicit)):
                scope = str(explicit)
            if scope and scope != target:
                continue
            yield node, path, scope
            for key, child in reversed(list(node.items())):
                label = key if key in PATH_KEYS else "*"
                stack.append((child, path + "." + label, scope, depth + 1))
        elif isinstance(node, list):
            stack.extend((child, f"{path}[{index}]", scope, depth + 1)
                         for index, child in reversed(list(enumerate(node[:1000]))))


def normalized(value):
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s\u200b\u200c\u200d\u2060\ufeff]+", "", value)


class PublicFieldComparison:
    def __init__(self, output: Path):
        self.output, self.search, self.detail = output, {}, {}

    def save(self):
        self.output.write_text(json.dumps({"search": self.search, "detail": self.detail}, ensure_ascii=False))

    def observe_search(self, payload):
        root = unpack(payload.get("data", {})) if isinstance(payload, dict) else {}
        cards = root.get("resultList", []) if isinstance(root, dict) else []
        if not isinstance(cards, list):
            return 0
        count = 0
        for card in cards[:128]:
            value = card
            for key in ("data", "item", "main", "exContent"):
                value = unpack(value.get(key, {})) if isinstance(value, dict) else {}
            if not isinstance(value, dict):
                continue
            item_id = str(value.get("itemId", ""))
            title = public_text(value.get("title", value.get("itemTitle")))
            if not ITEM_ID.fullmatch(item_id) or not title:
                continue
            texts = {key: public_text(value[key]) for key in TEXT_KEYS if public_text(value.get(key))}
            self.search[item_id] = {"texts": texts, "price": price_text(value.get("price")),
                                    "area": str(value.get("area", ""))[:60]}
            count += 1
        self.search = dict(list(self.search.items())[-128:])
        self.save()
        return count

    def observe_detail(self, payload, item_id):
        texts, parameters, features = [], [], {}
        for node, path, scope in walk(payload, item_id):
            for key in TEXT_KEYS:
                value = public_text(node.get(key))
                if value and len(texts) < 64:
                    texts.append({"path": path + "." + key, "scope": scope, **value})
            for key in PARAM_KEYS | MEDIA_KEYS:
                value = unpack(node.get(key))
                if isinstance(value, (list, dict, str)) and value:
                    features[path + "." + key] = {"type": type(value).__name__, "count": len(value), "scope": scope}
                if key in PARAM_KEYS:
                    entries = value if isinstance(value, list) else []
                    for entry in entries[:60]:
                        if not isinstance(entry, dict):
                            continue
                        # Public parameter labels/values only, no arbitrary object serialization.
                        pair = {name: entry[name][:500] for name in ("name", "value", "label", "text", "propertyName", "propertyValue")
                                if isinstance(entry.get(name), str)}
                        if pair and len(parameters) < 100:
                            parameters.append({"path": path + "." + key, "scope": scope, **pair})
        self.detail[item_id] = {"texts": texts, "parameters": parameters,
                                "features": dict(list(features.items())[:100])}
        self.detail = dict(list(self.detail.items())[-16:])
        self.save()
        return self.summary(item_id)

    def summary(self, item_id):
        listing = self.search.get(item_id, {})
        detail = self.detail.get(item_id, {})
        titles = list(listing.get("texts", {}).values())
        matches = [field["path"] for field in detail.get("texts", [])
                   if field.get("scope") == item_id and field["path"].endswith(BODY_SUFFIXES)
                   and not field["truncated"] and any(not title["truncated"] and
                       normalized(title["text"]) == normalized(field["text"]) for title in titles)]
        return {"item_id": item_id, "has_list": bool(listing),
                "list_text_lengths": {key: value["length"] for key, value in listing.get("texts", {}).items()},
                "detail_text_fields": [{key: field[key] for key in ("path", "scope", "length", "truncated")}
                                       for field in detail.get("texts", [])],
                "normalized_equal_paths": matches, "parameter_count": len(detail.get("parameters", []))}
