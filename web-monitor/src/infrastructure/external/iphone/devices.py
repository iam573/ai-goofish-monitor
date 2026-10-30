"""通过 Xcode 的 devicectl 发现已连接的 iPhone，并读写手机剪贴板。"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IPhoneDevice:
    udid: str
    name: str
    os_version: str
    connected: bool
    status: str

    def as_dict(self) -> dict:
        return asdict(self)


def _disconnected_status(connection: dict) -> str:
    if not connection.get("transportType"):
        return "未连接：请插上数据线并解锁手机"
    return "通道未建立：请解锁手机，或重新插拔数据线"


def parse_devicectl_devices(payload: dict) -> list[IPhoneDevice]:
    devices: list[IPhoneDevice] = []
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
        connected = connection.get("tunnelState") == "connected" or connection.get("state") == "connected"
        if pairing and pairing != "paired":
            connected = False
        status = "已连接" if connected else _disconnected_status(connection)
        if pairing == "pairingInProgress":
            status = "配对中，请在 Xcode 和手机上完成确认"
        elif pairing and pairing != "paired":
            status = "未配对，请在 Xcode 中连接并信任设备"
        elif connected and props.get("developerModeStatus") == "disabled":
            connected = False
            status = "未开启开发者模式"
        devices.append(IPhoneDevice(
            udid=udid,
            name=str(props.get("name") or hardware.get("marketingName") or "iPhone"),
            os_version=str(props.get("osVersionNumber") or ""),
            connected=connected,
            status=status,
        ))
    return devices


def discover_devices() -> list[IPhoneDevice]:
    """返回本机可见的 iPhone；没有 Xcode/devicectl 时抛出 RuntimeError。"""
    try:
        completed = subprocess.run(
            ["xcrun", "devicectl", "list", "devices", "--json-output", "-"],
            capture_output=True, text=True, timeout=12, check=True,
        )
        payload = json.loads(completed.stdout)
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 xcrun：iPhone 搜索只能在安装了完整 Xcode 的 macOS 上运行") from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(f"读取 iPhone 列表失败（需要完整 Xcode 的 devicectl）：{exc}") from exc
    return parse_devicectl_devices(payload)


def select_device(devices: list[IPhoneDevice], udid: str = "") -> IPhoneDevice:
    if udid:
        device = next((item for item in devices if item.udid == udid), None)
        if device is None:
            raise RuntimeError(f"未找到 IPHONE_UDID 指定的 iPhone：{udid}")
        if not device.connected:
            raise RuntimeError(f"iPhone「{device.name}」{device.status}")
        return device
    connected = [item for item in devices if item.connected]
    if not connected:
        detail = "；".join(f"{item.name}：{item.status}" for item in devices) or "没有发现 iPhone"
        raise RuntimeError(f"没有已连接的 iPhone（{detail}）")
    return connected[0]


def candidate_devices(
    devices: list[IPhoneDevice], selected_udids: list[str], udid: str = "",
) -> list[IPhoneDevice]:
    """顶栏勾选了手机时在其中已连接的手机里挑；未勾选时沿用 IPHONE_UDID / 第一台已连接手机。"""
    if not selected_udids:
        return [select_device(devices, udid)]
    by_udid = {device.udid: device for device in devices}
    connected = [by_udid[item] for item in selected_udids if item in by_udid and by_udid[item].connected]
    if not connected:
        detail = "；".join(
            f"{by_udid[item].name}：{by_udid[item].status}" if item in by_udid else f"{item[-6:]}：未发现"
            for item in selected_udids
        )
        raise RuntimeError(f"已选的 iPhone 都未连接（{detail}）")
    return connected


def read_pasteboard(udid: str) -> str:
    process = subprocess.run(
        ["xcrun", "devicectl", "device", "pasteboard", "paste", "--device", udid],
        capture_output=True, text=True, timeout=12, check=True,
    )
    return process.stdout.strip()


def prepare_pasteboard(udid: str) -> str:
    """写入一次性标记，确保随后读到的是本次复制的链接而不是旧内容。"""
    marker = f"web-monitor-copy-{uuid.uuid4().hex}"
    subprocess.run(
        ["xcrun", "devicectl", "device", "pasteboard", "copy", "--device", udid],
        input=marker, capture_output=True, text=True, timeout=12, check=True,
    )
    return marker
