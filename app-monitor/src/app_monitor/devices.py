"""Discover connected iPhones and persist the monitor's selected collection sources."""

from __future__ import annotations

import json
import ipaddress
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .db import Database, utc_now

logger = logging.getLogger(__name__)
MAC_DEVICE = "mac"
SELECTION_KEY = "collector_devices"
CHANNELS_KEY = "collector_device_channels"


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


def _probe_device(identifier: str) -> dict:
    """Wake an existing paired connection without initiating a new pairing."""
    try:
        with tempfile.TemporaryDirectory(prefix="app-monitor-device-") as directory:
            output = Path(directory) / "device.json"
            subprocess.run(
                ["xcrun", "devicectl", "device", "info", "details", "--device", identifier,
                 "--timeout", "5", "--json-output", str(output)],
                capture_output=True, text=True, timeout=7, check=True,
            )
            payload = json.loads(output.read_text())
            return payload.get("result") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


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
        if pairing == "paired" and raw.get("identifier") and (
            (connection.get("tunnelState") != "connected"
             and connection.get("state") != "connected")
            or props.get("developerModeStatus") is None
        ):
            fresh = _probe_device(str(raw["identifier"]))
            # Only accept details for the exact device requested.
            if fresh.get("hardwareProperties", {}).get("udid") == udid:
                connection = fresh.get("connectionProperties") or connection
                props = {**props, **(fresh.get("deviceProperties") or {})}
                pairing = connection.get("pairingState")
        connected = (connection.get("tunnelState") == "connected" or connection.get("state") == "connected")
        if pairing and pairing != "paired":
            connected = False
        status = "已连接" if connected else "未连接"
        if pairing == "pairingInProgress":
            status = "配对中，请在 Xcode 和手机上完成确认"
        elif pairing and pairing != "paired":
            status = "未配对，请在 Xcode 中连接并信任设备"
        elif props.get("developerModeStatus") == "disabled":
            connected = False
            status = "开发者模式未开启，请在手机设置中开启并重启确认"
        elif not connected and pairing == "paired":
            status = "已配对，开发连接未建立，请解锁手机并检查 Xcode 连接"
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


def device_channel(database: Database, device_id: str) -> dict:
    saved = database.state_get(CHANNELS_KEY, {})
    value = saved.get(device_id, {}) if isinstance(saved, dict) else {}
    if not isinstance(value, dict):
        value = {}
    try:
        address = str(ipaddress.ip_address(value.get("client_ip"))) if value.get("client_ip") else ""
    except ValueError:
        address = ""
    return {"channel": value.get("channel") if value.get("channel") in {"ui", "proxy"} else "ui",
            "client_ip": address}


def proxy_clients(database: Database) -> list[str]:
    saved = database.state_get(CHANNELS_KEY, {})
    if not isinstance(saved, dict):
        return []
    result = set()
    for key, value in saved.items():
        if not key.startswith("ios:") or not isinstance(value, dict) or value.get("channel") != "proxy":
            continue
        try:
            if value.get("client_ip"):
                result.add(str(ipaddress.ip_address(value["client_ip"])))
        except ValueError:
            continue
    return sorted(result)


def proxy_enabled(database: Database) -> bool:
    saved = database.state_get(CHANNELS_KEY, {})
    return isinstance(saved, dict) and any(key.startswith("ios:") and isinstance(value, dict)
        and value.get("channel") == "proxy" for key, value in saved.items())


def set_device_channel(database: Database, device_id: str, channel: str, client_ip: str | None = None) -> dict:
    if channel not in {"ui", "proxy"}:
        raise ValueError("采集通道必须为原有采集或代理采集")
    if device_id not in {device.id for device in discover_devices()}:
        raise ValueError("设备不存在，请刷新设备列表")
    previous = device_channel(database, device_id)
    address = previous["client_ip"] if client_ip is None else client_ip.strip()
    if address:
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise ValueError("请填写手机 Wi-Fi 详情中的有效 IP 地址") from exc
        if ipaddress.ip_address(address).is_loopback or ipaddress.ip_address(address).is_unspecified:
            raise ValueError("请填写手机的局域网 IP，不能使用本机地址")
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT value FROM app_state WHERE key=?", (CHANNELS_KEY,)).fetchone()
        saved = json.loads(row["value"]) if row else {}
        saved = saved if isinstance(saved, dict) else {}
        if client_ip is None:
            prior = saved.get(device_id, {})
            try:
                address = str(ipaddress.ip_address(prior["client_ip"])) if isinstance(prior, dict) and prior.get("client_ip") else ""
            except ValueError:
                address = ""
        if channel == "proxy" and address and any(key != device_id and isinstance(value, dict)
                and value.get("channel") == "proxy" and value.get("client_ip") == address for key, value in saved.items()):
            raise ValueError("该 IP 已绑定另一台代理设备，请核对手机 Wi-Fi IP")
        result = {"channel": channel, "client_ip": address}
        saved[device_id] = result
        connection.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                           (CHANNELS_KEY, json.dumps(saved), utc_now()))
    return result
