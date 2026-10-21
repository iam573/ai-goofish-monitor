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
