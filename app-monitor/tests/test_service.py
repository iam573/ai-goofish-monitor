from datetime import datetime, timedelta
import json

import pytest

from app_monitor.collector import CollectResult
from app_monitor.devices import Device
from app_monitor.rules import Rules, load_rules, save_rules
from app_monitor.service import MonitorService
from conftest import add_quotes


@pytest.fixture(autouse=True)
def phone_only_fixture(database, monkeypatch):
    database.state_set("collector_devices", ["ios:test-phone"])
    database.state_set("smart_config", {"enabled": False})
    monkeypatch.setattr("app_monitor.service.discover_devices", lambda: [
        Device("ios:test-phone", "测试 iPhone", "ios", True, "test-phone", "27.2")])


class FakeCollector:
    def __init__(self, items=None, error=None, filter_ok=True):
        self.items, self.error, self.filter_ok, self.calls = items or [], error, filter_ok, []

    async def collect(self, keyword, region_steps=None, region_hint="", storage_filters=None, scroll_pages=0, on_items=None):
        self.calls.append(
            {"keyword": keyword, "region": region_steps, "hint": region_hint,
             "storage_filters": storage_filters, "sort": "新发"}
        )
        if self.error:
            raise RuntimeError(self.error)
        filters = storage_filters or [("", [])]
        copied_items = []
        passes = []
        for storage, steps in filters:
            copied_items.extend({**item, "source_storage": storage or None} for item in self.items)
            passes.append({"label": "新发", "storage": storage or None, "filter_applied": bool(steps) and self.filter_ok,
                           "observed": len(self.items), "error": None if self.filter_ok else "筛选失败"})
        return CollectResult(
            items=copied_items,
            region_applied=bool(region_steps),
            filter_applied=bool(storage_filters) and self.filter_ok,
            passes=passes,
        )


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.payloads = []

    async def send(self, item, reason):
        self.sent.append((item["item_id"], reason))
        self.payloads.append(item)
        return {"ntfy": {"success": True}}


def setup(database, collector=None, **rules):
    add_quotes(database, [
        ("15 Pro", "256", "小花", 4000), ("15 Pro", "512", "小花", 4600), ("16", "128", "小花", 3000),
    ])
    rules.setdefault("search_start", "00:00")
    rules.setdefault("search_end", "00:00")  # 测试中全天可搜
    save_rules(database, Rules(**rules))
    notifier = FakeNotifier()
    fake = collector or FakeCollector()
    service = MonitorService(database, None, notifier)

    class PhoneAdapter:
        async def collect(self, udid, version, keyword, city, steps, storages, pages, is_candidate, on_items=None, check_detail=None):
            return await fake.collect(keyword, region_steps=steps, region_hint=city,
                                      storage_filters=[(value, load_rules(database).storage_steps(value)) for value in storages],
                                      scroll_pages=pages, on_items=on_items)
    service.iphone_collector = PhoneAdapter()
    return service, notifier


async def test_run_model_applies_region_and_capacity_filter(database):
    items = [
        {"item_id": "a", "title": "iPhone 15 Pro 256G 国行", "price": 3500, "display_region": "厦门"},
        {"item_id": "b", "title": "iPhone 15 Pro 256G 国行", "price": 3900, "display_region": "厦门"},
        {"item_id": "c", "title": "高价回收 iPhone 15 Pro 256G", "price": 3000, "display_region": "厦门"},
        {"item_id": "d", "title": "iPhone 15 Pro 原色 自用", "price": 3400, "display_region": "厦门"},
        {"item_id": "e", "title": "iPhone 15 Pro Max 256G 国行", "price": 3000, "display_region": "厦门"},
    ]
    collector = FakeCollector(items)
    service, notifier = setup(database, collector, region_city="厦门")
    add_quotes(database, [
        ("15 Pro", "256", "小花", 4000), ("15 Pro", "512", "小花", 4600),
        ("16", "128", "小花", 3000), ("15 Pro Max", "256", "小花", 4800),
    ])

    result = await service.run_model("15 Pro", "256")
    assert collector.calls == [{
        "keyword": "iPhone 15 Pro",
        "region": ["区域", "福建", "厦门", "查看"],
        "hint": "厦门",
        "storage_filters": [("256", ["筛选", "存储容量", "256G", "确定"])],
        "sort": "新发",
    }]
    # d 标题没写容量，但 App 内已筛选 256G，按 256G 报价判断
    assert result["observed"] == 5 and result["matched"] == 3
    assert sorted(sent[0] for sent in notifier.sent) == ["a", "b", "d"]
    assert notifier.payloads[0]["model"] == "15 Pro"
    assert notifier.payloads[0]["storage"] == "256"
    assert notifier.payloads[0]["quote_price"] == 4000

    again = await service.run_model("15 Pro", "256")
    assert again["matched"] == 0 and len(notifier.sent) == 3
    with database.connect() as connection:
        run = connection.execute("SELECT * FROM search_runs ORDER BY id DESC").fetchone()
        decision = connection.execute("SELECT decision FROM observed_items WHERE item_id='c'").fetchone()[0]
    assert run["storage"] == "256" and run["filter_applied"] == 1 and run["region_applied"] == 1
    assert '"新发"' in run["passes"] and "降价" not in run["passes"]
    with database.connect() as connection:
        rows = {r["item_id"]: dict(r) for r in connection.execute("SELECT * FROM run_items WHERE run_id=?", (run["id"],))}
    # 第二次搜索同样看到 5 件；a、b、d 仍判为命中但已推送过，不算新命中；轮次标签为筛选的容量
    assert len(rows) == 5 and rows["a"]["matched"] == 1 and rows["a"]["new_deal"] == 0
    assert rows["b"]["code"] == "matched" and rows["c"]["code"] == "excluded"
    assert rows["e"]["code"] == "other_model"
    assert rows["a"]["pass_label"] == "256G" and sorted(r["position"] for r in rows.values()) == [0, 1, 2, 3, 4]
    assert "回收" in decision
    pro_max_decision = next(d for d in result["decisions"] if d["item_id"] == "e")
    assert not pro_max_decision["matched"] and "与当前搜索目标" in pro_max_decision["reason"]


