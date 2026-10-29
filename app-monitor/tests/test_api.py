from fastapi.testclient import TestClient

from app_monitor import api
from app_monitor.devices import Device
from conftest import add_quotes


async def test_run_detail_exposes_saved_early_valuation_and_keeps_old_rows_unknown(database, monkeypatch):
    import json
    from app_monitor.db import utc_now
    from app_monitor.matching import MatchDecision
    from app_monitor.service import MonitorService

    monkeypatch.setattr(api, "database", database)
    saved = {"reference_grade": "小花", "grade": None, "base_price": 3550,
             "value_low": None, "value_high": 3550, "threshold_high": 3750, "deductions": []}
    with database.connect() as connection:
        run_id = connection.execute("INSERT INTO search_runs(model, keyword, started_at) VALUES ('16 Pro','iPhone 16 Pro',?)", (utc_now(),)).lastrowid
    service = MonitorService(database, None, None)
    for key, valuation in [("old", None), ("new", saved)]:
        service._save_run_item(run_id, "128", {"item_id": key, "title": "iPhone 16 Pro 128G", "price": 3844,
            "display_region": "厦门", "item_url": "", "publish_time": None},
            MatchDecision(False, "价格超出", code="price_high", quote_basis="detail", valuation=valuation), False)
    # Migration is idempotent and does not reconstruct the old row's snapshot.
    database.initialize()
    response = await api.run_detail(run_id)
    items = {item["item_id"]: item for item in response["items"]}
    assert items["old"]["valuation"] is None and items["new"]["valuation"] == saved
    assert "valuation_json" not in items["new"] and items["new"]["assessment_id"] is None
    with database.connect() as connection:
        assert json.loads(connection.execute("SELECT valuation_json FROM run_items WHERE item_id='new'").fetchone()[0]) == saved


async def _noop():
    return None


async def test_retry_search_api_returns_result_and_clear_errors(monkeypatch):
    import pytest
    from fastapi import HTTPException

    async def retry_ok():
        return {"model": "16", "run_id": 7}

    monkeypatch.setattr(api.monitor_service, "retry_failed", retry_ok)
    assert await api.retry_search() == {"model": "16", "run_id": 7}

    async def blocked():
        raise ValueError("正在搜索，请稍后再试")

    monkeypatch.setattr(api.monitor_service, "retry_failed", blocked)
    with pytest.raises(HTTPException) as error:
        await api.retry_search()
    assert error.value.status_code == 409

    async def failed():
        raise RuntimeError("手机连接失败")

    monkeypatch.setattr(api.monitor_service, "retry_failed", failed)
    with pytest.raises(HTTPException) as error:
        await api.retry_search()
    assert error.value.status_code == 502 and error.value.detail == "手机连接失败"


async def test_device_channel_api_exposes_both_modes_and_only_the_public_ca(database, monkeypatch, tmp_path):
    from types import SimpleNamespace
    import app_monitor.devices as module
    found = [Device("ios:one", "One", "ios", True)]
    certificate = tmp_path / "public.cer"
    certificate.write_text("PUBLIC CERTIFICATE")
    (tmp_path / "mitmproxy-ca.pem").write_text("PRIVATE KEY")
    calls = []
    proxy = SimpleNamespace(certificate=certificate,
        configure=lambda clients: calls.append(("clients", clients)), start=lambda: calls.append(("start",)),
        status=lambda ip="": {"ready": True, "client_ip": ip}, stop=lambda: None)
    monkeypatch.setattr(api, "database", database)
    monkeypatch.setattr(api.monitor_service, "proxy", proxy)
    monkeypatch.setattr(api, "discover_devices", lambda: found)
    monkeypatch.setattr(module, "discover_devices", lambda: found)
    assert (await api.devices())["devices"][0]["collection_channel"] == "ui"
    saved = await api.change_device_channel("ios:one", api.DeviceChannelPayload(channel="proxy", client_ip="192.168.1.20"))
    assert saved["channel"] == "proxy" and ("start",) in calls
    assert (await api.devices())["devices"][0]["proxy_client_ip"] == "192.168.1.20"
    await api.change_device_channel("ios:one", api.DeviceChannelPayload(channel="ui"))
    assert (await api.devices())["devices"][0]["collection_channel"] == "ui"
    response = await api.proxy_certificate()
    assert response.path == certificate and "PRIVATE" not in certificate.read_text()


