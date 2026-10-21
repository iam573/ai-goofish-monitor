"""Discover connected iPhones and persist the monitor's selected collection sources."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

from .db import Database

logger = logging.getLogger(__name__)
MAC_DEVICE = "mac"
SELECTION_KEY = "collector_devices"


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    kind: str
    connected: bool
    udid: str = ""
    os_version: str = ""

    def as_dict(self, selected: bool = False) -> dict:
        return {**self.__dict__, "selected": selected}


def discover_devices() -> list[Device]:
    devices = [Device(MAC_DEVICE, "本机 Mac 闲鱼", "mac", True)]
    try:
        completed = subprocess.run(
            ["xcrun", "devicectl", "list", "devices", "--json-output", "-"],
            capture_output=True, text=True, timeout=12, check=True,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("读取 iPhone 列表失败：%s", exc)
        return devices
    for raw in payload.get("result", {}).get("devices", []):
        hardware = raw.get("hardwareProperties") or {}
        props = raw.get("deviceProperties") or {}
        connection = (raw.get("properties") or {}).get("connection") or {}
        udid = str(hardware.get("udid") or "")
        if hardware.get("deviceType") != "iPhone" or hardware.get("reality") != "physical" or not udid:
            continue
        devices.append(Device(
            id=f"ios:{udid}", name=str(props.get("name") or hardware.get("marketingName") or "iPhone"),
            kind="ios", connected=connection.get("state") == "connected",
            udid=udid, os_version=str(props.get("osVersionNumber") or ""),
        ))
    return devices


def selected_devices(database: Database) -> list[str]:
    value = database.state_get(SELECTION_KEY, [MAC_DEVICE])
    if not isinstance(value, list):
        return [MAC_DEVICE]
    return list(dict.fromkeys(str(item) for item in value if item == MAC_DEVICE or str(item).startswith("ios:")))


def set_selected_devices(database: Database, ids: list[str]) -> list[str]:
    known = {device.id for device in discover_devices()}
    selected = list(dict.fromkeys(ids))
    if not selected:
        raise ValueError("至少选择一台采集设备")
    unknown = set(selected) - known
    if unknown:
        raise ValueError(f"设备不存在：{', '.join(sorted(unknown))}")
    database.state_set(SELECTION_KEY, selected)
    return selected