async def test_one_search_changes_filter_for_each_enabled_capacity(database):
    collector = FakeCollector()
    service, _ = setup(database, collector)
    result = await service.run_model("15 Pro")
    assert len(collector.calls) == 1
    assert collector.calls[0]["keyword"] == "iPhone 15 Pro"
    assert collector.calls[0]["storage_filters"] == [
        ("256", ["筛选", "存储容量", "256G", "确定"]),
        ("512", ["筛选", "存储容量", "512G", "确定"]),
    ]
    assert [entry["storage"] for entry in result["passes"]] == ["256", "512"]


async def test_iphone_prefilters_before_processing_shared_id(database, monkeypatch):
    import app_monitor.service as service_module

    service, notifier = setup(database, FakeCollector(), region_city="厦门")
    database.state_set("collector_devices", ["ios:phone-1"])
    monkeypatch.setattr(service_module, "discover_devices", lambda: [
        Device("mac", "Mac", "mac", True),
        Device("ios:phone-1", "测试 iPhone", "ios", True, "phone-1", "27.2"),
    ])

    class FakePhone:
        async def collect(self, udid, version, keyword, city, region_steps, storages, pages, is_candidate, on_items=None, check_detail=None):
            assert (udid, version, keyword, city, storages) == (
                "phone-1", "27.2", "iPhone 15 Pro", "厦门", ["256"]
            )
            assert region_steps == ["区域", "福建", "厦门", "查看"]
            assert is_candidate({"title": "iPhone 15 Pro 256G 国行", "price": 4100, "display_region": "思明区"}, True, "256")
            assert is_candidate({"title": "iPhone 15 Pro 国行 自用", "price": 4100, "display_region": ""}, True, "256")
            assert not is_candidate({"title": "iPhone 15 Pro 512G 国行", "price": 4200, "display_region": "思明区"}, True, "256")
            assert not is_candidate({"title": "iPhone 15 Pro 256G 国行", "price": 5000, "display_region": "思明区"}, True, "256")
            item = {"item_id": "1091531543430", "title": "iPhone 15 Pro 256G 国行",
                    "price": 4100, "display_region": "思明区", "source_storage": "256"}
            return CollectResult(items=[item], region_applied=True, filter_applied=True,
                                 passes=[{"label": "新发", "storage": "256", "filter_applied": True,
                                          "observed": 1, "cards": 7, "candidates": 1, "error": None}])

    service.iphone_collector = FakePhone()
    result = await service.run_model("15 Pro", "256")
    assert result["matched"] == 1 and notifier.sent[0][0] == "1091531543430"
    assert result["passes"][0]["device_name"] == "测试 iPhone"
    assert result["passes"][0]["cards"] == 7


