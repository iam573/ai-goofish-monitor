from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, settings
from .db import Database, rows_as_dicts, utc_now
from .instance_lock import monitor_instance
from .item_identity import has_model_evidence, item_text
from .device_screenshot import ScreenshotBusy, capture_screenshot
from .devices import discover_devices, selected_devices, set_selected_devices, device_channel, set_device_channel, proxy_clients, proxy_enabled
from .notifications import NotificationService
from .stop_control import STOP_REASON
from .quotes import (
    APPLE_PHONE_CATEGORIES,
    GRADE_COLUMNS,
    QuoteService,
    grouped_apple_products,
    latest_deductions,
    quote_status,
)
from .regions import default_region_path
from .rules import (
    Rules,
    load_rules,
    save_rules,
    search_targets,
    set_targets_enabled,
)
from .service import MonitorBusy, MonitorService, unit_key


database = Database(settings.database)
quote_service = QuoteService(database, settings)
notification_service = NotificationService(settings)
monitor_service = MonitorService(database, None, notification_service)
scheduler = AsyncIOScheduler(timezone=settings.timezone)


# ---------------------------------------------------------------------- 定时任务

def quote_time_reached(now: datetime | None = None) -> bool:
    now = now or datetime.now().astimezone()
    return (now.hour, now.minute) >= (settings.quote_hour, settings.quote_minute)


async def backfill_deductions() -> None:
    """升级后首次启动时，给已有的最新报价补识别扣费项；失败不影响服务。"""
    await asyncio.sleep(5)
    try:
        await quote_service.backfill_deductions()
    except Exception:
        pass


async def refresh_quotes_if_stale() -> None:
    """每天同步一次；启动时仅补同步当天尚未成功的分类。"""
    if quote_time_reached():
        await quote_service.sync_if_stale()


def quote_refresh_trigger() -> CronTrigger:
    return CronTrigger(hour=settings.quote_hour, minute=settings.quote_minute, timezone=settings.timezone)


