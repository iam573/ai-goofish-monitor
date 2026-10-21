from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .collector import GoofishCollector
from .config import ROOT, settings
from .db import Database, rows_as_dicts, utc_now
from .devices import discover_devices, selected_devices, set_selected_devices
from .notifications import NotificationService
from .quotes import (
    APPLE_PHONE_CATEGORIES,
    GRADE_COLUMNS,
    QuoteService,
    grouped_apple_products,
    quote_status,
)
from .regions import default_region_path
from .rules import (
    Rules,
    load_rules,
    save_rules,
    search_targets,
    series_of,
)
from .service import MonitorService, unit_key


database = Database(settings.database)
quote_service = QuoteService(database, settings)
notification_service = NotificationService(settings)
monitor_service = MonitorService(database, GoofishCollector(settings), notification_service)
scheduler = AsyncIOScheduler(timezone=settings.timezone)


# ---------------------------------------------------------------------- 定时任务

def quote_time_reached(now: datetime | None = None) -> bool:
    now = now or datetime.now().astimezone()
    return (now.hour, now.minute) >= (settings.quote_hour, settings.quote_minute)


async def refresh_quotes_if_stale() -> None:
    """报价每天早上更新：到点后每 30 分钟检查一次，拿到当天报价就不再下载。"""
    if quote_time_reached():
        await quote_service.sync_if_stale()


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    last_hour = min(23, settings.quote_hour + 3)
    scheduler.add_job(
        refresh_quotes_if_stale,
        CronTrigger(hour=f"{settings.quote_hour}-{last_hour}", minute="*/30", timezone=settings.timezone),
        id="daily-quotes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        monitor_service.run_due,
        "interval",
        seconds=15,
        id="search-tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    # 启动补偿：服务在报价时间之后才启动时，立即检查一次当天报价。
    startup = asyncio.create_task(refresh_quotes_if_stale())
    yield
    startup.cancel()
    scheduler.shutdown(wait=False)


app = FastAPI(title="闲鱼报价捡漏监控", version="0.3.4", lifespan=lifespan)
web_dir = ROOT / "web"
app.mount("/assets", StaticFiles(directory=web_dir), name="assets")


class DeviceSelectionPayload(BaseModel):
    ids: list[str]


@app.get("/api/devices")
async def devices() -> dict[str, Any]:
    selected = selected_devices(database)
    found = await asyncio.to_thread(discover_devices)
    return {"devices": [device.as_dict(device.id in selected) for device in found], "selected": selected}


@app.put("/api/devices/selection")
async def select_devices(payload: DeviceSelectionPayload) -> dict[str, Any]:
    try:
        selected = await asyncio.to_thread(set_selected_devices, database, payload.ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"selected": selected}


@app.middleware("http")
async def no_stale_assets(request: Request, call_next):
    """后台页面和脚本每次都向服务端确认是否有更新，避免更新后浏览器继续用旧脚本。"""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """给脚本和样式加上按文件修改时间生成的版本号，文件一变浏览器就会取新的。"""
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "styles.css"):
        version = int((web_dir / name).stat().st_mtime)
        html = html.replace(f"/assets/{name}", f"/assets/{name}?v={version}")
    return HTMLResponse(html)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "time": utc_now()}


# ---------------------------------------------------------------------- 概览

@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    today = datetime.now().astimezone().date().isoformat()
    with database.connect() as connection:
        counts = {
            "deals_today": connection.execute(
                "SELECT COUNT(*) FROM deals WHERE substr(matched_at, 1, 10)=?", (today,)
            ).fetchone()[0],
            "deals_total": connection.execute("SELECT COUNT(*) FROM deals").fetchone()[0],
            "runs_today": connection.execute(
                "SELECT COUNT(*) FROM search_runs WHERE substr(started_at, 1, 10)=?", (today,)
            ).fetchone()[0],
            "items_today": connection.execute(
                "SELECT COUNT(*) FROM observed_items WHERE substr(observed_at, 1, 10)=?", (today,)
            ).fetchone()[0],
        }
        last_run = connection.execute("SELECT * FROM search_runs ORDER BY id DESC LIMIT 1").fetchone()
    rules = load_rules(database)
    return {
        **counts,
        "today": today,
        "rules": rules.to_dict(),
        "region_steps": rules.region_steps,
        "quotes": quote_status(database),
        "quote_schedule": f"{settings.quote_hour:02d}:{settings.quote_minute:02d}",
        "quote_last_check": database.state_get("quote_last_check"),
        "schedule": monitor_service.schedule_state(),
        "busy": monitor_service.busy,
        "last_run": monitor_service._run_dict(last_run) if last_run else None,
        "track_db_available": settings.track_db.exists(),
        "notification_channels": notification_service.configured_channels,
    }


