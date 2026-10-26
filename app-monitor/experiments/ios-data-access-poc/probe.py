# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3==11.44.0", "cryptography==46.0.3"]
# ///
"""Read-only iOS data-access checks; never opens pages or sends product requests."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.webinspector import WebinspectorService

BUNDLE = "com.taobao.fleamarket"


async def connect(udid: str):
    client = await create_using_usbmux(serial=udid, autopair=False, connection_type="USB")
    if not client.paired:
        await client.close()
        raise RuntimeError("Existing device pairing unavailable; no new pairing attempted")
    return client


async def metadata(udid: str) -> dict:
    async with await connect(udid) as client:
        async with InstallationProxyService(client) as service:
            fields = ["CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion",
                      "ApplicationType", "UIFileSharingEnabled", "IsDeveloperApp", "Entitlements"]
            result = await service.lookup({"BundleIDs": [BUNDLE], "ReturnAttributes": fields})
            app = (result or {}).get(BUNDLE)
            if app is None:
                return {"found": False}
            allowed = {key: app[key] for key in fields[:-1] if key in app}
            entitlements = app.get("Entitlements")
            allowed["entitlements_available"] = isinstance(entitlements, dict)
            if isinstance(entitlements, dict):
                allowed["get_task_allow"] = entitlements.get("get-task-allow")
            return {"found": True, "app": allowed}


async def container(udid: str) -> dict:
    async with await connect(udid) as client:
        service = await HouseArrestService.create(client, BUNDLE, documents_only=False)
        try:
            names = await service.listdir("Library")
            candidates = [name for name in names
                          if name.lower() in {"caches", "application support"}
                          or name.lower().endswith((".db", ".sqlite", ".sqlite3"))]
            return {"accessible": True, "directory_entry_count": len(names), "candidates": candidates}
        finally:
            await service.close()


async def webview(udid: str) -> dict:
    async with await connect(udid) as client:
        service = WebinspectorService(client)
        try:
            await service.connect()
            pages = await service.get_open_application_pages(timeout=3)
            apps = [app for app in service.connected_application.values() if app.bundle == BUNDLE]
            owned_ids = {app.id_ for app in apps}
            matches = [pair for pair in pages
                       if pair.application.bundle == BUNDLE or pair.application.host in owned_ids]
            return {
                "inspector_connected": True,
                "total_page_count": len(pages),
                "goofish_application_count": len(apps),
                "goofish_pages": [{"page_id": pair.page.id_, "type": pair.page.type_.value,
                                   "already_inspected": bool(pair.page.web_connection_id)}
                                  for pair in matches],
            }
        finally:
            await service.close()


async def main(args: argparse.Namespace) -> int:
    probes = {"metadata": metadata, "container": container, "webview": webview}
    records = []
    for name in probes if args.probe == "all" else [args.probe]:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(probes[name](args.udid), timeout=18)
            record = {"probe": name, "status": "completed", "result": result}
        except Exception as error:
            record = {"probe": name, "status": "failed", "error_type": type(error).__name__,
                      "error": str(error).splitlines()[0][:400] if str(error) else ""}
        record["elapsed_seconds"] = round(time.monotonic() - started, 2)
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(any(record["status"] == "failed" for record in records))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True, help="Explicit USB device UDID")
    parser.add_argument("--probe", choices=["all", "metadata", "container", "webview"], default="all")
    parser.add_argument("--output", type=Path, help="Optional sanitized JSON output; prefer .runtime/")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
