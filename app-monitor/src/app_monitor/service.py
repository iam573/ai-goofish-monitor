"""监控主流程：按全局规则把到期机型分给空闲 iPhone 并行搜索，判价、去重、推送。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from threading import Event
from typing import Any

from .collector import CollectResult
from .db import Database, utc_now
from .config import settings as default_settings
from .smart import SmartService, assess_local, candidate_decision, load_config, policies_for, scope_decision
from .devices import MAC_DEVICE, discover_devices, selected_devices, device_channel, proxy_clients
from .iphone_collector import IPhoneCollector, _brief_error
from .item_identity import has_model_evidence, item_text
from .proxy_capture import ProxyCapture
from .iphone_proxy_collector import IPhoneProxyCollector
from .matching import MatchDecision, evaluate
from .notifications import NotificationService, notification_result_text
from .wda_maintenance import WdaMaintenance
from .stop_control import MonitorStopped, STOP_REASON, checkpoint, stop_scope
from .quotes import quote_reference, latest_entries
from .rules import (
    Rules,
    eligible_quotes,
    in_search_window,
    load_rules,
    search_keyword,
    search_targets,
    search_units,
)

logger = logging.getLogger(__name__)


class ItemStream:
    """边搜边判：采集器每读到新商品就放进队列，后台任务立刻判价、写入运行记录并推送。

    ``emit`` 可以在事件循环里调用，也可以从 iPhone 采集线程里调用，只入队，不阻塞采集。
    """

    def __init__(self, handler: Any):
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.handler = handler
        self.task = asyncio.create_task(self._run())

    def emit(self, items: list[dict[str, Any]], **meta: Any) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (items, meta))

    def for_device(self, device_id: str, device_name: str) -> Any:
        return lambda items, **meta: self.emit(items, device=device_id, device_name=device_name, **meta)

    async def _run(self) -> None:
        while True:
            entry = await self.queue.get()
            if entry is None:
                return
            items, meta = entry
            try:
                await self.handler(items, meta)
            except MonitorStopped:
                return
            except Exception:
                logger.exception("实时处理商品失败 count=%d", len(items))

    async def close(self) -> None:
        """等队列里已经交出的商品全部处理完。"""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        await self.task


class CollectionFailure(RuntimeError):
    def __init__(self, message: str, partial: CollectResult):
        super().__init__(message)
        self.partial = partial

def unit_key(model: str, storage: str | None) -> str:
    return f"{model}|{storage or ''}"


def unit_label(model: str, storage: str | None) -> str:
    if not storage:
        return model
    return f"{model} {storage if storage.endswith('T') else storage + 'G'}"


# 同一台设备连续失败达到该次数后暂停一段时间（通常是 App 未运行、未登录或 WDA 不可用）。
FAILURE_PAUSE_AFTER = 3
FAILURE_PAUSE = timedelta(minutes=10)

_current_live: ContextVar[dict[str, Any] | None] = ContextVar("monitor_live_run", default=None)


class MonitorBusy(ValueError):
    """设备或机型已被其他搜索占用。"""


class MonitorService:
    def __init__(
        self,
        database: Database,
        collector: Any,
        notifications: NotificationService,
    ):
        self.database = database
        self.collector = collector
        self.settings = getattr(collector, "settings", default_settings)
        self.iphone_collector = IPhoneCollector(self.settings)
        self.proxy = ProxyCapture(self.settings)
        self.iphone_proxy_collector = IPhoneProxyCollector(self.settings, self.proxy)
        self.wda_maintenance = WdaMaintenance(self.settings, database)
        self.iphone_collector.wda_maintenance = self.wda_maintenance
        self.iphone_proxy_collector.wda_maintenance = self.wda_maintenance
        self.smart = SmartService(database, self.settings)
        self.notifications = notifications
        # 持有时不再开始新的搜索，用于需要独占的维护操作（如重新分析）。
        self._lock = asyncio.Lock()
        # 每台设备、每款机型同一时间只跑一个搜索；不同设备各搜各的机型。
        self._device_runs: dict[str, str] = {}
        self._model_runs: set[str] = set()
        # 正在进行的搜索：run_id → 设备、当前步骤、已看到 / 命中件数，供后台实时显示
        self._live: dict[int, dict[str, Any]] = {}
        self._stop_events: set[Event] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def busy(self) -> bool:
        return self._lock.locked() or bool(self._model_runs)

    @property
    def stopping(self) -> bool:
        return any(event.is_set() for event in self._stop_events)

    @property
    def live(self) -> list[dict[str, Any]]:
        return [dict(self._live[run_id]) for run_id in sorted(self._live)]

    def live_for(self, run_id: int | None) -> dict[str, Any] | None:
        value = self._live.get(run_id) if run_id is not None else None
        return dict(value) if value else None

    def request_stop(self) -> None:
        for event in self._stop_events:
            event.set()
        for value in self._live.values():
            value["stage"] = "正在终止，等待当前指令退出"

    # ------------------------------------------------------------------ 调度

    def schedule_state(self, now: datetime | None = None) -> dict[str, Any]:
        """当前是否应该搜索、为什么、下一个搜索单元是谁。供调度和后台展示共用。"""
        now = now or datetime.now().astimezone()
        rules = load_rules(self.database)
        units = search_units(self.database, rules)
        selected = selected_devices(self.database)
        paused = [device for device in selected if self._device_paused(device, now)]
        blocked = {device: attention for device in selected
                   if (attention := self.wda_maintenance.blocking_attention(device.removeprefix("ios:")))}
        state: dict[str, Any] = {
            "active": False, "reason": "", "next_model": None, "next_storage": None,
            "next_label": None, "targets": len({unit["model"] for unit in units}), "units": len(units),
            "can_retry": False, "ready_devices": [], "paused_devices": [], "attention_devices": [],
        }
        if not rules.enabled:
            state["reason"] = "监控已暂停"
        elif not selected:
            state["reason"] = "请选择已连接的 iPhone；Mac 采集已禁用"
        elif not units:
            state["reason"] = "没有可搜索的机型，请先同步报价"
        elif not in_search_window(rules, now):
            state["reason"] = f"不在搜索时段 {rules.search_start}–{rules.search_end}"
        else:
            state.update(paused_devices=paused, attention_devices=list(blocked), can_retry=bool(paused or blocked),
                         ready_devices=[device for device in selected if device not in paused and device not in blocked])
            if state["ready_devices"]:
                state["active"] = True
                state["reason"] = "搜索中"
            elif paused:
                state["reason"] = f"连续 {FAILURE_PAUSE_AFTER} 次搜索失败，暂停 10 分钟"
            else:
                state["reason"] = next(iter(blocked.values()))["action"]
        due = self._due_units(units, rules, now)
        if due:
            state.update(
                next_model=due[0]["model"], next_storage=due[0]["storage"],
                next_label=unit_label(due[0]["model"], due[0]["storage"]),
            )
        if state["active"] and not due:
            state["reason"] = "本轮已搜完，等待下一轮"
        return state

    async def run_due(self, now: datetime | None = None) -> list[asyncio.Task]:
        """调度器每 15 秒调用一次：给每台空闲且已连接的设备各派一个到期机型，各自在后台搜索。"""
        now = now or datetime.now().astimezone()
        state = self.schedule_state(now)
        idle = [device for device in state["ready_devices"] if device not in self._device_runs]
        if self._lock.locked() or not state["active"] or not state["next_model"] or not idle:
            return []
        found = {device.id: device for device in await asyncio.to_thread(discover_devices)}
        # 未连接的设备不领机型，否则失败的搜索会把机型推迟到下一轮。
        idle = [device for device in idle
                if device not in self._device_runs and found.get(device) and found[device].connected]
        rules = load_rules(self.database)
        units = self._due_units(search_units(self.database, rules), rules, now)
        tasks = []
        for device_id, unit in zip(idle, units):
            if self._lock.locked():
                break
            self._claim(unit["model"], device_id)
            task = asyncio.create_task(self._run_claimed_quietly(unit["model"], unit["storage"], device_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            tasks.append(task)
        return tasks

    async def _run_claimed_quietly(self, model: str, storage: str | None, device_id: str) -> dict[str, Any] | None:
        try:
            return await self._run_claimed(model, storage, "schedule", None, False, False, device_id)
        except Exception:
            return None  # 错误已写入 search_runs

    def _due_units(self, units: list[dict[str, Any]], rules: Rules, now: datetime) -> list[dict[str, Any]]:
        """到期且没有正在搜索的单元，最久没搜的排在前面。"""
        last = self.last_runs()
        interval = timedelta(minutes=max(1, rules.model_interval_minutes))
        due = []
        for unit in units:
            if unit["model"] in self._model_runs:
                continue
            started = last.get(unit_key(unit["model"], None), {}).get("started_at")
            started_at = datetime.fromisoformat(started) if started else None
            if started_at is None or now - started_at >= interval:
                due.append((started_at or datetime.min.replace(tzinfo=now.tzinfo), unit))
        return [unit for _, unit in sorted(due, key=lambda value: value[0])]

    def _device_paused(self, device_id: str, now: datetime) -> bool:
        reset_after = self.database.state_get("failure_pause_reset_after", 0)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT ok, started_at FROM search_runs WHERE device=? AND ok IS NOT NULL AND id > ? "
                "AND COALESCE(error, '') != ? ORDER BY id DESC LIMIT ?",
                (device_id, reset_after, STOP_REASON, FAILURE_PAUSE_AFTER),
            ).fetchall()
        if len(rows) < FAILURE_PAUSE_AFTER or any(row["ok"] for row in rows):
            return False
        return now - datetime.fromisoformat(rows[0]["started_at"]) < FAILURE_PAUSE

    async def retry_failed(self) -> dict[str, Any]:
        """Reset failure streaks and attention blocks, then immediately retry on an affected device."""
        if self._lock.locked():
            raise ValueError("正在执行其他操作，请稍后再试")
        state = self.schedule_state()
        if not state["can_retry"]:
            raise ValueError(f"当前无法重试：{state['reason']}")
        device_id = next((device for device in [*state["paused_devices"], *state["attention_devices"]]
                          if device not in self._device_runs), None)
        if device_id is None:
            raise ValueError("暂停的设备正在搜索，请稍后再试")
        units = [unit for unit in search_units(self.database, load_rules(self.database))
                 if unit["model"] not in self._model_runs]
        if not units:
            raise ValueError("可搜索的机型都在搜索中，请稍后再试")
        with self.database.connect() as connection:
            latest = connection.execute("SELECT id, model FROM search_runs WHERE device=? ORDER BY id DESC LIMIT 1",
                                        (device_id,)).fetchone()
        unit = next((unit for unit in units if latest and unit["model"] == latest["model"]), units[0])
        if latest:
            self.database.state_set("failure_pause_reset_after", latest["id"])
        if device_id in state["attention_devices"]:
            self.wda_maintenance.recovered(device_id.removeprefix("ios:"))
        return await self.run_model(unit["model"], None, "manual", device_id=device_id)

    def last_runs(self) -> dict[str, dict[str, Any]]:
        """每款机型最近一次搜索，容量结果保存在该次运行的 passes 中。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.* FROM search_runs r
                JOIN (
                    SELECT MAX(id) AS id FROM search_runs GROUP BY model
                ) latest ON latest.id=r.id
                """
            ).fetchall()
        return {unit_key(row["model"], None): self._run_dict(row) for row in rows}

    @staticmethod
    def _run_dict(row: Any) -> dict[str, Any]:
        value = dict(row)
        value["passes"] = json.loads(value.get("passes") or "[]")
        value["stopped"] = value.get("error") == STOP_REASON
        return value

    # ------------------------------------------------------------------ 执行

    async def run_model(
        self,
        model: str,
        storage: str | None = None,
        trigger: str = "manual",
        injected_items: list[dict[str, Any]] | None = None,
        region_applied: bool = False,
        filter_applied: bool = False,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """立即搜索一个机型；未指定设备时使用一台空闲的已选设备。调试注入不占用设备。"""
        if injected_items is None and device_id is None:
            device_id = await self._pick_device()
        self._claim(model, device_id)
        return await self._run_claimed(model, storage, trigger, injected_items, region_applied, filter_applied, device_id)

    async def _pick_device(self) -> str | None:
        selected = selected_devices(self.database)
        idle = [device for device in selected if device not in self._device_runs]
        if selected and not idle:
            raise MonitorBusy("所有采集设备都在搜索，请稍后再试")
        if len(idle) > 1:
            found = {device.id: device for device in await asyncio.to_thread(discover_devices)}
            idle.sort(key=lambda device: (bool(self.wda_maintenance.blocking_attention(device.removeprefix("ios:"))),
                                          not (found.get(device) and found[device].connected)))
        return idle[0] if idle else None

    def _claim(self, model: str, device_id: str | None) -> None:
        if self._lock.locked():
            raise MonitorBusy("正在执行其他操作，请稍后再试")
        if model in self._model_runs:
            raise MonitorBusy(f"iPhone {model} 正在搜索，请稍后再试")
        if device_id is not None and device_id in self._device_runs:
            raise MonitorBusy("这台设备正在搜索其他机型，请稍后再试")
        self._model_runs.add(model)
        if device_id is not None:
            self._device_runs[device_id] = model

    async def _run_claimed(self, model, storage, trigger, injected_items, region_applied, filter_applied, device_id):
        event = Event()
        self._stop_events.add(event)
        try:
            with self.smart.run_settings(), stop_scope(event):
                return await self._execute_model(model, storage, trigger, injected_items,
                                                 region_applied, filter_applied, device_id)
        except asyncio.CancelledError:
            event.set()
            raise
        finally:
            self._stop_events.discard(event)
            self._model_runs.discard(model)
            if device_id is not None:
                self._device_runs.pop(device_id, None)

    async def _execute_model(self, model, storage, trigger, injected_items, region_applied, filter_applied, device_id=None):
        rules = load_rules(self.database)
        keyword = search_keyword(model)
        with self.database.connect() as connection:
            run_id = connection.execute(
                """
                INSERT INTO search_runs(model, storage, keyword, region, trigger, started_at, device)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (model, storage, keyword, rules.region_city or None, trigger, utc_now(), device_id),
            ).lastrowid
        logger.info(
            "监控任务开始 run_id=%s trigger=%s model=%s storage=%s keyword=%s region=%s device=%s",
            run_id, trigger, model, storage or "全部容量", keyword, rules.region_city or "未指定", device_id or "无",
        )
        live = {"run_id": run_id, "model": model, "device": device_id, "stage": "准备搜索", "observed": 0, "matched": 0}
        self._live[run_id] = live
        live_token = _current_live.set(live)
        try:
            results: list[dict[str, Any]] = []
            processed: set[str] = set()
            pass_stats: dict[tuple[str, str | None], dict[str, int]] = {}
            live_passes: list[dict[str, Any]] = []

            async def handle(items: list[dict[str, Any]], meta: dict[str, Any]) -> None:
                """实时处理采集器交出的商品：写入运行记录、判价、命中就立即推送。"""
                checkpoint()
                if meta.get("stage"):
                    live["stage"] = (
                        f"{meta['device_name']} · {meta['stage']}"
                        if meta.get("device_name") and meta.get("device") != MAC_DEVICE else meta["stage"]
                    )
                if meta.get("device_name"):
                    live["device_name"] = meta["device_name"]
                if meta.get("pass_info") is not None:
                    live_passes.append({**meta["pass_info"], "device": meta.get("device", MAC_DEVICE),
                                        "device_name": meta.get("device_name")})
                    self._save_live_passes(run_id, live_passes)
                if meta.get("cards"):
                    recorded = self._record_cards(run_id, meta["cards"], rules, meta, model)
                    live["observed"] += recorded
                fresh = [item for item in items if item.get("item_id") and item["item_id"] not in processed]
                if not fresh:
                    return
                processed.update(item["item_id"] for item in fresh)
                storage_value = meta.get("storage")
                part = await self.process_items(
                    fresh, rules, keyword, bool(meta.get("region_applied")),
                    storage_value if meta.get("filter_applied") else None,
                    run_id=run_id, expected_model=model,
                )
                stats = pass_stats.setdefault((meta.get("device", MAC_DEVICE), storage_value), {"matched": 0, "notified": 0})
                stats["matched"] += part["matched"]
                stats["notified"] += part["notified"]
                live["observed"] += part["observed"]
                live["matched"] += part["matched"]
                results.append(part)

            if injected_items is None:
                checkpoint()
                target = next(
                    (item for item in search_targets(self.database, rules) if item["model"] == model),
                    None,
                )
                if target is None:
                    raise ValueError(f"报价表中没有机型 {model}")
                storages = [storage] if storage else [
                    unit["storage"] for unit in target["units"] if unit["enabled"]
                ]
                if not storages:
                    raise ValueError(f"{model} 没有启用的容量筛选项")
                stream = ItemStream(handle)
                try:
                    collected = await self._collect_sources(keyword, model, storages, rules, stream,
                                                            [device_id] if device_id else [])
                finally:
                    await stream.close()
            else:
                collected = CollectResult(
                    items=injected_items,
                    region_applied=region_applied,
                    filter_applied=filter_applied,
                    passes=[{"label": "调试", "observed": len(injected_items), "error": None}],
                )
            checkpoint()
            if injected_items is not None:
                results.append(await self.process_items(
                    collected.items, rules, keyword, collected.region_applied,
                    storage if collected.filter_applied else None, run_id=run_id, expected_model=model,
                ))
            else:
                # 采集器已经边搜边交出了商品；这里只补处理没交出过的（例如不支持实时回调的采集器）。
                for pass_result in collected.passes:
                    pass_storage = pass_result.get("storage")
                    pass_device = pass_result.get("device", MAC_DEVICE)
                    stats = pass_stats.setdefault((pass_device, pass_storage), {"matched": 0, "notified": 0})
                    pass_items = [item for item in collected.items
                                  if item.get("source_storage") == pass_storage
                                  and item.get("source_device", MAC_DEVICE) == pass_device
                                  and item["item_id"] not in processed]
                    if not pass_result.get("error") and pass_items:
                        processed.update(item["item_id"] for item in pass_items)
                        part = await self.process_items(
                            pass_items, rules, keyword, pass_result.get("region_applied", collected.region_applied),
                            pass_storage if pass_result.get("filter_applied") else None,
                            run_id=run_id, expected_model=model,
                        )
                        stats["matched"] += part["matched"]
                        stats["notified"] += part["notified"]
                        results.append(part)
                    pass_result.update(matched=stats["matched"], notified=stats["notified"])
                if not any(pass_result.get("storage") is None for pass_result in collected.passes):
                    ambiguous_items = [item for item in collected.items
                                       if item.get("source_storage") is None and item["item_id"] not in processed]
                    if ambiguous_items:
                        results.append(await self.process_items(
                            ambiguous_items, rules, keyword, collected.region_applied, None,
                            run_id=run_id, expected_model=model,
                        ))
            result = {
                "observed": sum(part["observed"] for part in results),
                "matched": sum(part["matched"] for part in results),
                "notified": sum(part["notified"] for part in results),
                "decisions": [decision for part in results for decision in part["decisions"]],
            }
            result.update(
                region_applied=collected.region_applied,
                filter_applied=collected.filter_applied,
                passes=collected.passes,
                warnings=collected.warnings,
                # 兼容旧前端字段
                region_error=collected.region_error,
            )
            self._finish_run(run_id, True, result, "；".join(collected.warnings) or None)
            logger.info(
                "监控任务结束 run_id=%s ok=true observed=%d matched=%d notified=%d region_applied=%s filter_applied=%s warnings=%s",
                run_id, result.get("observed", 0), result.get("matched", 0), result.get("notified", 0),
                collected.region_applied, collected.filter_applied, collected.warnings or "无",
            )
            return {"run_id": run_id, "model": model, "storage": storage, **result}
        except MonitorStopped:
            result = {"observed": live["observed"], "matched": live["matched"],
                      "notified": sum(part["notified"] for part in results), "passes": live_passes,
                      "stopped": True}
            self._finish_run(run_id, False, result, STOP_REASON)
            return {"run_id": run_id, "model": model, "storage": storage, **result}
        except Exception as exc:
            partial = exc.partial if isinstance(exc, CollectionFailure) else None
            self._finish_run(run_id, False, {
                "passes": partial.passes if partial else [],
                "region_applied": partial.region_applied if partial else False,
                "filter_applied": partial.filter_applied if partial else False,
            }, str(exc))
            logger.exception("监控任务失败 run_id=%s model=%s storage=%s error=%s", run_id, model, storage, exc)
            raise
        finally:
            _current_live.reset(live_token)
            self._live.pop(run_id, None)

    async def _collect_sources(
        self, keyword: str, model: str, storages: list[str], rules: Rules, stream: ItemStream | None = None,
        devices: list[str] | None = None,
    ) -> CollectResult:
        selected = selected_devices(self.database) if devices is None else devices
        channels = {key: device_channel(self.database, key) for key in selected}
        clients = [value["client_ip"] for value in channels.values() if value["channel"] == "proxy" and value["client_ip"]]
        with self.proxy.reserve(clients):
            return await self._collect_selected_sources(keyword, model, storages, rules, stream, selected, channels)

    async def _collect_selected_sources(self, keyword, model, storages, rules, stream, selected, channels) -> CollectResult:
        """Run selected devices in order. A failed device does not discard others."""
        available = {device.id: device for device in await asyncio.to_thread(discover_devices)}
        combined = CollectResult(items=[])
        failures = []
        successes = 0
        smart = load_config(self.database)
        quotes = latest_entries(self.database, rules.quote_category) if smart.enabled else eligible_quotes(self.database, rules)

        def is_candidate(item: dict[str, Any], region_applied: bool, storage_hint: str) -> bool:
            decision = (candidate_decision(item, rules, quotes, smart, region_applied, storage_hint, model)
                        if smart.enabled else scope_decision(item, smart, model, storage_hint, basis="fixed")
                        or evaluate(item, rules, quotes, region_applied, storage_hint, model))
            return decision.matched and decision.storage == storage_hint and storage_hint in storages

        def check_detail(item: dict[str, Any], region_applied: bool, storage_hint: str) -> dict | None:
            try:
                # Current detail drives both early and final assessment.
                config = load_config(self.database)
                if not config.enabled:
                    return asdict(scope_decision(item, config, model, storage_hint, basis="detail", detail=True)
                                  or evaluate(item, rules, eligible_quotes(self.database, rules),
                                              region_applied, storage_hint, model))
                _, value = assess_local(
                    item, rules, latest_entries(self.database, rules.quote_category),
                    policies_for(self.database, model, rules.quote_category, storage_hint),
                    config, model, storage_hint, region_applied,
                )
                decision = asdict(self.smart_decision({"result": value, "snapshot": item}))
                decision.update(threshold_price=value.get("threshold_high"), expected_margin=None)
                if value["status"] != "rejected":
                    return decision  # Carry the completed local stage if link extraction fails.
                details = []
                if value.get("base_price") is not None:
                    details.append(f"基础报价 ¥{value['base_price']:g}")
                for fee in value.get("deductions", []):
                    amount = f"¥{fee['min']:g}" if fee['min'] == fee['max'] else f"¥{fee['min']:g}～{fee['max']:g}"
                    details.append(f"{fee['name']}扣{amount}{'起' if fee.get('open_ended') else ''}")
                if value.get("value_high") is not None:
                    details.append(f"扣费后回收参考上限 ¥{value['value_high']:g}")
                decision["reason"] = "详情判价不符合：" + value["reason"]
                if details:
                    decision["reason"] += " · " + "；".join(details)
                return decision
            except Exception:
                logger.warning("详情提前判价失败，继续原有取链接及正式评估流程", exc_info=True)
                return None

        for device_id in selected:
            checkpoint()
            device = available.get(device_id)
            label = device.name if device else device_id
            try:
                if not device_id.startswith("ios:") or (device and device.kind != "ios"):
                    raise RuntimeError("仅支持 iPhone；Mac 采集已禁用")
                if device is None or not device.connected:
                    status = device.connection_status if device else "未连接"
                    if device and "开发者模式未开启" in status:
                        self.wda_maintenance.attention(device.udid, "Developer Mode disabled")
                    elif device and ("未配对" in status or "配对中" in status):
                        self.wda_maintenance.attention(device.udid, "untrusted device")
                    raise RuntimeError(f"iPhone {status or '未连接'}；已保留勾选，重连后可继续使用")
                channel = channels[device_id]
                adapter, options = self.iphone_collector, {}
                if channel["channel"] == "proxy":
                    if not channel["client_ip"]:
                        raise RuntimeError("代理采集尚未配置手机 Wi-Fi IP，请在设备列表填写")
                    self.proxy.configure(proxy_clients(self.database))
                    adapter, options = self.iphone_proxy_collector, {"client_ip": channel["client_ip"]}
                part = await adapter.collect(
                    device.udid, device.os_version, keyword, rules.region_city,
                    rules.region_steps, storages, rules.scroll_pages, is_candidate,
                    on_items=stream.for_device(device_id, label) if stream else None,
                    check_detail=check_detail,
                    **options,
                )
                if any(not entry.get("error") for entry in part.passes):
                    successes += 1
                combined.items.extend({**item, "source_device": device_id} for item in part.items)
                for entry in part.passes:
                    combined.passes.append({**entry, "device": device_id, "device_name": label,
                                            "region_applied": entry.get("region_applied", part.region_applied)})
                failures.extend(f"{label}：{warning}" for warning in dict.fromkeys(part.warnings))
                combined.region_applied = combined.region_applied or part.region_applied
                combined.filter_applied = combined.filter_applied or part.filter_applied
            except Exception as exc:
                logger.exception("采集设备失败 device=%s model=%s", label, model)
                error = _brief_error(exc)
                failures.append(f"{label}：{error}")
                combined.passes.append({"label": "新发", "device": device_id, "device_name": label,
                                        "storage": None, "observed": 0, "error": error, "actions": []})
        combined.region_error = "；".join(failures) or None
        if not successes:
            raise CollectionFailure(combined.region_error or "没有可用的采集设备", combined)
        return combined

    def _record_cards(
        self, run_id: int, cards: list[dict[str, Any]], rules: Rules, meta: dict[str, Any], model: str,
    ) -> int:
        """把没有商品链接的列表卡片也记进运行记录：不推送，只用来核对这次搜索看到了什么。

        iPhone 端只给符合条件的候选进详情取链接，其余卡片走这里；Mac 端是没对上曝光记录的卡片。
        """
        storage = meta.get("storage")
        storage_hint = storage if meta.get("filter_applied") else None
        quotes = eligible_quotes(self.database, rules)
        label = (storage if str(storage).endswith("T") else f"{storage}G") if storage else meta.get("label")
        recorded = 0
        for card in cards:
            try:
                price = float(card["price"])
            except (KeyError, TypeError, ValueError):
                continue
            title = str(card.get("title") or "").strip()
            if not title:
                continue
            region = str(card.get("display_region") or "")
            item = {"title": title, "price": price, "display_region": region,
                    "item_url": "", "publish_time": None, "tags": [],
                    "seller_name": card.get("seller_name") or "",
                    "description": card.get("description") or "",
                    "detail_parameters": card.get("detail_parameters") or []}
            smart = load_config(self.database)
            decision = (candidate_decision(item, rules, latest_entries(self.database, rules.quote_category), smart,
                                           bool(meta.get("region_applied")), storage_hint, model)
                        if smart.enabled else scope_decision(item, smart, model, storage_hint, basis="fixed")
                        or evaluate(item, rules, quotes, bool(meta.get("region_applied")), storage_hint, model))
            if card.get("detail_rejection"):
                decision = MatchDecision(**card["detail_rejection"])
            elif card.get("detail_decision"):
                checked = MatchDecision(**card["detail_decision"])
                summary = "详情判价待确认" if checked.code == "pending" else "详情判价符合"
                decision = replace(checked, matched=False, code="link_failed",
                                   reason=f"{summary}，链接获取失败：{card.get('link_error') or '未取得分享链接'}")
            elif card.get("acquisition_stage"):
                stage = card["acquisition_stage"]
                label_text = "详情读取失败" if stage == "detail" else "核验未完成"
                decision = replace(decision, matched=False,
                                   code="detail_failed" if stage == "detail" else "verification_incomplete",
                                   reason=f"{label_text}：{card.get('link_error') or '尚未完成详情判价'}")
            elif decision.matched:
                reason = card.get("link_error") or (
                    f"标题容量（{decision.storage}）与当前容量筛选不一致，未进详情取链接"
                    if storage_hint and decision.storage != storage_hint else "未取得详情分享链接，原因未记录"
                )
                decision = replace(decision, matched=False, code="no_link", reason=f"核验未完成，尚不能判断是否符合推送条件：{reason}")
            digest = hashlib.sha1(f"{storage}|{title}|{price}|{region}".encode("utf-8")).hexdigest()[:12]
            real_id = str(card.get("item_id") or "")
            item["item_id"] = real_id if real_id.isdigit() and len(real_id) >= 8 else f"card:{digest}"
            if item["item_id"] == real_id:
                item["item_url"] = f"https://www.goofish.com/item?id={real_id}"
                item["publish_time"] = card.get("publish_time")
            self._save_run_item(run_id, label, item, decision, False)
            recorded += 1
        return recorded

    def close_interrupted_runs(self) -> int:
        """服务启动时：上次退出时还没结束的搜索标记为中断，避免一直显示「搜索中」。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE search_runs SET ok=0, finished_at=?, error=? WHERE ok IS NULL",
                (utc_now(), "服务重启或退出，本次搜索中断；已读到的商品保留在下方"),
            )
        return cursor.rowcount

    def _save_live_passes(self, run_id: int, passes: list[dict[str, Any]]) -> None:
        """搜索进行中，每完成一个容量就把该轮结果写进记录，后台可以实时看到。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_runs SET passes=? WHERE id=? AND ok IS NULL",
                (json.dumps(passes, ensure_ascii=False), run_id),
            )

    def _finish_run(self, run_id: int, ok: bool, result: dict[str, Any], error: str | None) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE search_runs SET finished_at=?, ok=?, observed=?, matched=?,
                    region_applied=?, filter_applied=?, passes=?, error=? WHERE id=?
                """,
                (
                    utc_now(),
                    int(ok),
                    result.get("observed", 0),
                    result.get("matched", 0),
                    int(bool(result.get("region_applied"))),
                    int(bool(result.get("filter_applied"))),
                    json.dumps(result.get("passes") or [], ensure_ascii=False),
                    error,
                    run_id,
                ),
            )

    async def process_items(
        self,
        items: list[dict[str, Any]],
        rules: Rules,
        keyword: str,
        region_applied: bool = False,
        storage_hint: str | None = None,
        run_id: int | None = None,
        expected_model: str | None = None,
    ) -> dict[str, Any]:
        config = load_config(self.database)
        if config.enabled:
            return await self._process_smart_items(items, rules, keyword, region_applied, storage_hint, run_id, expected_model)
        quotes = eligible_quotes(self.database, rules)
        matched = notified = 0
        decisions = []
        for raw in items:
            checkpoint()
            item = self._normalize_item(raw, keyword)
            decision = scope_decision(item, config, expected_model, storage_hint, basis="fixed", detail=True) or evaluate(
                item, rules, quotes, region_applied, storage_hint, expected_model)
            if decision.matched and not has_model_evidence(decision.model or "", item_text(item)):
                decision = replace(decision, matched=False, code="model_unconfirmed",
                                   reason="商品文字未确认iPhone机型，不能按搜索目标推送")
            self._save_item(item, decision)
            decisions.append(
                {"item_id": item["item_id"], "title": item["title"], "price": item["price"],
                 "matched": decision.matched, "reason": decision.reason, "code": decision.code}
            )
            deal_id = None if config.shadow else self._save_deal(item, decision, rules)
            if run_id is not None:
                storage_label = raw.get("source_storage")
                pass_label = (
                    f"{storage_label if str(storage_label).endswith('T') else f'{storage_label}G'}"
                    if storage_label else raw.get("source_pass")
                )
                self._save_run_item(run_id, pass_label, item, decision, bool(deal_id))
            if not deal_id:
                continue
            matched += 1
            checkpoint()
            result = await self.notifications.send(
                {
                    **item,
                    "model": decision.model,
                    "storage": decision.storage,
                    "quote_price": decision.quote_price,
                    "quote_grade": rules.quote_grade,
                    "region": decision.region,
                    "quote_reference": self._quote_reference(decision.model, decision.storage, rules),
                },
                decision.reason,
            )
            success = bool(result) and any(value.get("success") for value in result.values())
            notified += int(success)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE deals SET notification_status=?, notification_result=? WHERE id=?",
                    (
                        "sent" if success else ("disabled" if not result else "failed"),
                        notification_result_text(result),
                        deal_id,
                    ),
                )
        return {"observed": len(items), "matched": matched, "notified": notified, "decisions": decisions}

    async def _process_smart_items(self, items, rules, keyword, region_applied, storage_hint, run_id, expected_model):
        config = load_config(self.database)
        quotes = latest_entries(self.database, rules.quote_category)
        matched = notified = 0
        decisions = []
        for raw in items:
            checkpoint()
            created = False
            record = None
            item = self._normalize_item(raw, keyword)
            broad = candidate_decision(item, rules, quotes, config, region_applied, storage_hint, expected_model)
            if not broad.matched:
                decision = broad
            else:
                if (live := _current_live.get()) is not None:
                    live["stage"] = f"分析详情与扣费 · {item['item_id']}"
                logger.info("本地规则/AI复核开始 item_id=%s model=%s storage=%s", item["item_id"], broad.model, broad.storage)
                record = await self.smart.assess(item, rules, broad.model, broad.storage, region_applied)
                decision = self.smart_decision(record)
                created, sent = await self.publish_smart(record, rules)
                matched += int(created)
                notified += int(sent)
                logger.info("本地规则/AI复核结束 item_id=%s status=%s reason=%s", item["item_id"], record["status"], decision.reason)
            self._save_item(item, decision)
            if run_id is not None:
                label = raw.get("source_storage") or storage_hint
                self._save_run_item(run_id, label, item, decision, bool(broad.matched and created), record["id"] if record else None)
            decisions.append({"item_id": item["item_id"], "title": item["title"], "price": item["price"],
                              "matched": decision.matched, "reason": decision.reason, "code": decision.code})
        return {"observed": len(items), "matched": matched, "notified": notified, "decisions": decisions}

    @staticmethod
    def smart_decision(record):
        value, item = record["result"], record["snapshot"]
        low = value.get("value_low")
        return MatchDecision(
            value["status"] == "matched", value["reason"], value["model"], value["storage"],
            quote_entry_id=value.get("quote_entry_id"), quote_price=low,
            quote_grade=value.get("grade"), quote_basis="detail",
            valuation={key: value.get(key) for key in ("grade", "reference_grade", "base_price", "value_low", "value_high",
                       "threshold_low", "threshold_high", "max_over_quote", "deductions", "unpriced_conditions", "unpriced_notes")},
            threshold_price=value.get("threshold_low"),
            expected_margin=low - float(item["price"]) if low is not None else None,
            region=item.get("display_region"),
            code={"matched": "matched", "pending": "pending", "rejected": value.get("rejection_code") or "smart_rejected",
                  "analysis_failed": "analysis_failed"}[value["status"]],
        )

    async def publish_smart(self, record, rules):
        """Delivery dedup is separate from assessments, including one pending → matched upgrade."""
        checkpoint()
        value = record["result"]
        item = self._normalize_item(record["snapshot"], "")
        decision = self.smart_decision(record)
        blocked = scope_decision(item, load_config(self.database), decision.model, decision.storage, basis="detail", detail=True)
        if blocked is None and not has_model_evidence(decision.model or "", item_text(item)):
            blocked = MatchDecision(False, "商品文字未确认iPhone机型，不能按搜索目标推送", model=decision.model,
                                    storage=decision.storage, code="model_unconfirmed", quote_basis="detail")
        if blocked is not None:
            self._save_item(item, blocked)
            with self.database.connect() as c:
                c.execute("UPDATE smart_assessments SET notification_status='none' WHERE id=? AND notification_status NOT IN ('sent','duplicate')", (record["id"],))
            return False, False
        self._save_item(item, decision)
        if value.get("shadow") or value["status"] not in {"pending", "matched"} or value["status"] == "pending" and not value.get("notify_pending", True):
            state = "shadow" if value.get("shadow") else "none"
            with self.database.connect() as c:
                c.execute("UPDATE smart_assessments SET notification_status=? WHERE id=?", (state, record["id"]))
            return False, False
        tier = value["status"]
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            higher = c.execute("SELECT 1 FROM smart_notifications WHERE item_id=? AND tier='matched' AND status IN ('sent','sending')",
                               (item["item_id"],)).fetchone()
            existing = c.execute("SELECT status FROM smart_notifications WHERE item_id=? AND tier=?", (item["item_id"], tier)).fetchone()
            legacy = c.execute("SELECT 1 FROM deals WHERE item_id=? AND notification_status='sent'", (item["item_id"],)).fetchone()
            if higher or legacy or existing and existing[0] in {"sent", "sending"}:
                c.execute("UPDATE smart_assessments SET notification_status='duplicate' WHERE id=?", (record["id"],))
                return False, False
            c.execute("INSERT OR REPLACE INTO smart_notifications(item_id,tier,status,updated_at) VALUES(?,?,'sending',?)",
                      (item["item_id"], tier, utc_now()))
        grade_rules = replace(rules, quote_grade=value.get("grade") or rules.quote_grade)
        deal_id = self._save_deal(item, decision, grade_rules)
        try:
            response = await self.notifications.send({**item, "model": value["model"], "storage": value["storage"],
                "quote_price": value.get("value_low"), "quote_grade": value.get("grade"),
                "smart_assessment": value, "assessment_facts": record["facts"],
                "quote_reference": self._quote_reference(value["model"], value["storage"], grade_rules)}, decision.reason)
            sent = bool(response) and any(channel.get("success") for channel in response.values())
            detail = notification_result_text(response)
        except Exception:
            sent, detail = False, "通知发送失败"
        state = "sent" if sent else "failed"
        with self.database.connect() as c:
            c.execute("UPDATE smart_notifications SET status=?,updated_at=?,result=? WHERE item_id=? AND tier=?",
                      (state, utc_now(), detail, item["item_id"], tier))
            c.execute("UPDATE smart_assessments SET notification_status=?,notification_result=? WHERE id=?",
                      (state, detail, record["id"]))
            if deal_id:
                c.execute("UPDATE deals SET notification_status=?, notification_result=? WHERE id=?", (state, detail, deal_id))
            elif tier == "matched":
                c.execute("UPDATE deals SET notification_status=?, notification_result=? WHERE item_id=?", (state, detail, item["item_id"]))
        return bool(deal_id), sent

    def _quote_reference(self, model: str | None, storage: str | None, rules: Rules) -> dict[str, Any] | None:
        """推送里的完整参考价；查询失败不影响推送。"""
        if not model or not storage:
            return None
        try:
            return quote_reference(self.database, model, storage, rules.quote_category, rules.quote_grade)
        except Exception:
            logger.exception("读取参考报价失败 model=%s storage=%s", model, storage)
            return None

    # ------------------------------------------------------------------ 存储

    @staticmethod
    def _normalize_item(item: dict[str, Any], fallback_keyword: str) -> dict[str, Any]:
        item_id = str(item.get("item_id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not item_id or not title:
            raise ValueError("商品必须包含 item_id 和 title")
        try:
            price = float(item["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("商品 price 必须是数字") from exc
        # 只用采集到的真实商品 ID 生成标准详情页链接，避免把列表序号或任务 ID 当商品 ID。
        item_url = f"https://www.goofish.com/item?id={item_id}" if item_id.isdigit() and len(item_id) >= 8 else ""
        return {
            "item_id": item_id,
            "keyword": str(item.get("keyword") or fallback_keyword),
            "title": title,
            "price": price,
            "description": str(item.get("description") or ""),
            "detail_parameters": item.get("detail_parameters") if isinstance(item.get("detail_parameters"), list) else [],
            "publish_time": item.get("publish_time"),
            "display_region": str(item.get("display_region") or ""),
            "seller_name": str(item.get("seller_name") or "").strip(),
            "is_fish_shop": item.get("is_fish_shop") if type(item.get("is_fish_shop")) is bool else None,
            "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
            "image_urls": [
                url.strip() for url in item.get("image_urls", [])
                if isinstance(url, str) and url.strip().startswith(("https://", "http://"))
            ] if isinstance(item.get("image_urls"), list) else [],
            "item_url": item_url,
            "observed_at": str(item.get("observed_at") or utc_now()),
            "source_row_id": item.get("source_row_id"),
            "index": item.get("index"),
        }

    def _save_item(self, item: dict[str, Any], decision: MatchDecision) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO observed_items(
                    item_id, keyword, title, price, publish_time, display_region,
                    tags_json, image_urls_json, item_url, observed_at, source_row_id, raw_index,
                    model, storage, decision, matched, seller_name, is_fish_shop
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title=excluded.title, price=excluded.price,
                    display_region=excluded.display_region, tags_json=excluded.tags_json,
                    image_urls_json=excluded.image_urls_json,
                    item_url=excluded.item_url,
                    observed_at=excluded.observed_at, model=excluded.model,
                    storage=excluded.storage, decision=excluded.decision, matched=excluded.matched,
                    seller_name=COALESCE(NULLIF(excluded.seller_name, ''), observed_items.seller_name),
                    is_fish_shop=COALESCE(excluded.is_fish_shop, observed_items.is_fish_shop)
                """,
                (
                    item["item_id"], item["keyword"], item["title"], item["price"],
                    item["publish_time"], item["display_region"],
                    json.dumps(item["tags"], ensure_ascii=False),
                    json.dumps(item["image_urls"], ensure_ascii=False), item["item_url"],
                    item["observed_at"], item["source_row_id"], item["index"],
                    decision.model, decision.storage, decision.reason, int(decision.matched),
                    item["seller_name"], item["is_fish_shop"],
                ),
            )

    def _save_run_item(
        self,
        run_id: int,
        pass_label: str | None,
        item: dict[str, Any],
        decision: MatchDecision,
        new_deal: bool,
        assessment_id: int | None = None,
    ) -> None:
        """记录本次搜索看到的这件商品，供运行记录逐条查看。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO run_items(
                    run_id, item_id, pass_label, position, title, price, display_region, item_url,
                    publish_time, model, storage, quote_price, threshold_price, expected_margin,
                    matched, code, decision, new_deal, assessment_id, quote_grade, quote_basis, valuation_json
                ) VALUES (?, ?, ?, (SELECT COUNT(*) FROM run_items WHERE run_id=?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, item["item_id"], pass_label, run_id, item["title"], item["price"],
                    item["display_region"], item["item_url"], item["publish_time"],
                    decision.model, decision.storage, decision.quote_price, decision.threshold_price,
                    decision.expected_margin, int(decision.matched), decision.code, decision.reason,
                    int(new_deal), assessment_id, decision.quote_grade, decision.quote_basis,
                    json.dumps(decision.valuation, ensure_ascii=False) if decision.valuation is not None else None,
                ),
            )

    def _save_deal(self, item: dict[str, Any], decision: MatchDecision, rules: Rules) -> int | None:
        """保存命中商品；同一商品 ID 只保存并推送一次。"""
        if not decision.matched:
            return None
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO deals(
                    item_id, model, storage, grade, quote_category, quote_entry_id, quote_price,
                    item_price, expected_margin, threshold_price, region, reason, matched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["item_id"], decision.model, decision.storage, rules.quote_grade,
                    rules.quote_category, decision.quote_entry_id, decision.quote_price,
                    item["price"], decision.expected_margin, decision.threshold_price,
                    decision.region, decision.reason, utc_now(),
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None
