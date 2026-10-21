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
    connection_status: str = ""

    def as_dict(self, selected: bool = False) -> dict:
        return {**self.__dict__, "selected": selected}


def discover_devices() -> list[Device]:
    devices: list[Device] = []
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
        connection = raw.get("connectionProperties") or (raw.get("properties") or {}).get("connection") or {}
        udid = str(hardware.get("udid") or "")
        reality = hardware.get("reality")
        physical = reality == "physical" or (
            reality is None and hardware.get("platform") == "iOS"
            and str(hardware.get("productType") or "").startswith("iPhone")
        )
        if hardware.get("deviceType") != "iPhone" or not physical or not udid:
            continue
        pairing = connection.get("pairingState")
        connected = (connection.get("tunnelState") == "connected" or connection.get("state") == "connected")
        if pairing and pairing != "paired":
            connected = False
        status = "已连接" if connected else "未连接"
        if pairing == "pairingInProgress":
            status = "配对中，请在 Xcode 和手机上完成确认"
        elif pairing and pairing != "paired":
            status = "未配对，请在 Xcode 中连接并信任设备"
        devices.append(Device(
            id=f"ios:{udid}", name=str(props.get("name") or hardware.get("marketingName") or "iPhone"),
            kind="ios", connected=connected,
            udid=udid, os_version=str(props.get("osVersionNumber") or ""),
            connection_status=status,
        ))
    return devices


def selected_devices(database: Database) -> list[str]:
    value = database.state_get(SELECTION_KEY, [])
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item).startswith("ios:")))


def set_selected_devices(database: Database, ids: list[str]) -> list[str]:
    known = {device.id for device in discover_devices() if device.kind == "ios"}
    selected = list(dict.fromkeys(ids))
    if not selected:
        raise ValueError("至少选择一台采集设备")
    unknown = set(selected) - known
    if unknown:
        raise ValueError(f"设备不存在：{', '.join(sorted(unknown))}")
    database.state_set(SELECTION_KEY, selected)
    return selected
