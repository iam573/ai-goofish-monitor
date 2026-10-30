"""顶栏「采集设备」中勾选的 iPhone 及各手机的采集通道，保存在 app_metadata 中。"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from typing import Any

from src.infrastructure.persistence.sqlite_connection import init_schema, sqlite_connection

_SELECTION_KEY = "iphone:selected_udids"
_CHANNELS_KEY = "iphone:device_channels"
CHANNELS = ("ui", "proxy")


def _load(key: str, db_path: str | None) -> Any:
    try:
        with sqlite_connection(db_path) as conn:
            row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _save(key: str, value: Any, db_path: str | None) -> None:
    with sqlite_connection(db_path) as conn:
        init_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        conn.commit()


def load_selected_iphone_udids(db_path: str | None = None) -> list[str]:
    values = _load(_SELECTION_KEY, db_path)
    return [str(value) for value in values if str(value).strip()] if isinstance(values, list) else []


def save_selected_iphone_udids(udids: list[str], db_path: str | None = None) -> list[str]:
    cleaned = list(dict.fromkeys(str(udid).strip() for udid in udids if str(udid).strip()))
    _save(_SELECTION_KEY, cleaned, db_path)
    return cleaned


def load_iphone_device_channels(db_path: str | None = None) -> dict[str, dict[str, str]]:
    """{udid: {"channel": "ui"|"proxy", "client_ip": 手机 Wi-Fi IP}}"""
    values = _load(_CHANNELS_KEY, db_path)
    if not isinstance(values, dict):
        return {}
    result = {}
    for udid, entry in values.items():
        if isinstance(entry, dict) and entry.get("channel") in CHANNELS:
            result[str(udid)] = {"channel": entry["channel"], "client_ip": str(entry.get("client_ip") or "")}
    return result


def iphone_device_channel(udid: str, db_path: str | None = None) -> dict[str, str]:
    return load_iphone_device_channels(db_path).get(udid, {"channel": "ui", "client_ip": ""})


def proxy_client_ips(channels: dict[str, dict[str, str]]) -> list[str]:
    return sorted({entry["client_ip"] for entry in channels.values()
                   if entry["channel"] == "proxy" and entry["client_ip"]})


def save_iphone_device_channel(
    udid: str, channel: str, client_ip: str = "", db_path: str | None = None,
) -> dict[str, str]:
    udid = str(udid).strip()
    if not udid:
        raise ValueError("缺少设备 UDID")
    if channel not in CHANNELS:
        raise ValueError("采集通道只能是 ui 或 proxy")
    channels = load_iphone_device_channels(db_path)
    client_ip = str(client_ip or "").strip()
    if client_ip:
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            raise ValueError("请填写手机 Wi-Fi 详情中的有效 IP 地址") from None
        if address.is_loopback or address.is_unspecified:
            raise ValueError("请填写手机 Wi-Fi 详情中的有效 IP 地址")
        client_ip = str(address)
    elif channel == "proxy":
        raise ValueError("代理采集需要填写手机 Wi-Fi 详情中的 IP 地址")
    if channel == "proxy" and any(
        other != udid and entry["channel"] == "proxy" and entry["client_ip"] == client_ip
        for other, entry in channels.items()
    ):
        raise ValueError("该 IP 已绑定另一台代理设备，请核对手机 Wi-Fi IP")
    entry = {"channel": channel, "client_ip": client_ip}
    channels[udid] = entry
    _save(_CHANNELS_KEY, channels, db_path)
    return entry