@pytest.mark.parametrize("price, rejected", [(1350, True), (1200, False)])
async def test_detail_gate_prices_repairs_before_links_and_preserves_pending(database, price, rejected):
    from app_monitor.quotes import QuoteDeduction, _save_deductions
    service, notifier = setup(database, region_city="厦门")
    batch = add_quotes(database, [("12 Pro", "128", "靓机", 1500), ("12 Pro", "128", "小花", 1300)])
    with database.connect() as connection:
        _save_deductions(connection, batch, "100025", [QuoteDeduction("12 Pro", 0, 0, "弹电-300", "弹电", 300, 300)])
    database.state_set("smart_config", {"enabled": True, "ai_enabled": True})
    raw = {"title": "苹果12Pro原装128G国行双卡双待", "price": price, "display_region": "集美区"}
    detail = {**raw, "description": "新原装电芯100健康，功能正常，无拆修", "detail_parameters": []}

    class Phone:
        async def collect(self, *args, check_detail=None, on_items=None, **kwargs):
            decision = check_detail(detail, True, "128")
            assert (decision["code"] == "price_high") == rejected
            if rejected:
                assert decision["code"] == "price_high" and decision["quote_basis"] == "detail"
                assert decision["quote_price"] is None and decision["threshold_price"] == 1200
                on_items([], cards=[{**raw, "detail_rejection": decision}], storage="128",
                         region_applied=True, filter_applied=True)
            return CollectResult(items=[], region_applied=True, filter_applied=True,
                                 passes=[{"storage": "128", "filter_applied": True, "error": None}])

    service.iphone_collector = Phone()
    run = await service.run_model("12 Pro", "128")
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM run_items WHERE run_id=?", (run["run_id"],)).fetchall()
        assert connection.execute("SELECT COUNT(*) FROM smart_assessments").fetchone()[0] == 0
    assert notifier.sent == [] and len(rows) == int(rejected)
    if rejected:
        assert rows[0]["code"] == "price_high" and rows[0]["assessment_id"] is None
        assert "扣¥300" in rows[0]["decision"] and "未取得商品链接" not in rows[0]["decision"]
        assert "参考上限 ¥1000" in rows[0]["decision"] and rows[0]["item_url"] == ""
        saved = json.loads(rows[0]["valuation_json"])
        assert saved["reference_grade"] == "小花" and saved["grade"] is None
        assert saved["value_low"] is None and saved["value_high"] == 1000
        assert saved["base_price"] == 1300 and saved["deductions"][0]["min"] == 300


async def test_detail_gate_failure_keeps_original_collection_path(database, monkeypatch):
    service, _ = setup(database)
    database.state_set("smart_config", {"enabled": True})

    def fail(*args, **kwargs):
        raise ValueError("invalid quote")

    monkeypatch.setattr("app_monitor.service.assess_local", fail)

    class Phone:
        async def collect(self, *args, check_detail=None, **kwargs):
            assert check_detail({"title": "iPhone 15 Pro 256G", "price": 3500}, True, "256") is None
            return CollectResult(items=[], passes=[{"error": None}])

    service.iphone_collector = Phone()
    await service._collect_sources("iPhone 15 Pro", "15 Pro", ["256"], load_rules(database))


@pytest.mark.parametrize("case, expected, text", [
    ("unread", "detail_failed", "详情读取失败"),
    ("unpriced", "verification_incomplete", "核验未完成"),
    ("matched", "link_failed", "详情判价符合"),
    ("pending", "link_failed", "详情判价待确认"),
    ("rejected", "smart_rejected", "扣费后价格不符合"),
])
async def test_run_records_distinguish_detail_failure_link_failure_and_rejection(database, case, expected, text):
    from dataclasses import asdict
    from app_monitor.matching import MatchDecision
    service, notifier = setup(database)
    raw = {"title": "iPhone 15 Pro 256G 实际详情", "price": 3500, "display_region": "厦门"}
    decision = asdict(MatchDecision(case == "matched", "扣费后价格不符合" if case == "rejected" else "详情判价",
                                   model="15 Pro", storage="256", quote_price=4000,
                                   quote_basis="detail", code="smart_rejected" if case == "rejected" else case))

    class Phone:
        async def collect(self, *args, on_items=None, **kwargs):
            card = {**raw, "link_error": "本次未取得链接", "acquisition_stage": "detail" if case == "unread" else "link"}
            if case in {"matched", "pending"}:
                card["detail_decision"] = decision
            elif case == "rejected":
                card["detail_rejection"] = decision
            on_items([], cards=[card], storage="256", region_applied=True, filter_applied=True)
            return CollectResult(items=[], passes=[{"storage": "256", "error": None}])

    service.iphone_collector = Phone()
    run = await service.run_model("15 Pro", "256")
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM run_items WHERE run_id=?", (run["run_id"],)).fetchone()
    assert row["code"] == expected and text in row["decision"]
    assert row["matched"] == 0 and row["item_url"] == "" and notifier.sent == []