# ---------------------------------------------------------------------- 规则与机型

class RulesPayload(BaseModel):
    enabled: bool = True
    region_city: str = Field(default="", max_length=20)
    region_path: str = Field(default="", max_length=120)
    quote_category: str = "100025"
    quote_grade: str = "小花"
    max_over_quote: float = Field(default=200, ge=0)
    min_price_ratio: float = Field(default=0.3, ge=0, lt=1)
    exclude_keywords: str = Field(default="", max_length=1000)
    search_start: str = "09:00"
    search_end: str = "23:00"
    storage_path: str = Field(default="筛选 > 存储容量 > {容量} > 确定", max_length=120)
    scroll_pages: int = Field(default=3, ge=0, le=10)
    model_interval_minutes: int = Field(default=90, ge=5, le=1440)
    disabled_models: list[str] = Field(default_factory=list)


@app.get("/api/rules")
async def get_rules() -> dict[str, Any]:
    rules = load_rules(database)
    return _rules_view(rules)


def _rules_view(rules: Rules) -> dict[str, Any]:
    return {
        **rules.to_dict(),
        "region_steps": rules.region_steps,
        "storage_steps_example": rules.storage_steps("256"),
        "pass_list": rules.passes,
    }


@app.put("/api/rules")
async def put_rules(payload: RulesPayload) -> dict[str, Any]:
    if payload.quote_grade not in GRADE_COLUMNS:
        raise HTTPException(422, "不支持的参考成色")
    if payload.quote_category not in APPLE_PHONE_CATEGORIES:
        raise HTTPException(422, "不支持的报价分类")
    try:
        rules = save_rules(database, Rules.from_dict(payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _rules_view(rules)


@app.get("/api/rules/region-path")
async def region_path(city: str = Query(default="", max_length=20)) -> dict[str, str]:
    return {"path": default_region_path(city)}


@app.get("/api/targets")
async def targets() -> list[dict[str, Any]]:
    rules = load_rules(database)
    last = monitor_service.last_runs()
    with database.connect() as connection:
        deal_counts = {
            row["model"]: row["count"]
            for row in connection.execute("SELECT model, COUNT(*) AS count FROM deals GROUP BY model")
        }
    values = search_targets(database, rules)
    for value in values:
        latest_run = last.get(unit_key(value["model"], None))
        for unit in value["units"]:
            pass_result = next(
                (result for result in (latest_run or {}).get("passes", [])
                 if result.get("storage") == unit["storage"]),
                None,
            )
            unit["last_run"] = ({
                **latest_run,
                "observed": pass_result.get("observed", 0),
                "matched": pass_result.get("matched", 0),
                "ok": 0 if pass_result.get("error") else latest_run.get("ok"),
                "error": pass_result.get("error") or latest_run.get("error"),
                "passes": [pass_result],
            } if latest_run and pass_result else None)
        value["last_run"] = latest_run
        value["deals"] = deal_counts.get(value["model"], 0)
    return values


class TargetPayload(BaseModel):
    enabled: bool
    storage: str | None = None


@app.put("/api/targets/{model}")
async def update_target(model: str, payload: TargetPayload) -> dict[str, Any]:
    """机型勾选启用全部容量；容量按钮只切换该容量。"""
    rules = load_rules(database)
    key = f"{model}|{payload.storage}" if payload.storage else model
    disabled = set(rules.disabled_models)
    if payload.enabled:
        disabled.discard(key)
        if not payload.storage:
            disabled = {value for value in disabled if not value.startswith(f"{model}|")}
    else:
        disabled.add(key)
    rules.disabled_models = sorted(disabled)
    save_rules(database, rules)
    return {"model": model, "storage": payload.storage, "enabled": payload.enabled}


@app.put("/api/target-series/{series}")
async def update_target_series(series: int, payload: TargetPayload) -> dict[str, Any]:
    """系列勾选启用全部机型与容量；取消勾选停用整个系列。"""
    rules = load_rules(database)
    key = f"@series:{series}"
    disabled = set(rules.disabled_models)
    if payload.enabled:
        disabled.discard(key)
        disabled = {
            value for value in disabled
            if value.startswith("@series:") or series_of(value.split("|", 1)[0]) != series
        }
    else:
        disabled.add(key)
    rules.disabled_models = sorted(disabled)
    save_rules(database, rules)
    return {"series": series, "enabled": payload.enabled}


@app.post("/api/targets/{model}/run")
async def run_target(model: str, storage: str | None = None) -> dict[str, Any]:
    """立即搜索一个单元；按容量拆分时需要指定 storage。"""
    if monitor_service.busy:
        raise HTTPException(409, "正在搜索其他机型，请稍后再试")
    target = next(
        (item for item in search_targets(database, load_rules(database)) if item["model"] == model), None
    )
    if not target:
        raise HTTPException(404, "报价表中没有该机型")
    if storage and storage not in target["storages"]:
        raise HTTPException(404, f"报价表中没有 {model} {storage}")
    try:
        return await monitor_service.run_model(model, storage, trigger="manual")
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


class ObservationPayload(BaseModel):
    item_id: str
    title: str
    price: float = Field(gt=0)
    keyword: str | None = None
    publish_time: str | None = None
    display_region: str | None = None
    tags: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    item_url: str | None = None


@app.post("/api/targets/{model}/observations")
async def ingest_observations(
    model: str,
    payload: list[ObservationPayload],
    storage: str | None = None,
    region_applied: bool = False,
    filter_applied: bool = False,
) -> dict[str, Any]:
    """不经过 App，直接提交商品走一遍判价和推送，用于调试规则。"""
    return await monitor_service.run_model(
        model,
        storage,
        trigger="inject",
        injected_items=[item.model_dump(exclude_none=True) for item in payload],
        region_applied=region_applied,
        filter_applied=filter_applied,
    )


# ---------------------------------------------------------------------- 报价

@app.get("/api/apple-products")
async def apple_products() -> list[dict[str, Any]]:
    return grouped_apple_products(database)


@app.get("/api/quotes/status")
async def quotes_status() -> list[dict[str, Any]]:
    return quote_status(database)


@app.post("/api/quotes/sync")
async def sync_quotes() -> list[dict[str, Any]]:
    return await quote_service.sync_all()


@app.get("/api/quotes/batches")
async def quote_batches() -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT qb.*, COUNT(qe.id) AS entry_count
            FROM quote_batches qb LEFT JOIN quote_entries qe ON qe.batch_id=qb.id
            GROUP BY qb.id ORDER BY qb.fetched_at DESC, qb.id DESC LIMIT 100
            """
        ).fetchall()
    values = rows_as_dicts(rows)
    quote_dir = (database.path.parent / "daily-quotes").resolve()
    for value in values:
        path = Path(value["image_path"]).resolve()
        value["image_available"] = (
            path.is_relative_to(quote_dir)
            and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            and path.is_file()
        )
    return values


@app.get("/api/quotes/batches/{batch_id}/image")
async def quote_batch_image(batch_id: int) -> FileResponse:
    """从已保存的报价批次读取原图，供后台直接预览。"""
    with database.connect() as connection:
        row = connection.execute(
            "SELECT image_path FROM quote_batches WHERE id=?", (batch_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "报价图片不存在")
    path = Path(row["image_path"]).resolve()
    quote_dir = (database.path.parent / "daily-quotes").resolve()
    if not path.is_relative_to(quote_dir) or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not path.is_file():
        raise HTTPException(404, "报价图片不存在")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


class QuoteEntryPayload(BaseModel):
    model: str = Field(min_length=1, max_length=80)
    storage: str = Field(min_length=1, max_length=20)
    grade: str
    price: float = Field(gt=0)
    enabled: bool = True


@app.put("/api/quotes/entries/{entry_id}")
async def update_quote_entry(entry_id: int, payload: QuoteEntryPayload) -> dict[str, Any]:
    if payload.grade not in GRADE_COLUMNS:
        raise HTTPException(422, "不支持的报价成色")
    with database.connect() as connection:
        cursor = connection.execute(
            """
            UPDATE quote_entries SET model=?, storage=?, grade=?, price=?,
                enabled=?, source='manual', updated_at=? WHERE id=?
            """,
            (payload.model, payload.storage.upper(), payload.grade, payload.price,
             int(payload.enabled), utc_now(), entry_id),
        )
        row = connection.execute("SELECT * FROM quote_entries WHERE id=?", (entry_id,)).fetchone()
    if not cursor.rowcount or not row:
        raise HTTPException(404, "报价不存在")
    return dict(row)


# ---------------------------------------------------------------------- 结果

@app.get("/api/deals")
async def deals(limit: int = Query(default=200, ge=1, le=1000)) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT d.*, i.title, i.item_url, i.publish_time, i.display_region, i.image_urls_json
            FROM deals d JOIN observed_items i ON i.item_id=d.item_id
            ORDER BY d.matched_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows_as_dicts(rows)


_RUN_COUNTS = """
    SELECT r.*,
        (SELECT COUNT(*) FROM run_items i WHERE i.run_id=r.id) AS item_count,
        (SELECT COUNT(*) FROM run_items i WHERE i.run_id=r.id AND i.matched=1) AS hit_count,
        (SELECT COUNT(*) FROM run_items i WHERE i.run_id=r.id AND i.new_deal=1) AS new_count
    FROM search_runs r
"""


@app.get("/api/runs")
async def runs(
    limit: int = Query(default=100, ge=1, le=1000),
    status: str | None = Query(default=None, pattern="^(hit|failed|warn)$"),
    model: str | None = None,
) -> list[dict[str, Any]]:
    """搜索记录列表；status=hit 只看有命中的，failed 只看失败的，warn 只看有警告的。"""
    clauses, params = [], []
    if status == "hit":
        clauses.append("EXISTS (SELECT 1 FROM run_items i WHERE i.run_id=r.id AND i.matched=1)")
    elif status == "failed":
        clauses.append("r.ok=0")
    elif status == "warn":
        clauses.append("r.ok=1 AND r.error IS NOT NULL")
    if model:
        clauses.append("r.model=?")
        params.append(model)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with database.connect() as connection:
        rows = connection.execute(
            f"{_RUN_COUNTS} {where} ORDER BY r.id DESC LIMIT ?", [*params, limit]
        ).fetchall()
    return [monitor_service._run_dict(row) for row in rows]


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: int) -> dict[str, Any]:
    """一次搜索的完整明细：看到的每件商品、属于哪一轮、判价结论。命中的排在最前。"""
    with database.connect() as connection:
        row = connection.execute(f"{_RUN_COUNTS} WHERE r.id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "搜索记录不存在")
        items = rows_as_dicts(
            connection.execute(
                "SELECT * FROM run_items WHERE run_id=? ORDER BY matched DESC, position",
                (run_id,),
            ).fetchall()
        )
    codes: dict[str, int] = {}
    for item in items:
        codes[item["code"] or "other"] = codes.get(item["code"] or "other", 0) + 1
    return {**monitor_service._run_dict(row), "items": items, "codes": codes}


@app.get("/api/items")
async def items(
    limit: int = Query(default=200, ge=1, le=1000), model: str | None = None
) -> list[dict[str, Any]]:
    clause, params = ("WHERE model=?", [model]) if model else ("", [])
    with database.connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM observed_items {clause} ORDER BY observed_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    values = rows_as_dicts(rows)
    for value in values:
        value["tags"] = json.loads(value.pop("tags_json") or "[]")
    return values


@app.post("/api/notifications/test")
async def test_notification() -> dict[str, Any]:
    return {
        "configured_channels": notification_service.configured_channels,
        "results": await notification_service.test(),
    }
