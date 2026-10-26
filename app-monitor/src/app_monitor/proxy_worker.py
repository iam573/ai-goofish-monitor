# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["mitmproxy==12.3.1", "cryptography==46.0.3", "pyOpenSSL<26"]
# ///
"""Isolated optional runtime. stdout is a whitelist-only product IPC channel."""
import argparse
import asyncio
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading

from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster

from proxy_protocol import HOSTS, SEARCH_API, DETAIL_API, api_name, parse_search, parse_detail, request_meta, verification


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


class Capture:
    def __init__(self):
        self.clients = set()

    def peer(self, flow):
        peer = flow.client_conn.peername
        return str(peer[0]) if peer and str(peer[0]) in self.clients else ""

    def running(self):
        emit("ready")

    def tls_failed_client(self, data):
        peer = data.context.client.peername
        if peer and str(peer[0]) in self.clients:
            emit("tls_failed", client_ip=str(peer[0]))

    def response(self, flow):
        client = self.peer(flow)
        if not client or flow.request.host not in HOSTS:
            return
        emit("traffic", client_ip=client)
        name = api_name(flow.request.path)
        if not name:
            return
        # All raw request data stays inside this process and is never replayed.
        value = flow.request.query.get("data")
        if value is None and "application/x-www-form-urlencoded" in flow.request.headers.get("content-type", ""):
            value = flow.request.urlencoded_form.get("data")
        try:
            request = json.loads(value) if value and len(value) <= 2_000_000 else {}
            meta = request_meta(request)
            context = {"client_ip": client, "api": name, "request_started": flow.request.timestamp_start, **meta}
            body = flow.response.get_content(strict=False)
            payload = json.loads(body) if body and len(body) <= 16_000_000 else None
            if verification(payload, flow.response.status_code):
                emit("verification", **context)
            elif flow.response.status_code != 200 or payload is None:
                emit("parse_error", **context)
            elif name == SEARCH_API:
                result = parse_search(payload)
                emit("search", **context, **result) if result is not None else emit("parse_error", **context)
            elif name == DETAIL_API:
                result = parse_detail(payload, meta["item_id"])
                emit("detail", **context, item=result) if result is not None else emit("parse_error", **context)
        except (ValueError, TypeError, RecursionError, OverflowError):
            # Never log an exception containing a request URL or raw body.
            emit("parse_error", client_ip=client, api=name, request_started=flow.request.timestamp_start)


async def main(args):
    logging.disable(logging.CRITICAL)
    confdir = Path(args.confdir)
    confdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    capture = Capture()
    opts = options.Options(listen_host=args.host, listen_port=args.port, confdir=str(confdir),
                           allow_hosts=[rf"^{re.escape(host)}(?::[0-9]+)?$" for host in HOSTS | {"mitm.it"}])
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.addons.add(capture)
    loop = asyncio.get_running_loop()

    def control():
        for line in sys.stdin:
            try:
                command = json.loads(line)
                values = command.get("clients", [])
                capture.clients = {str(ipaddress.ip_address(value)) for value in values}
            except (ValueError, TypeError):
                continue
        loop.call_soon_threadsafe(master.shutdown)

    threading.Thread(target=control, daemon=True).start()
    await master.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--confdir", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(main(args))
