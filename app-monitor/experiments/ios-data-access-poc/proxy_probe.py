# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["mitmproxy==12.3.1", "cryptography==46.0.3", "pyOpenSSL<26"]
# ///
"""Observe existing responses; never replay, modify, or persist raw traffic."""

import argparse
import asyncio
from collections import Counter
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, parse_qs

from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster

from search_fields import search_fields, request_pages
from compare_fields import PublicFieldComparison


# Offline package evidence, not a claim that the installed version uses these.
HOSTS = frozenset({
    "acs.m.goofish.com", "g-acs.m.goofish.com", "api.goofish.com",
    "acs.m.taobao.com", "guide-acs.m.taobao.com", "trade-acs.m.taobao.com",
    "h5.m.goofish.com", "pages.goofish.com", "ssr.m.goofish.com",
})
API = re.compile(r"(?:mtop\.taobao\.(?:idle|idlemtopsearch|idleitem)|com\.taobao\.idle)\.[a-z0-9_.]+\Z")
PATH_API = re.compile(r"/(?:gw|h5)/([^/]+)/")
ITEM_ID = re.compile(r"[1-9][0-9]{7,19}\Z")


def product_api(name):
    return bool(name and any(word in name for word in ("item", "detail", "search", "share")))


def product_ids(value):
    """Only explicit item identity keys; never seller IDs or arbitrary numbers."""
    found, stack, visited = set(), [(value, 0)], 0
    while stack and visited < 20_000:
        node, depth = stack.pop()
        visited += 1
        if depth > 20:
            continue
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"itemId", "item_id"} and isinstance(child, (str, int)):
                    candidate = str(child)
                    if ITEM_ID.fullmatch(candidate):
                        found.add(candidate)
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
                elif isinstance(child, str) and child.startswith(("{", "[")):
                    try:
                        stack.append((json.loads(child), depth + 1))
                    except (ValueError, RecursionError):
                        pass
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node[:10_000])
    return sorted(found)[:128]


def api_name(path):
    parsed = urlsplit(path)
    match = PATH_API.search(parsed.path)
    candidate = (match.group(1) if match else parse_qs(parsed.query).get("api", [""])[0]).lower()
    return candidate if API.fullmatch(candidate) else None


def request_payload(request):
    value = request.query.get("data")
    if value is None and "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        value = request.urlencoded_form.get("data")
    if value and len(value) <= 2_000_000:
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return None


class Observer:
    def __init__(self, output, compare_fields=False):
        self.output = output
        self.counts = Counter()
        self.items = {}
        self.search_pages = []
        self.comparison = PublicFieldComparison(output.parent / "public-field-comparison.json") if compare_fields else None

    def emit(self, event, host, **fields):
        if host not in HOSTS:
            return
        self.counts[event] += 1
        # Only whitelisted fields generated below can reach disk/stdout.
        data = {"event": event, "host": host, **fields}
        name = fields.get("api")
        ids = fields.get("item_ids", [])
        if name and ids and (name in self.items or len(self.items) < 64):
            self.items[name] = sorted(set(self.items.get(name, [])) | set(ids))[:128]
        if event == "search_data":
            self.search_pages = (self.search_pages + [data])[-12:]
        print(json.dumps(data, ensure_ascii=False), flush=True)
        self.output.write_text(json.dumps({"counts": self.counts, "items_by_api": self.items,
                                          "search_pages": self.search_pages,
                                          "last": data}, ensure_ascii=False))

    def http_connect(self, flow):
        self.emit("proxy_connect", flow.request.host)

    def tls_failed_client(self, data):
        self.emit("client_tls_failed", data.context.client.sni)

    def request(self, flow):
        name = api_name(flow.request.path)
        if flow.request.host not in HOSTS or not product_api(name):
            return
        # Observe only explicit product identity from the App's existing payload.
        # Never output or replay the payload, headers, signatures, or other fields.
        payload = request_payload(flow.request)
        ids = product_ids(payload)
        if ids:
            self.emit("request_product_ids", flow.request.host, api=name, item_ids=ids)

    def response(self, flow):
        host = flow.request.host
        if host not in HOSTS:
            return
        response = flow.response
        name = api_name(flow.request.path)
        fields = {"status": response.status_code, "api": name,
                  "format": "not_product_api", "item_ids": []}
        # Parse product APIs only, not login responses or arbitrary H5 content.
        if product_api(name):
            fields["format"] = "unparsed"
            try:
                body = response.get_content(strict=False)
                if body and len(body) <= 16_000_000:
                    payload = json.loads(body)
                    fields["item_ids"] = product_ids(payload)
                    fields["format"] = "json"
                    if self.comparison and name == "mtop.taobao.idlemtopsearch.search":
                        count = self.comparison.observe_search(payload)
                        self.emit("main_search_fields", host, api=name, product_count=count)
                    if self.comparison and name == "mtop.taobao.idle.awesome.detail.unit":
                        requested = product_ids(request_payload(flow.request))
                        if len(requested) == 1:
                            comparison = self.comparison.observe_detail(payload, requested[0])
                            self.emit("field_comparison", host, api=name, **comparison)
                    if "search" in name:
                        result = search_fields(payload)
                        products = result["products"]
                        self.emit("search_data", host, api=name,
                                  request_page=request_pages(request_payload(flow.request)),
                                  product_count=len(products), item_ids=[p["item_id"] for p in products],
                                  fields_present={key: sum(bool(p[key]) for p in products)
                                                  for key in ("title", "price", "area")},
                                  samples=products[:3], shape=result["shape"], pagination=result["pagination"])
                else:
                    fields["format"] = "oversize" if body else "empty"
            except (ValueError, RecursionError):
                pass
        self.emit("response", host, **fields)


async def serve(args):
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    confdir = runtime / "ca"
    confdir.mkdir(exist_ok=True, mode=0o700)
    os.chmod(confdir, 0o700)
    # Disable mitmproxy logs, including errors that may contain raw URLs.
    logging.disable(logging.CRITICAL)
    opts = options.Options(
        listen_host=args.listen_host, listen_port=args.port, confdir=str(confdir),
        allow_hosts=[rf"^{re.escape(host)}(?::[0-9]+)?$" for host in HOSTS | {"mitm.it"}],
    )
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.addons.add(Observer(runtime / "summary.json", compare_fields=args.compare_fields))

    async def expire():
        await asyncio.sleep(args.duration)
        master.shutdown()

    expiry = asyncio.create_task(expire())
    print(json.dumps({"event": "starting", "listen": args.listen_host, "port": args.port,
                      "duration_seconds": args.duration, "public_ca": str(confdir / "mitmproxy-ca-cert.cer")}))
    try:
        await master.run()
    finally:
        expiry.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--compare-fields", action="store_true", help="Compare whitelisted full public list/detail text in ignored runtime only")
    parser.add_argument("--runtime", type=Path, default=Path(".runtime/ios-proxy-probe"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.duration <= 3600:
        parser.error("port must be 1..65535 and duration 1..3600 seconds")
    os.umask(0o077)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
