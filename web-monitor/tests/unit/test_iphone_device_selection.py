from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import iphone
from src.infrastructure.external.iphone.devices import IPhoneDevice
from src.infrastructure.persistence.iphone_device_selection import (
    load_selected_iphone_udids,
    save_selected_iphone_udids,
)


def test_selection_round_trips_and_dedupes(tmp_path):
    db_path = str(tmp_path / "app.sqlite3")
    assert load_selected_iphone_udids(db_path) == []
    assert save_selected_iphone_udids(["B", " ", "A", "B"], db_path) == ["B", "A"]
    assert load_selected_iphone_udids(db_path) == ["B", "A"]
    assert save_selected_iphone_udids([], db_path) == []
    assert load_selected_iphone_udids(db_path) == []


def test_status_marks_selected_and_busy_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("IOS_WDA_BUNDLE_ID", "")
    monkeypatch.setattr(iphone.sys, "platform", "darwin")
    monkeypatch.setattr(iphone, "discover_devices", lambda: [
        IPhoneDevice("UDID-A", "主力机", "18.1", True, "已连接"),
        IPhoneDevice("UDID-B", "备用机", "18.1", False, "未连接"),
    ])
    monkeypatch.setattr(iphone, "iphone_device_busy", lambda udid: udid == "UDID-A")
    monkeypatch.setattr(iphone, "appium_ready", lambda url: True)
    app = FastAPI()
    app.include_router(iphone.router)
    client = TestClient(app)

    saved = client.put("/api/iphone/selection", json={"udids": ["UDID-B", "UDID-A"]})
    status = client.get("/api/iphone/status").json()

    assert saved.json() == {"selected_udids": ["UDID-B", "UDID-A"]}
    assert status["selected_udids"] == ["UDID-B", "UDID-A"]
    assert [(device["udid"], device["selected"], device["busy"]) for device in status["devices"]] == [
        ("UDID-A", True, True),
        ("UDID-B", True, False),
    ]
