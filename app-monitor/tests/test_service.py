from datetime import datetime, timedelta

from app_monitor.collector import CollectResult
from app_monitor.rules import Rules, save_rules
from app_monitor.service import MonitorService
from conftest import add_quotes


class FakeCollector:
    def __init__(self, items=None, error=None, filter_ok=True):
        self.items, self.error, self.filter_ok, self.calls = items or [], error, filter_ok, []

    async def collect(self, keyword, region_steps=None, region_hint="", storage_filters=None, scroll_pages=0):
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
    return MonitorService(database, collector or FakeCollector(), notifier), notifier


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


async def test_title_without_storage_is_rejected_when_filter_failed(database):
    items = [{"item_id": "d", "title": "iPhone 15 Pro 原色 自用", "price": 3400}]
    service, notifier = setup(database, FakeCollector(items, filter_ok=False))
    result = await service.run_model("15 Pro", "256")
    assert result["matched"] == 0 and result["decisions"] == []


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
