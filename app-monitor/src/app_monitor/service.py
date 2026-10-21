"""监控主流程：按全局规则轮流搜索各机型，判价、去重、推送。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .collector import CollectResult, GoofishCollector
from .db import Database, utc_now
from .devices import MAC_DEVICE, discover_devices, selected_devices
from .iphone_collector import IPhoneCollector
from .matching import MatchDecision, evaluate
from .notifications import NotificationService, notification_result_text
from .quotes import quote_reference
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


# 连续失败达到该次数后暂停一段时间（通常是 App 未运行、未登录或没有辅助功能权限）。
FAILURE_PAUSE_AFTER = 3
FAILURE_PAUSE = timedelta(minutes=10)


class MonitorService:
    def __init__(
        self,
        database: Database,
        collector: GoofishCollector,
        notifications: NotificationService,
    ):
        self.database = database
        self.collector = collector
        self.iphone_collector = IPhoneCollector(collector.settings) if hasattr(collector, "settings") else None
        self.notifications = notifications
        self._lock = asyncio.Lock()
        # 正在进行的搜索：run_id、当前步骤、已看到 / 命中件数，供后台实时显示
        self.live: dict[str, Any] | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    # ------------------------------------------------------------------ 调度

    def schedule_state(self, now: datetime | None = None) -> dict[str, Any]:
        """当前是否应该搜索、为什么、下一个搜索单元是谁。供调度和后台展示共用。"""
        now = now or datetime.now().astimezone()
        rules = load_rules(self.database)
        units = search_units(self.database, rules)
        state: dict[str, Any] = {
            "active": False, "reason": "", "next_model": None, "next_storage": None,
            "next_label": None, "targets": len({unit["model"] for unit in units}), "units": len(units),
        }
        if not rules.enabled:
            state["reason"] = "监控已暂停"
        elif not units:
            state["reason"] = "没有可搜索的机型，请先同步报价"
        elif not in_search_window(rules, now):
            state["reason"] = f"不在搜索时段 {rules.search_start}–{rules.search_end}"
        elif self._failure_paused(now):
            state["reason"] = f"连续 {FAILURE_PAUSE_AFTER} 次搜索失败，暂停 10 分钟"
        else:
            state["active"] = True
            state["reason"] = "搜索中"
        due = self._next_due(units, rules, now)
        if due:
            state.update(
                next_model=due["model"], next_storage=due["storage"],
                next_label=unit_label(due["model"], due["storage"]),
            )
        if state["active"] and not due:
            state["reason"] = "本轮已搜完，等待下一轮"
        return state

    async def run_due(self, now: datetime | None = None) -> dict[str, Any] | None:
        """调度器每 15 秒调用一次：空闲时搜索一个到期的单元，搜完立即可以开始下一个。"""
        if self.busy:
            return None
        now = now or datetime.now().astimezone()
        state = self.schedule_state(now)
        if not state["active"] or not state["next_model"]:
            return None
        try:
            return await self.run_model(state["next_model"], state["next_storage"], trigger="schedule")
        except Exception:
            return None  # 错误已写入 search_runs

    def _next_due(self, units: list[dict[str, Any]], rules: Rules, now: datetime) -> dict[str, Any] | None:
        last = self.last_runs()
        interval = timedelta(minutes=max(1, rules.model_interval_minutes))
        due = []
        for unit in units:
            started = last.get(unit_key(unit["model"], None), {}).get("started_at")
            started_at = datetime.fromisoformat(started) if started else None
            if started_at is None or now - started_at >= interval:
                due.append((started_at or datetime.min.replace(tzinfo=now.tzinfo), unit))
        return min(due, key=lambda value: value[0])[1] if due else None

    def _failure_paused(self, now: datetime) -> bool:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT ok, started_at FROM search_runs WHERE ok IS NOT NULL ORDER BY id DESC LIMIT ?",
                (FAILURE_PAUSE_AFTER,),
            ).fetchall()
        if len(rows) < FAILURE_PAUSE_AFTER or any(row["ok"] for row in rows):
            return False
        return now - datetime.fromisoformat(rows[0]["started_at"]) < FAILURE_PAUSE

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
    ) -> dict[str, Any]:
        async with self._lock:
            rules = load_rules(self.database)
            keyword = search_keyword(model)
            with self.database.connect() as connection:
                run_id = connection.execute(
                    """
                    INSERT INTO search_runs(model, storage, keyword, region, trigger, started_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (model, storage, keyword, rules.region_city or None, trigger, utc_now()),
                ).lastrowid
            logger.info(
                "监控任务开始 run_id=%s trigger=%s model=%s storage=%s keyword=%s region=%s",
                run_id, trigger, model, storage or "全部容量", keyword, rules.region_city or "未指定",
            )
            try:
                results: list[dict[str, Any]] = []
                processed: set[str] = set()
                pass_stats: dict[tuple[str, str | None], dict[str, int]] = {}
                live_passes: list[dict[str, Any]] = []
                self.live = {"run_id": run_id, "model": model, "stage": "准备搜索", "observed": 0, "matched": 0}

                async def handle(items: list[dict[str, Any]], meta: dict[str, Any]) -> None:
                    """实时处理采集器交出的商品：写入运行记录、判价、命中就立即推送。"""
                    if meta.get("stage"):
                        self.live["stage"] = (
                            f"{meta['device_name']} · {meta['stage']}"
                            if meta.get("device_name") and meta.get("device") != MAC_DEVICE else meta["stage"]
                        )
                    if meta.get("pass_info") is not None:
                        live_passes.append({**meta["pass_info"], "device": meta.get("device", MAC_DEVICE),
                                            "device_name": meta.get("device_name")})
                        self._save_live_passes(run_id, live_passes)
                    if meta.get("cards"):
                        recorded = self._record_cards(run_id, meta["cards"], rules, meta, model)
                        self.live["observed"] += recorded
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
                    self.live["observed"] += part["observed"]
                    self.live["matched"] += part["matched"]
                    results.append(part)

                if injected_items is None:
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
                        collected = await self._collect_sources(keyword, model, storages, rules, stream)
                    finally:
                        await stream.close()
                else:
                    collected = CollectResult(
                        items=injected_items,
                        region_applied=region_applied,
                        filter_applied=filter_applied,
                        passes=[{"label": "调试", "observed": len(injected_items), "error": None}],
                    )
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
                self.live = None

    async def _collect_sources(
        self, keyword: str, model: str, storages: list[str], rules: Rules, stream: ItemStream | None = None,
    ) -> CollectResult:
        """Run selected devices in order. A failed device does not discard others."""
        selected = selected_devices(self.database)
        available = {device.id: device for device in await asyncio.to_thread(discover_devices)}
        combined = CollectResult(items=[])
        failures = []
        successes = 0
        quotes = eligible_quotes(self.database, rules)

        def is_candidate(item: dict[str, Any], region_applied: bool, storage_hint: str) -> bool:
            decision = evaluate(item, rules, quotes, region_applied, storage_hint, model)
            return decision.matched and decision.storage == storage_hint and storage_hint in storages

        for device_id in selected:
            device = available.get(device_id)
            label = device.name if device else device_id
            try:
                if device_id == MAC_DEVICE:
                    part = await self.collector.collect(
                        keyword, region_steps=rules.region_steps,
                        region_hint=rules.region_city,
                        storage_filters=[(value, rules.storage_steps(value)) for value in storages],
                        scroll_pages=rules.scroll_pages,
                        on_items=stream.for_device(device_id, label) if stream else None,
                    )
                else:
                    if device is None or not device.connected:
                        raise RuntimeError("iPhone 未连接；已保留勾选，重连后可继续使用")
                    if self.iphone_collector is None:
                        raise RuntimeError("iPhone 采集器未配置")
                    part = await self.iphone_collector.collect(
                        device.udid, device.os_version, keyword, rules.region_city,
                        rules.region_steps, storages, rules.scroll_pages, is_candidate,
                        on_items=stream.for_device(device_id, label) if stream else None,
                    )
                if device_id == MAC_DEVICE or any(not entry.get("error") for entry in part.passes):
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
                failures.append(f"{label}：{exc}")
                combined.passes.append({"label": "新发", "device": device_id, "device_name": label,
                                        "storage": None, "observed": 0, "error": str(exc), "actions": []})
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
                    "item_url": "", "publish_time": None, "tags": []}
            decision = evaluate(item, rules, quotes, bool(meta.get("region_applied")), storage_hint, model)
            if decision.matched:
                reason = card.get("link_error") or (
                    f"标题容量（{decision.storage}）与当前容量筛选不一致，未进详情取链接" if storage_hint else "未进详情取链接"
                )
                decision = replace(decision, matched=False, code="no_link", reason=f"价格符合，但没拿到商品链接：{reason}")
            digest = hashlib.sha1(f"{storage}|{title}|{price}|{region}".encode("utf-8")).hexdigest()[:12]
            item["item_id"] = f"card:{digest}"
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
        quotes = eligible_quotes(self.database, rules)
        matched = notified = 0
        decisions = []
        for raw in items:
            item = self._normalize_item(raw, keyword)
            decision = evaluate(item, rules, quotes, region_applied, storage_hint, expected_model)
            self._save_item(item, decision)
            decisions.append(
                {"item_id": item["item_id"], "title": item["title"], "price": item["price"],
                 "matched": decision.matched, "reason": decision.reason, "code": decision.code}
            )
            deal_id = self._save_deal(item, decision, rules)
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
    ) -> None:
        """记录本次搜索看到的这件商品，供运行记录逐条查看。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO run_items(
                    run_id, item_id, pass_label, position, title, price, display_region, item_url,
                    publish_time, model, storage, quote_price, threshold_price, expected_margin,
                    matched, code, decision, new_deal
                ) VALUES (?, ?, ?, (SELECT COUNT(*) FROM run_items WHERE run_id=?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, item["item_id"], pass_label, run_id, item["title"], item["price"],
                    item["display_region"], item["item_url"], item["publish_time"],
                    decision.model, decision.storage, decision.quote_price, decision.threshold_price,
                    decision.expected_margin, int(decision.matched), decision.code, decision.reason,
                    int(new_deal),
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