@asynccontextmanager
async def _monitor_lifespan(_: FastAPI):
    database.initialize()
    monitor_service.close_interrupted_runs()
    if proxy_enabled(database):
        monitor_service.proxy.configure(proxy_clients(database))
        try:
            monitor_service.proxy.start()
        except (RuntimeError, OSError):
            pass  # The device panel reports optional proxy startup failures.
    scheduler.add_job(
        refresh_quotes_if_stale,
        quote_refresh_trigger(),
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
    backfill = asyncio.create_task(backfill_deductions())
    try:
        yield
    finally:
        startup.cancel()
        backfill.cancel()
        scheduler.shutdown(wait=False)
        await asyncio.to_thread(monitor_service.proxy.stop)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Acquire before initializing/closing old runs or starting the scheduler.
    # A second bind address must not mark the first process's active run interrupted.
    with monitor_instance(database.path):
        async with _monitor_lifespan(app):
            yield


app = FastAPI(title="闲鱼报价捡漏监控", version="0.3.4", lifespan=lifespan)
web_dir = ROOT / "web"
app.mount("/assets", StaticFiles(directory=web_dir), name="assets")


class DeviceSelectionPayload(BaseModel):
    ids: list[str]


@app.get("/api/devices")
async def devices() -> dict[str, Any]:
    selected = selected_devices(database)
    found = await asyncio.to_thread(discover_devices)
    result = []
    for device in found:
        channel = device_channel(database, device.id)
        result.append({**device.as_dict(device.id in selected), "collection_channel": channel["channel"],
                       "proxy_client_ip": channel["client_ip"],
                       "proxy_status": monitor_service.proxy.status(channel["client_ip"])})
    return {"devices": result, "selected": selected}


@app.get("/api/devices/{device_id}/screenshot")
async def device_screenshot(device_id: str) -> Response:
    found = await asyncio.to_thread(discover_devices)
    device = next((value for value in found if value.id == device_id and value.kind == "ios"), None)
    if device is None:
        raise HTTPException(404, "未找到这台iPhone，请刷新设备列表")
    if not device.connected:
        raise HTTPException(409, f"iPhone{device.connection_status or '未连接'}，请处理后刷新设备列表")
    try:
        data = await asyncio.to_thread(capture_screenshot, device.udid)
    except ScreenshotBusy as exc:
        raise HTTPException(429, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    return Response(data, media_type="image/png", headers={
        "Cache-Control": "no-store, private", "X-Captured-At": utc_now(),
        "X-Content-Type-Options": "nosniff",
    })


class DeviceChannelPayload(BaseModel):
    channel: Literal["ui", "proxy"]
    client_ip: str | None = Field(default=None, max_length=80)


@app.put("/api/devices/{device_id}/channel")
async def change_device_channel(device_id: str, payload: DeviceChannelPayload) -> dict[str, Any]:
    try:
        channel = await asyncio.to_thread(set_device_channel, database, device_id, payload.channel, payload.client_ip)
        monitor_service.proxy.configure(proxy_clients(database))
        if channel["channel"] == "proxy":
            monitor_service.proxy.start()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(503, str(exc)) from exc
    return {**channel, "next_run": monitor_service.busy,
            "proxy_status": monitor_service.proxy.status(channel["client_ip"])}


@app.get("/api/proxy/certificate")
async def proxy_certificate() -> FileResponse:
    path = monitor_service.proxy.certificate
    if not path.is_file():
        raise HTTPException(404, "请先在设备列表启用代理，等待证书准备完成")
    return FileResponse(path, media_type="application/x-x509-ca-cert", filename="goofish-proxy-ca.cer")


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
    for name in ("app.js", "smart-ui.js", "styles.css"):
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
        "stopping": monitor_service.stopping,
        "live": monitor_service.live,
        "last_run": monitor_service._run_dict(last_run) if last_run else None,
        "collector_mode": "iphone",
        "smart": monitor_service.smart.public_config(),
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
    if not rules.enabled:
        monitor_service.request_stop()
    return _rules_view(rules)


def _monitor_state(rules: Rules) -> dict[str, Any]:
    return {"rules": _rules_view(rules), "busy": monitor_service.busy,
            "stopping": monitor_service.stopping}


@app.post("/api/monitor/start")
async def start_monitor() -> dict[str, Any]:
    if monitor_service.stopping:
        raise HTTPException(409, "当前搜索正在终止，请等待退出后再开始监控")
    return _monitor_state(save_rules(database, replace(load_rules(database), enabled=True)))


@app.post("/api/monitor/stop")
async def stop_monitor() -> dict[str, Any]:
    rules = save_rules(database, replace(load_rules(database), enabled=False))
    monitor_service.request_stop()
    return _monitor_state(rules)


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
    """勾选机型启用其全部容量；带 storage 时只切换该容量。系列 / 机型状态由容量推导。"""
    rules = set_targets_enabled(database, load_rules(database), payload.enabled, model=model, storage=payload.storage)
    save_rules(database, rules)
    return {"model": model, "storage": payload.storage, "enabled": payload.enabled}


@app.put("/api/target-series/{series}")
async def update_target_series(series: int, payload: TargetPayload) -> dict[str, Any]:
    """勾选系列启用其全部机型与容量；取消勾选停用全部容量（之后仍可单独勾选子项）。"""
    rules = set_targets_enabled(database, load_rules(database), payload.enabled, series=series)
    save_rules(database, rules)
    return {"series": series, "enabled": payload.enabled}


@app.post("/api/search/retry")
async def retry_search() -> dict[str, Any]:
    try:
        return await monitor_service.retry_failed()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/targets/{model}/run")
async def run_target(model: str, storage: str | None = None) -> dict[str, Any]:
    """立即用一台空闲设备搜索一个单元；按容量拆分时需要指定 storage。"""
    target = next(
        (item for item in search_targets(database, load_rules(database)) if item["model"] == model), None
    )
    if not target:
        raise HTTPException(404, "报价表中没有该机型")
    if storage and storage not in target["storages"]:
        raise HTTPException(404, f"报价表中没有 {model} {storage}")
    try:
        return await monitor_service.run_model(model, storage, trigger="manual")
    except MonitorBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


class ObservationPayload(BaseModel):
    description: str = Field(default="", max_length=50000)
    detail_parameters: list[str] = Field(default_factory=list)
    seller_name: str = ""
    is_fish_shop: bool | None = None
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
    try:
        return await monitor_service.run_model(
            model,
            storage,
            trigger="inject",
            injected_items=[item.model_dump(exclude_none=True) for item in payload],
            region_applied=region_applied,
            filter_applied=filter_applied,
        )
    except MonitorBusy as exc:
        raise HTTPException(409, str(exc)) from exc


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


@app.post("/api/quotes/reparse")
async def reparse_quotes() -> list[dict[str, Any]]:
    """对最新的无保 / 有保原图重新识别价格和扣费项（手动校正过的价格保持不变）。"""
    return await quote_service.reparse_latest()


@app.get("/api/quotes/deductions")
async def quote_deductions(model: str = Query(min_length=1, max_length=40)) -> dict[str, Any]:
    """估价用：该机型在最新报价里的扣费项，按无保 / 有保分开。"""
    return latest_deductions(database, model)


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
        clauses.append("r.ok=0 AND COALESCE(r.error, '') != ?")
        params.append(STOP_REASON)
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
    return [_with_live(monitor_service._run_dict(row)) for row in rows]


def _with_live(run: dict[str, Any]) -> dict[str, Any]:
    """正在进行的搜索附上当前步骤（如「读取 256G 第 2/3 屏」）。"""
    live = monitor_service.live_for(run.get("id"))
    if live and run.get("ok") is None:
        run["live"] = {key: live[key] for key in ("stage", "observed", "matched")}
    return run


@app.get("/api/run-groups")
async def run_groups(
    status: str | None = Query(default=None, pattern="^(hit|failed|warn)$"),
) -> list[dict[str, Any]]:
    """按机型汇总记录；命中按累计筛选，失败与警告按最近一轮筛选。"""
    with database.connect() as connection:
        rows = connection.execute(f"""
            WITH runs AS ({_RUN_COUNTS}), grouped AS (
            SELECT model, MAX(id) AS latest_id,
                COUNT(*) AS run_count, SUM(MAX(item_count, observed)) AS item_count,
                SUM(hit_count) AS hit_count, SUM(new_count) AS new_count,
                SUM(ok IS NULL) AS running_count, SUM(ok=0 AND COALESCE(error, '') != ?) AS failed_count,
                SUM(ok=1 AND error IS NOT NULL) AS warn_count
            FROM runs GROUP BY model
            )
            SELECT grouped.*, latest.started_at,
                CASE WHEN latest.ok IS NULL THEN 'running'
                     WHEN latest.error=? THEN 'stopped'
                     WHEN latest.ok=0 THEN 'failed'
                     WHEN latest.error IS NOT NULL THEN 'warn'
                     ELSE 'completed' END AS latest_status
            FROM grouped JOIN search_runs latest ON latest.id=grouped.latest_id
            ORDER BY grouped.latest_id DESC
        """, (STOP_REASON, STOP_REASON)).fetchall()
    groups = rows_as_dicts(rows)
    if status == "hit":
        return [group for group in groups if group["hit_count"]]
    if status in {"failed", "warn"}:
        return [group for group in groups if group["latest_status"] == status]
    return groups


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: int) -> dict[str, Any]:
    """一次搜索的完整明细：看到的每件商品、属于哪一轮、判价结论。命中的排在最前。"""
    with database.connect() as connection:
        row = connection.execute(f"{_RUN_COUNTS} WHERE r.id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "搜索记录不存在")
        items = rows_as_dicts(
            connection.execute(
                """SELECT r.*, a.status AS assessment_status, a.notification_status AS assessment_notification,
                    a.result_json AS assessment_result FROM run_items r LEFT JOIN smart_assessments a ON a.id=r.assessment_id
                    WHERE r.run_id=? ORDER BY r.matched DESC, r.position""",
                (run_id,),
            ).fetchall()
        )
    codes: dict[str, int] = {}
    for item in items:
        item["assessment_result"] = json.loads(item["assessment_result"]) if item.get("assessment_result") else None
        saved_valuation = item.pop("valuation_json", None)
        item["valuation"] = json.loads(saved_valuation) if saved_valuation else None
        codes[item["code"] or "other"] = codes.get(item["code"] or "other", 0) + 1
    return {**_with_live(monitor_service._run_dict(row)), "items": items, "codes": codes}


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


# Smart estimation uses only detail text. Credentials are never returned by these endpoints.
from .smart import SmartConfig, Facts, Policy, policies_for, policy_templates_for, evidence_text


class SmartConfigPayload(SmartConfig):
    api_key: str | None = Field(default=None, max_length=1000, exclude=True)


@app.get("/api/smart/config")
async def smart_config():
    return monitor_service.smart.public_config()


@app.put("/api/smart/config")
async def save_smart_config(payload: SmartConfigPayload):
    try:
        return monitor_service.smart.save_config(SmartConfig.model_validate(payload.model_dump()), payload.api_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/smart/test")
async def test_smart_connection():
    try:
        await monitor_service.smart.request_facts(
            {"title": "模型连接测试", "price": 5000, "description": "iPhone 17，256G，个人自用整机，售价5000元。"}, monitor_service.smart.config())
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True}


@app.get("/api/smart/policies")
async def smart_policies(model: str = Query(min_length=1, max_length=40)):
    rules = load_rules(database)
    return {"model": model, "category": rules.quote_category,
            "items": policies_for(database, model, rules.quote_category)}


class PolicyUpdate(BaseModel):
    rule_key: str = Field(min_length=64, max_length=64)
    policy: Policy | None = None


def validate_policy(policy: Policy, has_amount: bool) -> None:
    if not policy.reviewed or policy.action == "ignore":
        return
    if not policy.value.strip():
        raise HTTPException(422, "已确认规则的触发值不能为空")
    if policy.operator in {"lt", "le", "gt", "ge"}:
        import math
        try:
            valid = math.isfinite(float(policy.value))
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(422, "数值比较条件需要有限数字")
    if policy.action == "deduct" and not has_amount:
        raise HTTPException(422, "没有金额的备注不能直接扣费，请选择降档或忽略")


@app.get("/api/smart/policy-templates")
async def smart_policy_templates():
    category = load_rules(database).quote_category
    return {"category": category, "category_name": APPLE_PHONE_CATEGORIES[category],
            **policy_templates_for(database, category)}


class TemplatePolicyUpdate(BaseModel):
    template_key: str = Field(min_length=64, max_length=64)
    policy: Policy | None = None


@app.put("/api/smart/policy-templates")
async def save_policy_templates(payload: list[TemplatePolicyUpdate]):
    category = load_rules(database).quote_category
    current = {row["template_key"]: row for row in policy_templates_for(database, category)["items"]}
    saved = database.state_get("smart_policy_templates", {})
    for update in payload:
        if update.template_key not in current:
            raise HTTPException(409, "报价类别或备注已更新，请刷新后重新确认")
        if update.policy is None:
            saved.pop(update.template_key, None)
            continue
        validate_policy(update.policy, current[update.template_key]["has_amount"])
        saved[update.template_key] = update.policy.model_copy(update={"automatic": False}).model_dump()
    database.state_set("smart_policy_templates", saved)
    return await smart_policy_templates()


@app.put("/api/smart/policies")
async def save_smart_policies(payload: list[PolicyUpdate], model: str = Query(min_length=1, max_length=40)):
    current = {row["rule_key"]: row for row in policies_for(database, model, load_rules(database).quote_category)}
    saved = database.state_get("smart_policies", {})
    for update in payload:
        if update.rule_key not in current:
            raise HTTPException(409, "报价备注已更新，请刷新后重新确认")
        policy = update.policy
        if policy is None:
            saved.pop(update.rule_key, None)
            continue
        validate_policy(policy, current[update.rule_key]["amount_min"] is not None)
        inherited = current[update.rule_key]
        if (inherited["policy_source"] != "model" and policy.model_dump(exclude={"automatic"})
                == Policy.model_validate(inherited["policy"]).model_dump(exclude={"automatic"})):
            saved.pop(update.rule_key, None)
            continue
        saved[update.rule_key] = policy.model_copy(update={"automatic": False}).model_dump()
    database.state_set("smart_policies", saved)
    return await smart_policies(model)


@app.get("/api/smart/assessments")
async def smart_assessments(status: str = "", limit: int = Query(default=100, ge=1, le=500)):
    if status and status not in {"matched", "pending", "rejected", "analysis_failed"}:
        raise HTTPException(422, "无效的分析状态")
    clause = "AND a.status=?" if status else ""
    with database.connect() as c:
        rows = c.execute(f"""SELECT a.* FROM smart_assessments a
            WHERE a.id=(SELECT MAX(b.id) FROM smart_assessments b WHERE b.item_id=a.item_id)
            {clause} ORDER BY a.id DESC LIMIT ?""", ([status, limit] if status else [limit])).fetchall()
    values = [monitor_service.smart.row(row) for row in rows]
    if status in {"pending", "matched"}:
        # Hide old invalid candidates without rewriting their historical decisions.
        values = [value for value in values if has_model_evidence(value["result"].get("model") or "", item_text(value["snapshot"]))]
    return values


@app.get("/api/smart/assessments/{record_id}")
async def smart_assessment(record_id: int):
    try:
        return monitor_service.smart.get(record_id)
    except KeyError:
        raise HTTPException(404, "分析记录不存在") from None


@app.post("/api/smart/assessments/{record_id}/review")
async def review_smart_assessment(record_id: int):
    await smart_assessment(record_id)
    with database.connect() as c:
        c.execute("UPDATE smart_assessments SET reviewed=1 WHERE id=?", (record_id,))
    return await smart_assessment(record_id)


async def rerun_smart(record_id: int, manual: Facts | None = None):
    if monitor_service.busy:
        raise HTTPException(409, "当前正在采集或分析，请稍后重试")
    record = await smart_assessment(record_id)
    if not monitor_service.smart.config().enabled:
        raise HTTPException(409, "请先启用智能估价（可以使用影子模式）")
    async with monitor_service._lock:
        snapshot = record["snapshot"]
        if manual:
            try:
                manual.verify(evidence_text(snapshot))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        rules = load_rules(database)
        updated = await monitor_service.smart.assess(snapshot, rules, snapshot["context_model"],
                    snapshot["context_storage"], snapshot.get("region_applied", False), force=True, manual=manual)
        await monitor_service.publish_smart(updated, rules)
        return monitor_service.smart.get(updated["id"])


@app.post("/api/smart/assessments/{record_id}/reanalyze")
async def reanalyze_smart_assessment(record_id: int):
    return await rerun_smart(record_id)


@app.put("/api/smart/assessments/{record_id}/correction")
async def correct_smart_assessment(record_id: int, payload: Facts):
    return await rerun_smart(record_id, payload)
