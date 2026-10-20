"""监控主流程：按全局规则轮流搜索各机型，判价、去重、推送。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from .collector import CollectResult, GoofishCollector
from .db import Database, utc_now
from .matching import MatchDecision, evaluate
from .notifications import NotificationService, notification_result_text
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
        self.notifications = notifications
        self._lock = asyncio.Lock()

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
                    collected = await self.collector.collect(
                        keyword,
                        region_steps=rules.region_steps,
                        region_hint=rules.region_city,
                        storage_filters=[(value, rules.storage_steps(value)) for value in storages],
                        scroll_pages=rules.scroll_pages,
                    )
                else:
                    collected = CollectResult(
                        items=injected_items,
                        region_applied=region_applied,
                        filter_applied=filter_applied,
                        passes=[{"label": "调试", "observed": len(injected_items), "error": None}],
                    )
                results = []
                if injected_items is not None:
                    results.append(await self.process_items(
                        collected.items, rules, keyword, collected.region_applied,
                        storage if collected.filter_applied else None, run_id=run_id, expected_model=model,
                    ))
                else:
                    for pass_result in collected.passes:
                        pass_storage = pass_result.get("storage")
                        pass_items = [item for item in collected.items
                                      if item.get("source_storage") == pass_storage]
                        pass_result["matched"] = 0
                        pass_result["notified"] = 0
                        if pass_result.get("error") or not pass_items:
                            continue
                        part = await self.process_items(
                            pass_items, rules, keyword, collected.region_applied,
                            pass_storage if pass_result.get("filter_applied") else None,
                            run_id=run_id, expected_model=model,
                        )
                        pass_result.update(matched=part["matched"], notified=part["notified"])
                        results.append(part)
                    if not any(pass_result.get("storage") is None for pass_result in collected.passes):
                        ambiguous_items = [item for item in collected.items if item.get("source_storage") is None]
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
                self._finish_run(run_id, False, {}, str(exc))
                logger.exception("监控任务失败 run_id=%s model=%s storage=%s error=%s", run_id, model, storage, exc)
                raise

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
            result = await self.notifications.send(item, decision.reason)
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
                    model, storage, decision, matched
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title=excluded.title, price=excluded.price,
                    display_region=excluded.display_region, tags_json=excluded.tags_json,
                    image_urls_json=excluded.image_urls_json,
                    item_url=excluded.item_url,
                    observed_at=excluded.observed_at, model=excluded.model,
                    storage=excluded.storage, decision=excluded.decision, matched=excluded.matched
                """,
                (
                    item["item_id"], item["keyword"], item["title"], item["price"],
                    item["publish_time"], item["display_region"],
                    json.dumps(item["tags"], ensure_ascii=False),
                    json.dumps(item["image_urls"], ensure_ascii=False), item["item_url"],
                    item["observed_at"], item["source_row_id"], item["index"],
                    decision.model, decision.storage, decision.reason, int(decision.matched),
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