def test_run_groups_merge_capacities_and_include_older_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(api.database, "path", tmp_path / "run-groups.sqlite3")
    monkeypatch.setattr(api, "refresh_quotes_if_stale", _noop)
    with TestClient(api.app) as client:
        with api.database.connect() as connection:
            connection.executemany(
                """INSERT INTO search_runs(model, keyword, storage, started_at, ok, observed, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [("16 Pro Max", "iPhone 16 Pro Max", "256", "2026-10-20T10:00:00", 0, 0, "失败")]
                + [("16", "iPhone 16", "128" if i % 2 else "256", "2026-10-21T10:00:00", 1, 2, None)
                   for i in range(125)]
                + [("16", "iPhone 16", None, "2026-10-22T10:00:00", None, 1, None),
                   ("16 Pro", "iPhone 16 Pro", "256", "2026-10-22T11:00:00", 1, 3, "部分失败")],
            )
        groups = client.get("/api/run-groups").json()
        assert [group["model"] for group in groups] == ["16 Pro", "16", "16 Pro Max"]
        base = groups[1]
        assert base["run_count"] == 126 and base["item_count"] == 251
        assert base["running_count"] == 1
        assert base["latest_status"] == "running"
        assert groups[0]["latest_status"] == "warn"
        assert groups[2]["latest_status"] == "failed"
        assert [g["model"] for g in client.get("/api/run-groups?status=failed").json()] == ["16 Pro Max"]
        assert [g["model"] for g in client.get("/api/run-groups?status=warn").json()] == ["16 Pro"]
        assert client.get("/api/run-groups?status=hit").json() == []
        assert client.get("/api/run-groups?status=invalid").status_code == 422
        history = client.get("/api/runs?model=16&limit=1000").json()
        assert len(history) == 126 and history[0]["id"] == base["latest_id"]
        assert {run["storage"] for run in history} == {None, "128", "256"}
        # 历史失败不影响恢复后的状态、筛选；状态取最后一条记录而非累计异常。
        with api.database.connect() as connection:
            connection.execute("""INSERT INTO search_runs(model,keyword,started_at,ok)
                                  VALUES('16 Pro Max','iPhone 16 Pro Max','2026-10-23T09:00:00',1)""")
            connection.execute("""INSERT INTO search_runs(model,keyword,started_at,ok)
                                  VALUES('16 Pro','iPhone 16 Pro','2026-10-23T09:00:00',1)""")
        latest = {g["model"]: g for g in client.get("/api/run-groups").json()}
        assert latest["16 Pro Max"]["latest_status"] == "completed"
        assert latest["16 Pro Max"]["failed_count"] == 1
        assert latest["16 Pro"]["latest_status"] == "completed"
        assert latest["16 Pro"]["warn_count"] == 1
        assert client.get("/api/run-groups?status=failed").json() == []
        assert client.get("/api/run-groups?status=warn").json() == []


def test_quote_schedule_runs_once_daily_at_configured_time(monkeypatch):
    from dataclasses import replace
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(api, 'settings', replace(api.settings, quote_hour=9, quote_minute=0,
                                                timezone='Asia/Shanghai'))
    trigger = api.quote_refresh_trigger()
    zone = ZoneInfo('Asia/Shanghai')
    assert trigger.get_next_fire_time(None, datetime(2026, 10, 22, 13, 5, tzinfo=zone)) == datetime(2026, 10, 23, 9, 0, tzinfo=zone)
    assert trigger.get_next_fire_time(None, datetime(2026, 10, 22, 23, 20, tzinfo=zone)) == datetime(2026, 10, 23, 9, 0, tzinfo=zone)
    assert not api.quote_time_reached(datetime(2026, 10, 22, 8, 59, tzinfo=zone))
    assert trigger.get_next_fire_time(None, datetime(2026, 10, 22, 8, 59, tzinfo=zone)) == datetime(2026, 10, 22, 9, 0, tzinfo=zone)
    monkeypatch.setattr(api, 'settings', replace(api.settings, quote_hour=10, quote_minute=17))
    assert api.quote_refresh_trigger().get_next_fire_time(None, datetime(2026, 10, 22, 10, 0, tzinfo=zone)) == datetime(2026, 10, 22, 10, 17, tzinfo=zone)


def test_device_selection_is_phone_only_and_mac_is_rejected(tmp_path, monkeypatch):
    import app_monitor.devices as devices_module

    found = [Device("ios:phone-1", "测试 iPhone", "ios", True, "phone-1", "27.2")]
    monkeypatch.setattr(api.database, "path", tmp_path / "devices.sqlite3")
    monkeypatch.setattr(api, "refresh_quotes_if_stale", _noop)
    monkeypatch.setattr(api, "discover_devices", lambda: found)
    monkeypatch.setattr(devices_module, "discover_devices", lambda: found)
    with TestClient(api.app) as client:
        api.database.state_set("smart_config", {"enabled": False})
        assert client.get("/api/devices").json()["selected"] == []
        assert client.put("/api/devices/selection", json={"ids": ["ios:phone-1"]}).status_code == 200
        assert set(client.get("/api/devices").json()["selected"]) == {"ios:phone-1"}
        assert client.put("/api/devices/selection", json={"ids": ["mac"]}).status_code == 400
        assert client.put("/api/devices/selection", json={"ids": []}).status_code == 400
        assert client.put("/api/devices/selection", json={"ids": ["ios:unknown"]}).status_code == 400


def test_api_flow_rules_targets_and_injected_items(tmp_path, monkeypatch):
    sent = []

    async def fake_send(item, reason):
        sent.append((item["item_id"], reason))
        return {"test": {"success": True}}

    monkeypatch.setattr(api.database, "path", tmp_path / "api.sqlite3")
    monkeypatch.setattr(api, "refresh_quotes_if_stale", _noop)
    # API 测试会命中虚构商品；绝不能使用本机 .env 中的真实推送渠道。
    monkeypatch.setattr(api.notification_service, "send", fake_send)
    with TestClient(api.app) as client:
        api.database.state_set("smart_config", {"enabled": False})
        batch_id = add_quotes(api.database, [
            ("XR", "64", "小花", 500), ("15 Pro", "256", "小花", 4000),
            ("15 Pro", "256", "靓机", 4300), ("17 Pro", "256", "小花", 5000),
        ])

        rules = client.get("/api/rules").json()
        assert rules["quote_grade"] == "小花"
        assert rules["max_over_quote"] == 200
        rules.update(region_city="厦门", search_start="00:00", search_end="00:00", max_over_quote=250)
        saved = client.put("/api/rules", json=rules).json()
        assert saved["region_steps"] == ["区域", "福建", "厦门", "查看"]
        assert saved["max_over_quote"] == 250
        assert client.put("/api/rules", json={**rules, "max_over_quote": -1}).status_code == 422
        assert client.put("/api/rules", json={**rules, "quote_grade": "九九新"}).status_code == 422

        targets = client.get("/api/targets").json()
        assert {t["model"] for t in targets} == {"XR", "15 Pro", "17 Pro"}
        target = next(t for t in targets if t["model"] == "15 Pro")
        assert target["units"][0]["storage"] == "256"
        client.put("/api/targets/15 Pro", json={"enabled": False, "storage": "256"})
        target = next(t for t in client.get("/api/targets").json() if t["model"] == "15 Pro")
        # 15 Pro 只有 256G 一个容量：容量全部停用时机型也显示为未启用
        assert target["enabled"] is False and target["units"][0]["enabled"] is False
        assert client.put("/api/targets/15 Pro", json={"enabled": False}).json()["enabled"] is False
        assert next(t for t in client.get("/api/targets").json() if t["model"] == "15 Pro")["enabled"] is False
        assert client.post("/api/targets/15 Pro/run?storage=999").status_code == 404
        assert client.put("/api/target-series/17", json={"enabled": False}).status_code == 200
        assert all(not t["enabled"] for t in client.get("/api/targets").json() if t["series"] == 17)
        client.put("/api/target-series/17", json={"enabled": True})

        result = client.post(
            "/api/targets/15 Pro/observations?storage=256",
            json=[{"item_id": "1", "title": "iPhone 15 Pro 256G", "price": 3500, "display_region": "厦门思明"}],
        ).json()
        assert result["matched"] == 1
        assert len(sent) == 1 and sent[0][0] == "1"
        deals = client.get("/api/deals").json()
        assert deals[0]["model"] == "15 Pro" and deals[0]["region"] == "厦门思明"
        run = client.get("/api/runs").json()[0]
        assert run["trigger"] == "inject" and run["passes"][0]["label"] == "调试"
        assert run["item_count"] == 1 and run["hit_count"] == 1 and run["new_count"] == 1
        detail = client.get(f"/api/runs/{run['id']}").json()
        assert detail["items"][0]["matched"] == 1 and detail["items"][0]["code"] == "matched"
        assert detail["codes"] == {"matched": 1}
        assert client.get("/api/runs?status=hit").json()[0]["id"] == run["id"]
        group = client.get("/api/run-groups?status=hit").json()[0]
        assert group["model"] == "15 Pro" and group["hit_count"] == 1 and group["new_count"] == 1
        assert client.get("/api/runs?status=failed").json() == []
        assert client.get("/api/runs/99999").status_code == 404
        target = next(t for t in client.get("/api/targets").json() if t["model"] == "15 Pro")
        assert target["last_run"]["observed"] == 1

        page = client.get("/")
        assert "/assets/app.js?v=" in page.text and page.headers["cache-control"] == "no-cache"
        assert client.get("/assets/app.js").headers["cache-control"] == "no-cache"

        overview = client.get("/api/overview").json()
        assert overview["deals_today"] == 1
        assert overview["quotes"][0]["entry_count"] == 4
        assert len(client.get("/api/apple-products").json()) == 3

        image_path = tmp_path / "daily-quotes" / "100025" / "quote.png"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"\x89PNG\r\n\x1a\noriginal quote")
        with api.database.connect() as connection:
            connection.execute(
                "UPDATE quote_batches SET image_path=? WHERE id=?", (str(image_path), batch_id)
            )
        status = client.get("/api/quotes/status").json()[0]
        assert status["batch_id"] == batch_id and status["image_available"] is True
        assert client.get("/api/quotes/batches").json()[0]["image_available"] is True
        original = client.get(f"/api/quotes/batches/{batch_id}/image")
        assert original.status_code == 200 and original.content == image_path.read_bytes()
        assert original.headers["content-type"] == "image/png"
        assert client.get("/api/quotes/batches/999/image").status_code == 404
        with api.database.connect() as connection:
            connection.execute(
                "UPDATE quote_batches SET image_path=? WHERE id=?",
                (str(tmp_path / "outside.png"), batch_id),
            )
        assert client.get(f"/api/quotes/batches/{batch_id}/image").status_code == 404
        assert client.get("/api/quotes/batches").json()[0]["image_available"] is False


def test_series_and_model_selection_cascades_to_all_capacities(tmp_path, monkeypatch):
    monkeypatch.setattr(api.database, "path", tmp_path / "selection.sqlite3")
    monkeypatch.setattr(api, "refresh_quotes_if_stale", _noop)
    with TestClient(api.app) as client:
        api.database.state_set("smart_config", {"enabled": False})
        add_quotes(api.database, [
            ("16", "128", "小花", 3000),
            ("17", "256", "小花", 4000),
            ("17 Pro", "256", "小花", 5000),
            ("17 Pro", "512", "小花", 5500),
        ])
        rules = client.get("/api/rules").json()
        rules["disabled_models"] = ["16|128", "17", "17 Pro|512"]
        client.put("/api/rules", json=rules).raise_for_status()

        client.put("/api/target-series/17", json={"enabled": True}).raise_for_status()
        targets = {item["model"]: item for item in client.get("/api/targets").json()}
        assert all(unit["enabled"] for model in ("17", "17 Pro") for unit in targets[model]["units"])
        assert targets["16"]["units"][0]["enabled"] is False
        assert client.get("/api/rules").json()["disabled_models"] == ["16|128"]

        client.put("/api/target-series/17", json={"enabled": False}).raise_for_status()
        targets = {item["model"]: item for item in client.get("/api/targets").json()}
        assert all(not unit["enabled"] for model in ("17", "17 Pro") for unit in targets[model]["units"])

        client.put("/api/target-series/17", json={"enabled": True}).raise_for_status()
        client.put("/api/targets/17 Pro", json={"enabled": False, "storage": "512"}).raise_for_status()
        targets = {item["model"]: item for item in client.get("/api/targets").json()}
        assert [unit["enabled"] for unit in targets["17 Pro"]["units"]] == [True, False]
        client.put("/api/targets/17 Pro", json={"enabled": True}).raise_for_status()
        targets = {item["model"]: item for item in client.get("/api/targets").json()}
        assert all(unit["enabled"] for unit in targets["17 Pro"]["units"])
        client.put("/api/targets/17 Pro", json={"enabled": False}).raise_for_status()
        targets = {item["model"]: item for item in client.get("/api/targets").json()}
        assert all(not unit["enabled"] for unit in targets["17 Pro"]["units"])


        # 取消整个系列后，仍可单独勾选其中的某个容量或机型
        client.put("/api/target-series/17", json={"enabled": False}).raise_for_status()
        client.put("/api/targets/17 Pro", json={"enabled": True, "storage": "256"}).raise_for_status()
        targets = {item["model"]: item for item in client.get("/api/targets").json()}
        assert [unit["enabled"] for unit in targets["17 Pro"]["units"]] == [True, False]
        assert targets["17 Pro"]["enabled"] is True and targets["17"]["enabled"] is False
        client.put("/api/targets/17", json={"enabled": True}).raise_for_status()
        assert {item["model"]: item for item in client.get("/api/targets").json()}["17"]["enabled"] is True

        # 旧版本保存的系列级 / 机型级停用项：勾选子项时自动展开
        rules = client.get("/api/rules").json()
        rules["disabled_models"] = ["@series:17", "16"]
        client.put("/api/rules", json=rules).raise_for_status()
        client.put("/api/targets/17 Pro", json={"enabled": True, "storage": "512"}).raise_for_status()
        client.put("/api/targets/16", json={"enabled": True, "storage": "128"}).raise_for_status()
        targets = {item["model"]: item for item in client.get("/api/targets").json()}
        assert [unit["enabled"] for unit in targets["17 Pro"]["units"]] == [False, True]
        assert targets["17"]["enabled"] is False and targets["16"]["enabled"] is True
        assert client.get("/api/rules").json()["disabled_models"] == ["17 Pro|256", "17|256"]


def test_quote_deductions_and_reparse_keep_manual_prices(tmp_path, monkeypatch):
    from app_monitor.quotes import QuoteDeduction, QuoteEntry

    monkeypatch.setattr(api.database, "path", tmp_path / "deductions.sqlite3")
    monkeypatch.setattr(api, "refresh_quotes_if_stale", _noop)
    monkeypatch.setattr(api, "backfill_deductions", _noop)
    with TestClient(api.app) as client:
        api.database.state_set("smart_config", {"enabled": False})
        batch_id = add_quotes(api.database, [("15 Pro", "256", "小花", 4000), ("15 Pro", "256", "靓机", 4300)])
        image = tmp_path / "quote.png"
        image.write_bytes(b"png")
        with api.database.connect() as connection:
            connection.execute("UPDATE quote_batches SET image_path=? WHERE id=?", (str(image), batch_id))
            connection.execute("UPDATE quote_entries SET price=4100, source='manual' WHERE grade='小花'")

        def fake_parse(path):
            return (
                [QuoteEntry("15 Pro", "256", "小花", 3900), QuoteEntry("15 Pro", "256", "靓机", 4350)],
                [QuoteDeduction("15 Pro", 0, 0, "港澳版-600-800", "港澳版", 600, 800),
                 QuoteDeduction("15 Pro", 1, 0, "后盖破当大花收", "后盖破当大花收", kind="note")],
            )

        monkeypatch.setattr(api.quote_service.parser, "parse_image", fake_parse)
        result = client.post("/api/quotes/reparse").json()
        assert result[0]["deduction_count"] == 2

        deductions = client.get("/api/quotes/deductions", params={"model": "15 Pro"}).json()
        assert [d["name"] for d in deductions["no_warranty"]] == ["港澳版", "后盖破当大花收"]
        assert deductions["no_warranty"][0]["amount_max"] == 800 and deductions["warranty"] == []

        product = client.get("/api/apple-products").json()[0]
        assert product["grades"]["小花"]["no_warranty"]["price"] == 4100  # 手动校正保留
        assert product["grades"]["靓机"]["no_warranty"]["price"] == 4350  # OCR 结果更新
        assert client.get("/api/quotes/status").json()[0]["deduction_count"] == 2