@pytest.mark.parametrize("description, price, code", [
    ("iPhone 15 Pro 256G 国行，99新无拆修", 4100, "matched"),
    ("iPhone 15 Pro 256G 国行，99新无拆修", 5100, "price_high"),
    ("iPhone 15 Pro Max 256G 国行，99新无拆修", 3500, "other_model"),
    ("iPhone 15 Pro 512G 国行，99新无拆修", 3500, "storage_mismatch"),
])
async def test_actual_detail_drives_price_model_and_capacity_decision(database, description, price, code):
    service, _ = setup(database)
    database.state_set("smart_config", {"enabled": True})
    add_quotes(database, [("15 Pro", "256", "靓机", 4500), ("15 Pro", "256", "小花", 4000)])

    class Phone:
        async def collect(self, *args, check_detail=None, **kwargs):
            result = check_detail({"title": description, "description": description, "price": price,
                                   "display_region": "厦门"}, True, "256")
            assert result["code"] == code
            return CollectResult(items=[], passes=[{"error": None}])

    service.iphone_collector = Phone()
    await service._collect_sources("iPhone 15 Pro", "15 Pro", ["256"], load_rules(database))


async def test_iphone_filter_failure_keeps_actions_in_failed_run(database, monkeypatch):
    import app_monitor.service as service_module

    service, _ = setup(database, FakeCollector(), region_city="厦门")
    database.state_set("collector_devices", ["ios:phone-1"])
    monkeypatch.setattr(service_module, "discover_devices", lambda: [
        Device("ios:phone-1", "测试 iPhone", "ios", True, "phone-1", "27.2"),
    ])

    class FailedPhone:
        async def collect(self, *args, **kwargs):
            return CollectResult(items=[], region_error="地区未确认", passes=[{
                "label": "新发", "storage": None, "observed": 0,
                "error": "地区未确认", "actions": [
                    {"action": "搜索", "target": "iPhone 15 Pro", "success": True},
                    {"action": "地区筛选", "target": "厦门", "success": False, "error": "地区未确认"},
                ],
            }])

    service.iphone_collector = FailedPhone()
    with pytest.raises(RuntimeError, match="地区未确认"):
        await service.run_model("15 Pro", "256")
    with database.connect() as connection:
        run = connection.execute("SELECT ok, passes FROM search_runs ORDER BY id DESC").fetchone()
    assert run["ok"] == 0
    saved = json.loads(run["passes"])
    assert saved[0]["device_name"] == "测试 iPhone"
    assert saved[0]["actions"][1]["action"] == "地区筛选"


async def test_title_without_storage_is_rejected_when_filter_failed(database):
    items = [{"item_id": "d", "title": "iPhone 15 Pro 原色 自用", "price": 3400}]
    service, notifier = setup(database, FakeCollector(items, filter_ok=False))
    with pytest.raises(RuntimeError, match="筛选失败"):
        await service.run_model("15 Pro", "256")
    assert notifier.sent == []


async def test_scheduler_walks_each_model_once_with_all_capacities(database):
    service, _ = setup(database, model_interval_minutes=30)
    now = datetime.now().astimezone()
    state = service.schedule_state(now)
    assert state["active"] and state["units"] == 2 and state["targets"] == 2

    seen = []
    while (state := service.schedule_state(now))["next_model"]:
        seen.append((state["next_model"], state["next_storage"]))
        await service.run_model(state["next_model"], trigger="schedule")
    assert sorted(seen) == [("15 Pro", None), ("16", None)]
    later = datetime.now().astimezone() + timedelta(minutes=31)
    assert service.schedule_state(later)["next_model"] is not None

    save_rules(database, Rules(search_start="09:00", search_end="23:00"))
    assert service.schedule_state(now.replace(hour=23, minute=30))["active"] is False


async def test_capacity_and_model_can_be_disabled(database):
    service, _ = setup(database, disabled_models=["15 Pro|512", "16"])
    assert service.schedule_state()["units"] == 1
    save_rules(database, Rules(disabled_models=["15 Pro|256", "15 Pro|512", "16"], search_start="00:00", search_end="00:00"))
    assert service.schedule_state()["units"] == 0


async def test_disabled_rules_and_repeated_failures_pause_searching(database):
    service, _ = setup(database, collector=FakeCollector(error="闲鱼 App 未运行"))
    now = datetime.now().astimezone()
    for _ in range(3):
        try:
            await service.run_model("16", "128")
        except RuntimeError:
            pass
    state = service.schedule_state(now)
    assert state["active"] is False and "连续" in state["reason"]

    save_rules(database, Rules(enabled=False))
    assert service.schedule_state(now)["reason"] == "监控已暂停"


async def test_run_due_runs_one_unit(database):
    service, _ = setup(database)
    result = await service.run_due(datetime.now().astimezone())
    assert result and result["storage"] is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0] == 1


