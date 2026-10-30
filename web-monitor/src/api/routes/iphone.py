"""
iPhone App 搜索状态、采集设备选择与代理采集通道路由
"""
import asyncio
import sys

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.infrastructure.external.iphone.appium_server import appium_ready
from src.infrastructure.external.iphone.device_lock import iphone_device_busy
from src.infrastructure.external.iphone.devices import candidate_devices, discover_devices
from src.infrastructure.external.iphone.settings import IPhoneSettings
from src.infrastructure.external.iphone.wda_signing import read_wda_signing, renewal_reason
from src.infrastructure.persistence.iphone_device_selection import (
    load_iphone_device_channels,
    load_selected_iphone_udids,
    save_iphone_device_channel,
    save_selected_iphone_udids,
)
from src.services.iphone_proxy_service import get_proxy_service, sync_proxy_service


router = APIRouter(prefix="/api/iphone", tags=["iphone"])


class IPhoneSelectionPayload(BaseModel):
    udids: list[str]


class IPhoneChannelPayload(BaseModel):
    channel: str
    client_ip: str = ""


def _channel_payload(entry: dict) -> dict:
    proxy = entry["channel"] == "proxy"
    return {
        "collection_channel": entry["channel"],
        "proxy_client_ip": entry["client_ip"] or None,
        "proxy_status": get_proxy_service().status(entry["client_ip"]) if proxy else None,
    }


def _device_payload(device, selected: set[str], channels: dict) -> dict:
    busy = sys.platform == "darwin" and device.connected and iphone_device_busy(device.udid)
    entry = channels.get(device.udid, {"channel": "ui", "client_ip": ""})
    return {**device.as_dict(), "selected": device.udid in selected, "busy": busy, **_channel_payload(entry)}


@router.get("/status", response_model=dict)
async def get_iphone_status():
    """检查 iPhone 搜索所需的设备、Appium 和 WDA 配置，不连接手机会话。"""
    settings = IPhoneSettings.from_env()
    selected_udids = await asyncio.to_thread(load_selected_iphone_udids)
    channels = await asyncio.to_thread(load_iphone_device_channels)
    payload = {
        "platform_supported": sys.platform == "darwin",
        "devices": [],
        "device_error": None,
        "selected_udid": settings.udid or None,
        "selected_udids": selected_udids,
        "appium_server_url": settings.appium_server_url,
        "appium_ready": False,
        "appium_autostart": settings.appium_bin is not None,
        "wda_configured": bool(settings.wda_bundle_id),
        "wda_auto_sign": bool(settings.wda_team_id),
        "wda_expires_at": None,
        "wda_issue": None,
        "proxy": await asyncio.to_thread(get_proxy_service().status),
    }
    if not payload["platform_supported"]:
        payload["device_error"] = "iPhone 搜索只能在安装了完整 Xcode 的 macOS 上运行（Docker 中不可用）。"
        return payload
    devices = []
    try:
        devices = await asyncio.to_thread(discover_devices)
    except RuntimeError as exc:
        payload["device_error"] = str(exc)
    selected = set(selected_udids)
    payload["devices"] = await asyncio.to_thread(
        lambda: [_device_payload(device, selected, channels) for device in devices]
    )
    payload["appium_ready"] = await asyncio.to_thread(appium_ready, settings.appium_server_url)
    if settings.wda_bundle_id:
        try:
            targets = candidate_devices(devices, selected_udids, settings.udid)
        except RuntimeError:
            targets = []
        signing = await asyncio.to_thread(read_wda_signing, settings.wda_derived_data_path)
        if signing is not None and signing.expires_at is not None:
            payload["wda_expires_at"] = signing.expires_at.isoformat()
        issues = [
            renewal_reason(signing, bundle_id=settings.wda_bundle_id, udid=device.udid)
            for device in targets
        ] or [renewal_reason(signing, bundle_id=settings.wda_bundle_id)]
        payload["wda_issue"] = next((issue for issue in issues if issue), None)
    return payload


@router.put("/selection", response_model=dict)
async def update_iphone_selection(payload: IPhoneSelectionPayload):
    """保存顶栏勾选的采集手机；iPhone 任务每次从中挑一台空闲的执行。"""
    selected = await asyncio.to_thread(save_selected_iphone_udids, payload.udids)
    return {"selected_udids": selected}


@router.put("/devices/{udid}/channel", response_model=dict)
async def update_iphone_channel(udid: str, payload: IPhoneChannelPayload):
    """切换手机的采集通道；从该手机的下一次搜索生效。"""
    try:
        entry = await asyncio.to_thread(save_iphone_device_channel, udid, payload.channel, payload.client_ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(sync_proxy_service)
    return {"udid": udid, **_channel_payload(entry)}


@router.get("/proxy/certificate")
async def download_proxy_certificate():
    certificate = get_proxy_service().certificate
    if not certificate.is_file():
        raise HTTPException(status_code=404, detail="代理证书尚未生成，请先为手机启用代理采集并等待代理准备完成")
    return FileResponse(certificate, media_type="application/x-x509-ca-cert", filename="goofish-proxy-ca.cer")
