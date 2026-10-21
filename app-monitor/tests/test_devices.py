import json
from types import SimpleNamespace

from app_monitor.devices import discover_devices


def test_discovery_only_marks_connected_physical_iphones_online(monkeypatch):
    def device(udid, reality, state):
        return {
            "hardwareProperties": {"udid": udid, "deviceType": "iPhone", "reality": reality},
            "deviceProperties": {"name": udid, "osVersionNumber": "27.2"},
            "properties": {"connection": {"state": state}},
        }

    payload = {"result": {"devices": [
        device("online", "physical", "connected"),
        device("paired-only", "physical", "disconnected"),
        device("simulator", "simulated", "connected"),
    ]}}
    monkeypatch.setattr("app_monitor.devices.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload)))

    found = discover_devices()
    assert [(item.id, item.connected) for item in found] == [
        ("ios:online", True), ("ios:paired-only", False),
    ]


def test_xcode26_devices_without_reality_remain_visible_while_pairing(monkeypatch):
    def device(udid, pairing, tunnel, **hardware):
        return {
            "hardwareProperties": {"udid": udid, "deviceType": "iPhone", "platform": "iOS",
                                   "productType": "iPhone14,2", **hardware},
            "deviceProperties": {"name": "iPhone", "osVersionNumber": "17.1"},
            "connectionProperties": {"pairingState": pairing, "tunnelState": tunnel,
                                     "transportType": "wired"},
        }

    payload = {"result": {"devices": [
        device("pairing", "pairingInProgress", "disconnected"),
        device("ready", "paired", "connected"),
        device("offline", "paired", "disconnected"),
        device("not-trusted", "unpaired", "connected"),
        device("simulator", "paired", "connected", reality="simulated"),
        device("ipad", "paired", "connected", deviceType="iPad", productType="iPad14,1"),
    ]}}
    monkeypatch.setattr("app_monitor.devices.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload)))
    found = discover_devices()
    assert [(item.id, item.connected) for item in found] == [
        ("ios:pairing", False), ("ios:ready", True), ("ios:offline", False), ("ios:not-trusted", False),
    ]
    assert found[0].connection_status.startswith("配对中")
    assert found[1].os_version == "17.1"
    assert found[1].connection_status == "已连接"
    assert found[3].connection_status.startswith("未配对")