async def test_retry_failed_search_runs_immediately_and_restarts_failure_streak(database):
    collector = FakeCollector(error="连接失败")
    service, _ = setup(database, collector=collector, disabled_models=["15 Pro"])
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await service.run_model("16")
    assert service.schedule_state()["can_retry"]
    assert service.schedule_state()["next_model"] is None  # Normal interval has not elapsed.
    # A failed retry counts as the first new failure, preserving all history.
    with pytest.raises(RuntimeError):
        await service.retry_failed()
    assert not service.schedule_state()["can_retry"]
    assert len(collector.calls) == 4
    replacement = MonitorService(database, None, None)
    assert not replacement.schedule_state()["can_retry"]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await service.run_model("16")
    assert service.schedule_state()["can_retry"]
    collector.error = None
    result = await service.retry_failed()
    assert result["model"] == "16"
    assert not service.schedule_state()["can_retry"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0] == 7
        assert connection.execute("SELECT trigger FROM search_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "manual"
    with pytest.raises(ValueError):
        await service.retry_failed()


@pytest.mark.parametrize("blocked", ["busy", "disabled", "outside_window", "no_devices", "no_units"])
async def test_retry_does_not_override_other_search_constraints(database, blocked):
    service, _ = setup(database, collector=FakeCollector(error="连接失败"))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await service.run_model("16")
    rules = load_rules(database)
    if blocked == "busy":
        await service._lock.acquire()
    elif blocked == "disabled":
        rules.enabled = False
    elif blocked == "outside_window":
        # Choose a one-minute window starting two hours from now.
        start = datetime.now().astimezone() + timedelta(hours=2)
        rules.search_start = start.strftime("%H:%M")
        rules.search_end = (start + timedelta(minutes=1)).strftime("%H:%M")
    elif blocked == "no_devices":
        database.state_set("collector_devices", [])
    else:
        rules.disabled_models = ["15 Pro", "16"]
    save_rules(database, rules)
    try:
        with pytest.raises(ValueError):
            await service.retry_failed()
        assert database.state_get("failure_pause_reset_after", 0) == 0
    finally:
        if blocked == "busy":
            service._lock.release()


async def test_developer_mode_intervention_stays_local_without_push(database, monkeypatch):
    service, notifier = setup(database)
    monkeypatch.setattr("app_monitor.service.discover_devices", lambda: [
        Device("ios:test-phone", "测试手机", "ios", False, "test-phone", "27.0", "开发者模式未开启")])
    for _ in range(2):
        with pytest.raises(RuntimeError, match="开发者模式"):
            await service.run_model("16")
    assert notifier.sent == []
    assert "开发者模式" in database.state_get(service.wda_maintenance._key("test-phone"))["action"]


async def test_stop_interrupts_phone_commands_and_preserves_cleanup(database, monkeypatch):
    import asyncio
    from threading import Event
    from fastapi import HTTPException
    from app_monitor import api
    from app_monitor.stop_control import guard_driver, STOP_REASON

    service, notifier = setup(database)
    monkeypatch.setattr(api, "database", database)
    monkeypatch.setattr(api, "monitor_service", service)
    ready, release = Event(), Event()
    commands = []

    class Driver:
        def execute(self, command, params=None):
            commands.append(command)
            return {}

        def quit(self):
            return self.execute("quit")

    class Phone:
        async def collect(self, *args, **kwargs):
            def worker():
                driver = guard_driver(Driver())
                try:
                    driver.execute("read")
                    ready.set()
                    assert release.wait(3)
                    driver.execute("click")
                finally:
                    driver.quit()
            return await asyncio.to_thread(worker)

    service.iphone_collector = Phone()
    task = asyncio.create_task(service.run_model("16"))
    try:
        assert await asyncio.to_thread(ready.wait, 3)
        response = await api.stop_monitor()
        assert response["stopping"] and not response["rules"]["enabled"]
        assert service.busy
        with pytest.raises(HTTPException) as error:
            await api.start_monitor()
        assert error.value.status_code == 409
    finally:
        release.set()
    result = await asyncio.wait_for(task, 3)
    assert result["stopped"] and commands == ["read", "quit"]
    assert not service.busy and not service.stopping
    assert notifier.sent == []
    with database.connect() as c:
        row = c.execute("SELECT * FROM search_runs WHERE id=?", (result["run_id"],)).fetchone()
    assert row["finished_at"] and row["error"] == STOP_REASON
    assert not service._failure_paused(datetime.now().astimezone())
    assert await service.run_due() is None
    assert (await api.start_monitor())["rules"]["enabled"]


async def test_stop_discards_late_streamed_and_collected_products(database, monkeypatch):
    import asyncio
    service, notifier = setup(database)
    ready, release = asyncio.Event(), asyncio.Event()

    class Phone:
        async def collect(self, *args, on_items=None, **kwargs):
            ready.set()
            await release.wait()
            item = {"item_id": "1090000000111", "title": "iPhone 16 128G 国行", "price": 2500}
            on_items([item], storage="128", filter_applied=True)
            return CollectResult(items=[item], passes=[{"storage": "128", "observed": 1, "error": None}])

    service.iphone_collector = Phone()
    task = asyncio.create_task(service.run_model("16"))
    await asyncio.wait_for(ready.wait(), 3)
    service.request_stop()
    release.set()
    result = await asyncio.wait_for(task, 3)
    assert result["stopped"] and notifier.sent == []
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 0
    # A subsequent run has a fresh stop context.
    again = await service.run_model("16", injected_items=[])
    assert not again.get("stopped")


async def test_push_includes_full_quote_reference(database):
    service, notifier = setup(database)
    await service.process_items(
        [{"item_id": "r1", "title": "iPhone 15 Pro 256G 国行", "price": 3900}], load_rules(database), "iPhone 15 Pro"
    )
    reference = notifier.payloads[0]["quote_reference"]
    assert reference["grade"] == "小花" and reference["grades"] == [{"grade": "小花", "price": 4000}]
    assert reference["deductions"] == [] and reference["category_name"] == "苹果无保"


async def test_items_are_judged_and_pushed_while_collection_is_still_running(database):
    import asyncio

    seen_during_collection = {}

    class StreamingCollector(FakeCollector):
        async def collect(self, keyword, region_steps=None, region_hint="", storage_filters=None,
                          scroll_pages=0, on_items=None):
            first = {"item_id": "s1", "title": "iPhone 15 Pro 256G 国行", "price": 3500,
                     "display_region": "厦门", "source_storage": "256", "source_pass": "新发"}
            on_items([dict(first)], storage="256", region_applied=True, filter_applied=True, stage="读取 256G 第 1/1 屏")
            await asyncio.sleep(0.05)  # 采集还没结束：第一件应已判价、写入记录并推送
            seen_during_collection["pushed"] = [item_id for item_id, _ in notifier.sent]
            with database.connect() as connection:
                seen_during_collection["run_items"] = connection.execute("SELECT COUNT(*) FROM run_items").fetchone()[0]
            seen_during_collection["live"] = dict(service.live)
            on_items([], pass_info={"label": "新发", "storage": "256", "filter_applied": True,
                                    "observed": 1, "error": None}, storage="256")
            await asyncio.sleep(0.05)
            with database.connect() as connection:
                seen_during_collection["passes"] = json.loads(
                    connection.execute("SELECT passes FROM search_runs ORDER BY id DESC").fetchone()[0])
            return CollectResult(items=[first], region_applied=True, filter_applied=True,
                                 passes=[{"label": "新发", "storage": "256", "filter_applied": True,
                                          "observed": 1, "error": None}])

    service, notifier = setup(database, StreamingCollector(), region_city="厦门", disabled_models=["15 Pro|512"])
    result = await service.run_model("15 Pro")

    assert seen_during_collection["pushed"] == ["s1"]
    assert seen_during_collection["run_items"] == 1
    assert seen_during_collection["live"]["stage"] == "测试 iPhone · 读取 256G 第 1/1 屏"
    assert seen_during_collection["live"]["matched"] == 1
    assert seen_during_collection["passes"][0]["storage"] == "256"
    assert len(notifier.sent) == 1  # 采集结束后不会重复处理同一商品
    assert result["matched"] == 1 and result["passes"][0]["matched"] == 1
    assert service.live is None


async def test_item_stream_accepts_items_from_iphone_thread():
    import asyncio
    import threading

    from app_monitor.service import ItemStream

    received = []

    async def handler(items, meta):
        received.append((items[0]["item_id"], meta["device"], meta["storage"]))

    stream = ItemStream(handler)
    emit = stream.for_device("ios:abc", "测试 iPhone")
    worker = threading.Thread(target=lambda: [emit([{"item_id": str(i)}], storage="256") for i in range(3)])
    worker.start()
    await asyncio.to_thread(worker.join)
    await stream.close()
    assert received == [("0", "ios:abc", "256"), ("1", "ios:abc", "256"), ("2", "ios:abc", "256")]


async def test_cards_without_links_are_recorded_but_never_pushed(database):
    class CardCollector(FakeCollector):
        async def collect(self, keyword, region_steps=None, region_hint="", storage_filters=None,
                          scroll_pages=0, on_items=None):
            meta = {"storage": "256", "region_applied": True, "filter_applied": True}
            on_items([], cards=[
                {"title": "iPhone 15 Pro 256G 国行", "price": 5200, "display_region": "厦门"},  # 价格偏高
                {"title": "iPhone 15 Pro 256G 国行 自用", "price": 3600, "display_region": "厦门",
                 "link_error": "分享面板没有出现"},  # 价格符合但没取到链接
            ], **meta)
            hit = {"item_id": "h1", "title": "iPhone 15 Pro 256G 国行 电池90", "price": 3500,
                   "display_region": "厦门", "source_storage": "256", "source_pass": "新发"}
            on_items([dict(hit)], **meta)
            return CollectResult(items=[hit], region_applied=True, filter_applied=True,
                                 passes=[{"label": "新发", "storage": "256", "filter_applied": True,
                                          "observed": 1, "cards": 3, "error": None}])

    service, notifier = setup(database, CardCollector(), region_city="厦门", disabled_models=["15 Pro|512"])
    await service.run_model("15 Pro")

    with database.connect() as connection:
        rows = connection.execute("SELECT item_id, item_url, matched, code, decision FROM run_items ORDER BY position").fetchall()
    assert [(row["matched"], row["code"]) for row in rows] == [(0, "price_high"), (0, "no_link"), (1, "matched")]
    assert rows[0]["item_id"].startswith("card:") and rows[0]["item_url"] == ""
    assert "分享面板没有出现" in rows[1]["decision"]
    assert [item_id for item_id, _ in notifier.sent] == ["h1"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observed_items WHERE item_id LIKE 'card:%'").fetchone()[0] == 0


def test_interrupted_runs_are_closed_on_startup(database):
    service, _ = setup(database)
    with database.connect() as connection:
        connection.execute("INSERT INTO search_runs(model, keyword, trigger, started_at) VALUES ('15 Pro', 'iPhone 15 Pro', 'schedule', '2026-10-21T05:00:00+00:00')")
    assert service.close_interrupted_runs() == 1
    with database.connect() as connection:
        row = connection.execute("SELECT ok, error FROM search_runs").fetchone()
    assert row["ok"] == 0 and "中断" in row["error"]


@pytest.mark.parametrize("shop", [True, False, None])
async def test_seller_metadata_survives_processing_and_persistence(database, shop):
    service, notifier = setup(database, region_city="厦门")
    item = {"item_id": "1090413599999", "title": "iPhone 15 Pro 256G 国行", "price": 3500,
            "display_region": "厦门", "seller_name": " 测试卖家 ", "is_fish_shop": shop}
    await service.process_items([item], load_rules(database), "iPhone 15 Pro")
    assert notifier.payloads[0]["seller_name"] == "测试卖家"
    assert notifier.payloads[0]["is_fish_shop"] is shop
    # A later list-only observation must not erase the detail metadata.
    await service.process_items([{k: v for k, v in item.items() if k not in {"seller_name", "is_fish_shop"}}],
                                load_rules(database), "iPhone 15 Pro")
    with database.connect() as connection:
        saved = connection.execute("SELECT seller_name, is_fish_shop FROM observed_items WHERE item_id=?",
                                   (item["item_id"],)).fetchone()
    assert saved["seller_name"] == "测试卖家"
    assert saved["is_fish_shop"] == shop


def test_run_quote_snapshot_keeps_initial_grade_after_rules_change(database):
    service, notifier = setup(database, region_city="厦门")
    add_quotes(database, [("15 Pro", "256", "靓机", 4500), ("15 Pro", "256", "小花", 4000)])
    database.state_set("smart_config", {"enabled": True})
    with database.connect() as connection:
        run_id = connection.execute("INSERT INTO search_runs(model, keyword, started_at) VALUES ('15 Pro', 'iPhone 15 Pro', '2026-10-23')").lastrowid
    service._record_cards(run_id, [{"title": "iPhone 15 Pro 256G 轻微划痕", "price": 3900,
                                   "display_region": "厦门", "link_error": "复制失败"}],
                          load_rules(database), {"storage": "256", "filter_applied": True, "region_applied": True}, "15 Pro")
    database.state_set("smart_config", {"enabled": True, "accepted_grades": ["小花"]})
    with database.connect() as connection:
        row = dict(connection.execute("SELECT * FROM run_items WHERE run_id=?", (run_id,)).fetchone())
    assert row["quote_grade"] == "靓机" and row["quote_basis"] == "candidate"
    assert row["quote_price"] == 4500 and row["assessment_id"] is None and row["code"] == "no_link"
    assert notifier.sent == []


def test_detail_quote_keeps_assessed_grade_and_adjusted_price(database):
    service, _ = setup(database)
    decision = service.smart_decision({"snapshot": {"price": 3600}, "result": {
        "status": "matched", "reason": "电池扣费后满足条件", "model": "15 Pro", "storage": "256",
        "grade": "小花", "base_price": 4000, "value_low": 3700, "threshold_low": 3900,
    }})
    assert decision.quote_grade == "小花" and decision.quote_basis == "detail"
    assert decision.quote_price == 3700 and decision.threshold_price == 3900


async def test_per_device_proxy_selection_routes_only_future_runs(database):
    service, _ = setup(database)
    calls = []
    class Phone:
        def __init__(self, name):
            self.name = name
        async def collect(self, *args, **kwargs):
            calls.append((self.name, kwargs.get("client_ip")))
            return CollectResult(items=[], passes=[{"label": "新发", "storage": "256", "filter_applied": True}])
    service.iphone_collector = Phone("ui")
    service.iphone_proxy_collector = Phone("proxy")
    service.proxy.configure = lambda clients: None
    rules = load_rules(database)
    await service._collect_sources("iPhone 15 Pro", "15 Pro", ["256"], rules)
    database.state_set("collector_device_channels", {"ios:test-phone": {"channel": "proxy", "client_ip": "192.168.1.20"}})
    await service._collect_sources("iPhone 15 Pro", "15 Pro", ["256"], rules)
    database.state_set("collector_device_channels", {"ios:test-phone": {"channel": "ui", "client_ip": "192.168.1.20"}})
    await service._collect_sources("iPhone 15 Pro", "15 Pro", ["256"], rules)
    assert calls == [("ui", None), ("proxy", "192.168.1.20"), ("ui", None)]


def test_proxy_rejected_cards_keep_real_identity_and_clickable_link(database):
    from app_monitor.db import utc_now
    service, _ = setup(database)
    with database.connect() as connection:
        run_id = connection.execute("INSERT INTO search_runs(model,keyword,started_at) VALUES('15 Pro','iPhone 15 Pro',?)", (utc_now(),)).lastrowid
    key = "1093306353441"
    service._record_cards(run_id, [{"item_id": key, "title": "iPhone 15 Pro 256G", "price": 9000,
                                   "display_region": "厦门"}], load_rules(database),
                          {"storage": "256", "filter_applied": True}, "15 Pro")
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM run_items WHERE run_id=?", (run_id,)).fetchone()
    assert row["item_id"] == key and row["item_url"] == f"https://www.goofish.com/item?id={key}"


async def test_mid_run_switch_waits_until_next_search_for_other_devices(database, monkeypatch):
    service, _ = setup(database)
    database.state_set("collector_devices", ["ios:one", "ios:two"])
    monkeypatch.setattr("app_monitor.service.discover_devices", lambda: [
        Device("ios:one", "One", "ios", True, "one"), Device("ios:two", "Two", "ios", True, "two")])
    calls = []
    class Adapter:
        def __init__(self, name):
            self.name = name
        async def collect(self, udid, *args, **kwargs):
            calls.append((self.name, udid))
            if udid == "one":
                database.state_set("collector_device_channels", {"ios:two": {"channel": "proxy", "client_ip": "192.168.1.20"}})
            return CollectResult(items=[], passes=[{"label": "新发", "filter_applied": True}])
    service.iphone_collector, service.iphone_proxy_collector = Adapter("ui"), Adapter("proxy")
    for _ in range(2):
        await service._collect_sources("iPhone 15 Pro", "15 Pro", ["256"], load_rules(database))
    assert calls == [("ui", "one"), ("ui", "two"), ("ui", "one"), ("proxy", "two")]


async def test_record_only_also_suppresses_fixed_grade_push_without_consuming_dedup(database):
    service,notifier=setup(database,quote_grade='小花',region_city='厦门')
    rules=load_rules(database)
    database.state_set('smart_config',{'enabled':False,'shadow':True})
    item={'item_id':'1090000000666','title':'iPhone 15 Pro 256G 国行','price':3500,
          'display_region':'厦门','item_url':'https://www.goofish.com/item?id=1090000000666'}
    result=await service.process_items([item],rules,'iPhone 15 Pro',True,'256',expected_model='15 Pro')
    assert result['notified']==0 and result['decisions'][0]['matched']
    assert not notifier.sent
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM observed_items WHERE item_id=?',(item['item_id'],)).fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM deals').fetchone()[0]==0
    database.state_set('smart_config',{'enabled':False,'shadow':False})
    result=await service.process_items([item],rules,'iPhone 15 Pro',True,'256',expected_model='15 Pro')
    assert result['notified']==1 and len(notifier.sent)==1
    result=await service.process_items([item],rules,'iPhone 15 Pro',True,'256',expected_model='15 Pro')
    assert result['notified']==0 and len(notifier.sent)==1


@pytest.mark.parametrize('description,blocked',[
    ('屏幕有水印',True),('没有水印',False),('屏幕可能有水印',False),
])
async def test_problem_exclusions_remain_active_with_grading_switch_off(database,description,blocked):
    service,notifier=setup(database,quote_grade='小花',region_city='厦门')
    item={'item_id':'1090000000667','title':'iPhone 15 Pro 256G 国行','description':description,
          'price':3500,'display_region':'厦门','item_url':'https://www.goofish.com/item?id=1090000000667'}
    rules=load_rules(database)
    result=await service.process_items([item],rules,'iPhone 15 Pro',True,'256',expected_model='15 Pro')
    assert result['notified']==int(not blocked)
    assert result['decisions'][0]['code']==('excluded' if blocked else 'matched')
