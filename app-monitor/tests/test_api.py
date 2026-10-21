from fastapi.testclient import TestClient

from app_monitor import api
from app_monitor.devices import Device
from conftest import add_quotes


async def _noop():
    return None


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
