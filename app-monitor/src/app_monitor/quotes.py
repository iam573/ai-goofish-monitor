from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
from PIL import Image

from .config import ROOT, Settings
from .db import Database, utc_now
from .model_names import normalize_model
from .swift_runtime import run_swift

logger = logging.getLogger(__name__)

APPLE_NO_WARRANTY_CATEGORY = "100025"
APPLE_WARRANTY_CATEGORY = "100027"
APPLE_PHONE_CATEGORIES = {
    APPLE_NO_WARRANTY_CATEGORY: "苹果无保",
    APPLE_WARRANTY_CATEGORY: "苹果有保",
}
GRADE_COLUMNS = {
    "靓机": (0.29, 0.39),
    "小花": (0.39, 0.475),
    "大花": (0.475, 0.56),
    "外爆": (0.56, 0.645),
    "内爆可测": (0.645, 0.73),
}


@dataclass(frozen=True)
class QuoteLayout:
    """报价图的列位置（按图片宽度归一化的左右边界）。"""

    name: str
    capacity: tuple[float, float]
    grades: dict[str, tuple[float, float]]
    deductions: tuple[tuple[float, float], ...]


# 无保（过保机）：容量 | 靓机 | 小花 | 大花 | 外爆 | 内爆可测 | 扣费项（两小列）
NO_WARRANTY_LAYOUT = QuoteLayout(
    name="no_warranty",
    capacity=(0.215, 0.29),
    grades=GRADE_COLUMNS,
    deductions=((0.73, 0.865), (0.865, 1.0)),
)
# 有保（在保机）多一档「高保充新」，其余各档整体右移一列，扣费在「颜色差价」「备注扣费」两列。
# 「高保充新」目前不入库（0.286–0.372 这一列直接跳过）。
WARRANTY_LAYOUT = QuoteLayout(
    name="warranty",
    capacity=(0.21, 0.286),
    grades={
        "靓机": (0.372, 0.456),
        "小花": (0.456, 0.541),
        "大花": (0.541, 0.625),
        "外爆": (0.625, 0.71),
        "内爆可测": (0.71, 0.787),
    },
    deductions=((0.787, 0.871), (0.871, 1.0)),
)


def detect_layout(tokens: Iterable[OCRToken]) -> QuoteLayout:
    """表头里出现「高保充新」的是有保版式，否则按无保版式。"""
    return WARRANTY_LAYOUT if any("高保" in token.text for token in tokens) else NO_WARRANTY_LAYOUT


@dataclass(frozen=True)
class QuoteDeduction:
    model: str
    column: int
    position: int
    text: str
    name: str
    amount_min: float | None = None
    amount_max: float | None = None
    open_ended: bool = False
    kind: str = "deduct"  # deduct：可勾选扣费；note：只是说明（如「后盖破当大花收」）


_DASH = re.compile(r"[－—–~～]")
_HAS_AMOUNT = re.compile(r"-\d+|扣\d+")
_STORAGE_SPEC = re.compile(r"(?:64|128|256|512|1T|2T)(?:G|GB)?(?:/(?:64|128|256|512|1T|2T)(?:G|GB)?)*", re.I)
_DEDUCTION_HEADERS = {"扣费项", "颜色差价", "备注扣费", "扣费"}


def normalize_deduction_text(text: str) -> str:
    """Correct the battery-note OCR error verified against the source chart."""
    return re.sub(r"(电池(?:低于|小于|不足|[<＜])\s*\d{2,3}\s*[%％]?\s*按)\s*3\s*单电", r"\1弹电", text)


def parse_deduction_text(text: str) -> tuple[str, float | None, float | None, bool, str]:
    """「港澳版-600-800」→ (港澳版, 600, 800, False, deduct)；「后盖破当大花收」→ note。"""
    clean = _DASH.sub("-", re.sub(r"\s+", "", normalize_deduction_text(text))).strip("，,;；")
    match = re.fullmatch(r"(?P<name>.+?)-(?P<a>\d+)(?:-(?P<b>\d+))?(?P<more>以上|起)?", clean)
    if match and match.group("name").strip("-"):
        low = float(match.group("a"))
        high = float(match.group("b")) if match.group("b") else low
        return match.group("name").strip("-"), min(low, high), max(low, high), bool(match.group("more")), "deduct"
    match = re.fullmatch(r"(?P<name>.+?)扣(?P<a>\d+)", clean)
    if match:
        amount = float(match.group("a"))
        return match.group("name"), amount, amount, False, "deduct"
    return clean, None, None, False, "note"


def merge_wrapped_lines(lines: list[OCRToken], max_gap: float = 30) -> list[str]:
    """把被单元格宽度折开的扣费项拼回去，例如「后三等级电池低于」+「95%-300」。"""
    merged: list[tuple[str, float]] = []
    for token in sorted(lines, key=lambda item: item.global_y):
        text = re.sub(r"\s+", "", token.text)
        if not text:
            continue
        if merged:
            previous, previous_y = merged[-1]
            close = token.global_y - previous_y <= max_gap
            has_amount = bool(_HAS_AMOUNT.search(_DASH.sub("-", previous)))
            continues = (
                previous.endswith(("-", "－", "—"))  # 「银色黑色-」+「200」
                or (not has_amount and re.match(r"^[\d%\-－—]", text))  # 「电池低于」+「95%-300」
                or _STORAGE_SPEC.fullmatch(previous)  # 「512/1T/2T」+「银色黑色-200」
            )
            if close and continues:
                merged[-1] = (previous + text, token.global_y)
                continue
        merged.append((text, token.global_y))
    return [text for text, _ in merged]


@dataclass(frozen=True)
class OCRToken:
    text: str
    x: float
    global_y: float
    width: float = 0.0


@dataclass(frozen=True)
class QuoteEntry:
    model: str
    storage: str
    grade: str
    price: float


def normalize_storage(value: str) -> str | None:
    text = value.upper().replace(" ", "").replace("IT", "1T")
    text = text.replace("O", "0") if len(text) <= 4 else text
    match = re.fullmatch(r"(64|128|256|512|1T|2T)(?:G|GB)?", text)
    return match.group(1) if match else None




class QuoteImageParser:
    """使用 macOS Vision OCR 的坐标结果解析报价表。"""

    slice_height = 1250
    slice_step = 1200

    def __init__(self, script: Path | None = None, sdk: str = ""):
        self.script = script or ROOT / "scripts" / "vision_ocr.swift"
        self.sdk = sdk

    def parse_image(self, image_path: Path) -> tuple[list[QuoteEntry], list[QuoteDeduction]]:
        """一次识别同时得到价格和扣费项。"""
        tokens = self._recognize(image_path)
        layout = detect_layout(tokens)
        return self.parse_tokens(tokens, layout), self.parse_deductions(tokens, layout)

    def parse(self, image_path: Path) -> list[QuoteEntry]:
        tokens = self._recognize(image_path)
        return self.parse_tokens(tokens)

    def _recognize(self, image_path: Path) -> list[OCRToken]:
        with Image.open(image_path) as source:
            width, height = source.size
            slices: list[tuple[Path, int, int]] = []
            with tempfile.TemporaryDirectory(prefix="quote-ocr-") as temp:
                temp_path = Path(temp)
                for index, top in enumerate(range(0, height, self.slice_step)):
                    bottom = min(top + self.slice_height, height)
                    crop_path = temp_path / f"slice-{index}.png"
                    source.crop((0, top, width, bottom)).save(crop_path)
                    slices.append((crop_path, top, bottom - top))

                result = run_swift(self.script, [str(item[0]) for item in slices], sdk=self.sdk, timeout=180)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or "Vision OCR 执行失败")

                tokens: list[OCRToken] = []
                for line in result.stdout.splitlines():
                    parts = line.split("\t", 5)
                    if len(parts) != 6:
                        continue
                    index = int(parts[0])
                    x, y, _width, box_height = map(float, parts[1:5])
                    text = parts[5].strip()
                    _, top, crop_height = slices[index]
                    center_y = top + (1 - (y + box_height / 2)) * crop_height
                    tokens.append(OCRToken(text=text, x=x, global_y=center_y, width=_width))
        return self._deduplicate(tokens)

    @staticmethod
    def _deduplicate(tokens: list[OCRToken]) -> list[OCRToken]:
        result: list[OCRToken] = []
        for token in sorted(tokens, key=lambda item: (item.global_y, item.x)):
            duplicate = any(
                token.text == current.text
                and abs(token.x - current.x) < 0.015
                and abs(token.global_y - current.global_y) < 10
                for current in result[-40:]
            )
            if not duplicate:
                result.append(token)
        return result

    def parse_tokens(
        self, tokens: Iterable[OCRToken], layout: QuoteLayout | None = None
    ) -> list[QuoteEntry]:
        values = list(tokens)
        layout = layout or detect_layout(values)
        rows = self._capacity_rows(values, layout)
        if not rows:
            return []

        entries: list[QuoteEntry] = []
        for capacity_token, storage, model in rows:
            for grade, (left, right) in layout.grades.items():
                candidates = [
                    token
                    for token in values
                    if left <= token.x < right
                    and abs(token.global_y - capacity_token.global_y) <= 24
                    and re.fullmatch(r"\d{2,5}", token.text.replace(" ", ""))
                ]
                if not candidates:
                    continue
                price_token = min(
                    candidates, key=lambda token: abs(token.global_y - capacity_token.global_y)
                )
                price = float(price_token.text.replace(" ", ""))
                if 20 <= price <= 100000:
                    entries.append(
                        QuoteEntry(
                            model=normalize_model(model.text),
                            storage=storage or "",
                            grade=grade,
                            price=price,
                        )
                    )

        unique: dict[tuple[str, str, str], QuoteEntry] = {}
        for entry in entries:
            unique[(entry.model, entry.storage, entry.grade)] = entry
        return list(unique.values())

    def _capacity_rows(
        self, values: list[OCRToken], layout: QuoteLayout
    ) -> list[tuple[OCRToken, str, OCRToken]]:
        """每一行容量及其所属机型（取纵向最近的机型名）。"""
        left, right = layout.capacity
        capacity_rows = [
            (token, normalize_storage(token.text))
            for token in values
            if left <= token.x <= right and normalize_storage(token.text)
        ]
        models = self._model_tokens(values)
        if not capacity_rows or not models:
            return []
        return [
            (token, storage or "", min(models, key=lambda model: abs(model.global_y - token.global_y)))
            for token, storage in capacity_rows
        ]

    def parse_deductions(
        self, tokens: Iterable[OCRToken], layout: QuoteLayout | None = None
    ) -> list[QuoteDeduction]:
        """读取每款机型右侧的扣费项。

        先用该机型各容量行的纵向范围圈出它的区块，再把扣费列里落在区块内的文字
        按列、按从上到下的顺序拼接折行并拆成「名称 + 金额 / 区间」。
        """
        values = list(tokens)
        layout = layout or detect_layout(values)
        rows = self._capacity_rows(values, layout)
        if not rows:
            return []
        blocks: dict[int, dict[str, Any]] = {}
        for token, _, model in rows:
            block = blocks.setdefault(id(model), {"model": normalize_model(model.text), "ys": []})
            block["ys"].append(token.global_y)
        ordered = sorted(blocks.values(), key=lambda block: min(block["ys"]))
        gaps = sorted(
            b - a for block in ordered for a, b in zip(sorted(block["ys"]), sorted(block["ys"])[1:])
        )
        pad = (gaps[len(gaps) // 2] if gaps else 60) * 0.75
        for index, block in enumerate(ordered):
            block["top"] = min(block["ys"]) - pad
            block["bottom"] = max(block["ys"]) + pad
            if index + 1 < len(ordered):  # 区块之间留空的部分各归一半
                block["bottom"] = min(block["bottom"], (max(block["ys"]) + min(ordered[index + 1]["ys"])) / 2)
            if index:
                block["top"] = max(block["top"], (min(block["ys"]) + max(ordered[index - 1]["ys"])) / 2)

        grouped: dict[tuple[int, int], list[OCRToken]] = {}
        for token in values:
            if any(header in token.text for header in _DEDUCTION_HEADERS):
                continue
            column = next(
                (i for i, (left, right) in enumerate(layout.deductions) if left - 0.005 <= token.x < right),
                None,
            )
            if column is None:
                continue
            owner = next(
                (i for i, block in enumerate(ordered) if block["top"] <= token.global_y <= block["bottom"]),
                None,
            )
            if owner is not None:
                grouped.setdefault((owner, column), []).append(token)

        result: list[QuoteDeduction] = []
        for (owner, column), lines in sorted(grouped.items()):
            model = ordered[owner]["model"]
            for position, text in enumerate(merge_wrapped_lines(lines)):
                text = normalize_deduction_text(text)
                name, low, high, more, kind = parse_deduction_text(text)
                result.append(QuoteDeduction(model, column, position, text, name, low, high, more, kind))
        # 同一机型可能占多个区块（报价表分段重复表头），按内容去重
        unique: dict[tuple[str, str], QuoteDeduction] = {}
        for item in result:
            unique.setdefault((item.model, item.text), item)
        return list(unique.values())

    @staticmethod
    def _model_tokens(tokens: list[OCRToken]) -> list[OCRToken]:
        ignored = {"型号", "成色", "成色说明", "容量"}
        raw = [
            token
            for token in tokens
            if token.x < 0.115
            and token.text not in ignored
            and re.search(r"(?:\d|\bX[RS]?\b|SE)", token.text, re.I)
            and len(token.text) <= 20
        ]
        suffixes = [
            token
            for token in tokens
            if token.x < 0.115 and token.text.upper().strip() in {"MAX", "PRO MAX"}
        ]
        merged: list[OCRToken] = []
        used_suffixes: set[int] = set()
        for base in raw:
            nearby = [
                (index, suffix)
                for index, suffix in enumerate(suffixes)
                if index not in used_suffixes
                and 5 <= abs(suffix.global_y - base.global_y) <= 45
            ]
            if "PRO" in base.text.upper() and nearby:
                index, suffix = min(
                    nearby, key=lambda item: abs(item[1].global_y - base.global_y)
                )
                used_suffixes.add(index)
                merged.append(
                    OCRToken(
                        text=f"{base.text} MAX",
                        x=base.x,
                        global_y=(base.global_y + suffix.global_y) / 2,
                    )
                )
            else:
                merged.append(base)
        return merged


class QuoteService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.parser = QuoteImageParser(sdk=settings.swift_sdk)
        self._lock = asyncio.Lock()

    async def categories(self) -> list[dict[str, str]]:
        payload = await self._get_json(f"{self.settings.quote_api_root}/categoryTree")
        leaves: list[dict[str, str]] = []

        def visit(nodes: Any) -> None:
            if not isinstance(nodes, list):
                return
            for node in nodes:
                children = node.get("quoteCategoryList")
                if children:
                    visit(children)
                elif node.get("status") == 1:
                    leaves.append(
                        {
                            "category_id": str(node["categoryId"]),
                            "category_name": str(node["categoryName"]),
                        }
                    )

        visit(payload.get("data"))
        return leaves

    async def sync(self, category_id: str, category_name: str | None = None) -> dict[str, Any]:
        # Startup refresh, scheduled checks and manual sync share this service.
        # Wait for an active parse rather than reporting its half-finished row as unchanged.
        async with self._lock:
            return await self._sync(category_id, category_name)

    async def _sync(self, category_id: str, category_name: str | None = None) -> dict[str, Any]:
        payload = await self._get_json(
            f"{self.settings.quote_api_root}/getQuoteImage", params={"categoryId": category_id}
        )
        image_url = payload.get("data")
        parsed = urlparse(image_url if isinstance(image_url, str) else "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("报价接口没有返回有效的 HTTPS 图片地址")

        image = await self._download(image_url)
        digest = hashlib.sha256(image).hexdigest()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM quote_batches WHERE category_id=? AND image_sha256=?",
                (category_id, digest),
            ).fetchone()

        fetched_at = utc_now()
        quote_date = self._date_from_url(image_url) or fetched_at[:10]
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".png"
        directory = self.settings.quote_dir / category_id / quote_date
        directory.mkdir(parents=True, exist_ok=True)
        image_path = directory / f"{digest[:16]}{suffix}"
        image_path.write_bytes(image)

        if existing:
            # A moved installation may still contain the previous machine's absolute path.
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE quote_batches SET image_path=?, image_url=?, quote_date=?, fetched_at=? WHERE id=?",
                    (str(image_path), image_url, quote_date, fetched_at, existing["id"]),
                )
            count = self._entry_count(existing["id"])
            if existing["parse_status"] == "parsed" and count > 0:
                batch = {**dict(existing), "image_path": str(image_path), "image_url": image_url,
                         "quote_date": quote_date, "fetched_at": fetched_at}
                logger.info("报价图片未变化且已有有效报价 category=%s batch_id=%s entries=%s", category_id, existing["id"], count)
                return {"updated": False, "batch": batch, "batch_id": existing["id"],
                        "category_name": existing["category_name"], "entry_count": count, "parse_status": "parsed"}
            logger.info("报价图片未变化，重试未完成解析 category=%s batch_id=%s status=%s entries=%s",
                        category_id, existing["id"], existing["parse_status"], count)
            result = await self._reparse(int(existing["id"]))
            return {**result, "updated": True, "reparsed": True,
                    "category_name": existing["category_name"], "image_url": image_url, "image_path": str(image_path)}

        resolved_name = category_name or category_id
        if not category_name:
            try:
                resolved_name = next(
                    item["category_name"]
                    for item in await self.categories()
                    if item["category_id"] == category_id
                )
            except (StopIteration, httpx.HTTPError):
                pass

        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO quote_batches(
                    category_id, category_name, quote_date, image_url, image_path,
                    image_sha256, fetched_at, parse_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'parsing')
                """,
                (
                    category_id,
                    resolved_name,
                    quote_date,
                    image_url,
                    str(image_path),
                    digest,
                    fetched_at,
                ),
            )
            batch_id = cursor.lastrowid

        result = await self._reparse(int(batch_id))
        return {
            **result,
            "updated": True,
            "category_name": resolved_name,
            "image_url": image_url,
            "image_path": str(image_path),
        }

    async def reparse(self, batch_id: int) -> dict[str, Any]:
        """对已下载的报价原图重新识别价格和扣费项；手动校正过的价格保持不变。"""
        async with self._lock:
            return await self._reparse(batch_id)

    async def _reparse(self, batch_id: int) -> dict[str, Any]:
        with self.database.connect() as connection:
            batch = connection.execute("SELECT * FROM quote_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError("报价批次不存在")
        with self.database.connect() as connection:
            connection.execute("UPDATE quote_batches SET parse_status='parsing',parse_error='' WHERE id=?", (batch_id,))
        logger.info("报价识别开始 category=%s batch_id=%s", batch["category_id"], batch_id)
        try:
            image_path = Path(batch["image_path"])
            if not image_path.is_file():
                raise FileNotFoundError(f"报价原图不存在：{image_path}")
            entries, deductions = await asyncio.to_thread(self.parser.parse_image, image_path)
            status = "parsed" if entries else "needs_review"
            error = "" if entries else "图片已下载，但未识别到有效报价，请核对原图或重新识别"
            now = utc_now()
            with self.database.connect() as connection:
                if entries:
                    connection.execute("DELETE FROM quote_entries WHERE batch_id=? AND source='ocr'", (batch_id,))
                    connection.executemany(
                        """INSERT OR IGNORE INTO quote_entries(
                            batch_id, category_id, model, storage, grade, price, source, enabled, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'ocr', 1, ?)""",
                        [(batch_id, batch["category_id"], e.model, e.storage, e.grade, e.price, now) for e in entries],
                    )
                _save_deductions(connection, batch_id, batch["category_id"], deductions)
                connection.execute("UPDATE quote_batches SET parse_status=?,parse_error=? WHERE id=?", (status, error, batch_id))
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute("UPDATE quote_batches SET parse_status='failed',parse_error=? WHERE id=?", (str(exc)[:4000], batch_id))
            logger.exception("报价识别失败 category=%s batch_id=%s", batch["category_id"], batch_id)
            raise
        count = self._entry_count(batch_id)
        logger.info("报价识别结束 category=%s batch_id=%s status=%s parsed=%s entries=%s deductions=%s",
                    batch["category_id"], batch_id, status, len(entries), count, len(deductions))
        return {
            "batch_id": batch_id,
            "category_id": batch["category_id"],
            "entry_count": count,
            "deduction_count": len(deductions),
            "parse_status": status,
            "parse_error": error,
        }

    async def reparse_latest(self) -> list[dict[str, Any]]:
        results = []
        for category_id in APPLE_PHONE_CATEGORIES:
            batch = latest_downloaded_batch(self.database, category_id)
            if not batch:
                continue
            try:
                results.append(await self.reparse(int(batch["id"])))
            except Exception as exc:
                results.append({"category_id": category_id, "batch_id": batch["id"], "error": str(exc)})
        return results

    async def backfill_deductions(self) -> None:
        """升级后第一次启动：给还没有扣费项的最新报价补做一次识别（每个批次只自动尝试一次）。"""
        tried = set(self.database.state_get("deductions_backfill", []))
        for category_id in APPLE_PHONE_CATEGORIES:
            batch = latest_batch(self.database, category_id)
            if not batch or batch["id"] in tried or batch.get("deduction_count"):
                continue
            tried.add(batch["id"])
            self.database.state_set("deductions_backfill", sorted(tried))
            try:
                await self.reparse(int(batch["id"]))
            except Exception:
                continue

    async def sync_all(self, *, only_stale: bool = False, today: str | None = None) -> list[dict[str, Any]]:
        """同步苹果无保与有保两份报价。"""
        results = []
        today = today or utc_now()[:10]
        for category_id, category_name in APPLE_PHONE_CATEGORIES.items():
            batch = latest_downloaded_batch(self.database, category_id)
            if only_stale and batch and is_fresh(batch, today):
                continue
            if results:
                await asyncio.sleep(3)
            try:
                results.append({"category_id": category_id, **await self.sync(category_id, category_name)})
            except Exception as exc:
                results.append({"category_id": category_id, "error": str(exc)})
        if results:
            self.database.state_set("quote_last_check", {"at": utc_now(), "results": results})
        return results

    async def sync_if_stale(self, today: str | None = None) -> list[dict[str, Any]]:
        """仅同步当天尚未成功解析的分类，已同步的分类不再请求接口。"""
        return await self.sync_all(only_stale=True, today=today)

    def _entry_count(self, batch_id: int) -> int:
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM quote_entries WHERE batch_id=?", (batch_id,)
                ).fetchone()[0]
            )

    async def _get_json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    response = await client.get(url, params={
                        "appid": self.settings.quote_app_id,
                        "platform": self.settings.quote_platform,
                        **(params or {}),
                    }, headers={
                        "User-Agent": "GoofishAppMonitor/0.1",
                        "wzw-app-id": self.settings.quote_app_id,
                        "wzw-client-type": self.settings.quote_platform,
                    })
                    response.raise_for_status()
                    payload = response.json()
                if payload.get("code") == 200:
                    return payload
                raise RuntimeError(f"报价接口返回 code={payload.get('code')}: {payload.get('msg')}")
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(delay)
                    delay *= 2
        raise RuntimeError(f"报价接口请求失败：{last_error}")

    async def _download(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "GoofishAppMonitor/0.1"})
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                raise ValueError(f"报价地址返回了非图片内容：{content_type}")
            if len(response.content) < 1024:
                raise ValueError("报价图片内容不完整")
            return response.content

    @staticmethod
    def _date_from_url(url: str) -> str | None:
        match = re.search(r"/(20\d{6})/", url)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
        except ValueError:
            return None



# ---------------------------------------------------------------------------
# 报价查询
# ---------------------------------------------------------------------------

_LATEST_BATCH_SQL = """
    SELECT id FROM quote_batches
    WHERE category_id=? AND parse_status IN ('parsed', 'needs_review')
    ORDER BY fetched_at DESC, id DESC LIMIT 1
"""


def latest_batch(database: Database, category_id: str) -> dict[str, Any] | None:
    with database.connect() as connection:
        row = connection.execute(
            f"""
            SELECT qb.*, (SELECT COUNT(*) FROM quote_entries qe WHERE qe.batch_id=qb.id) AS entry_count,
                   (SELECT COUNT(*) FROM quote_deductions qd WHERE qd.batch_id=qb.id) AS deduction_count
            FROM quote_batches qb WHERE qb.id=({_LATEST_BATCH_SQL})
            """,
            (category_id,),
        ).fetchone()
    return dict(row) if row else None


def latest_downloaded_batch(database: Database, category_id: str) -> dict[str, Any] | None:
    """Status/image views must expose failed or in-progress downloads too."""
    with database.connect() as connection:
        row = connection.execute(
            """SELECT qb.*, (SELECT COUNT(*) FROM quote_entries qe WHERE qe.batch_id=qb.id) AS entry_count,
                   (SELECT COUNT(*) FROM quote_deductions qd WHERE qd.batch_id=qb.id) AS deduction_count
               FROM quote_batches qb WHERE category_id=? ORDER BY fetched_at DESC,id DESC LIMIT 1""",
            (category_id,),
        ).fetchone()
    return dict(row) if row else None


def is_fresh(batch: dict[str, Any], today: str) -> bool:
    """Only a successfully parsed, nonempty batch can satisfy the daily refresh."""
    return (batch.get("parse_status") == "parsed" and int(batch.get("entry_count") or 0) > 0
            and (str(batch.get("fetched_at") or "")[:10] == today or batch.get("quote_date") == today))


def latest_entries(
    database: Database, category_id: str, grade: str | None = None
) -> list[dict[str, Any]]:
    """某个分类最新一批有效报价；可按成色过滤。"""
    params: list[Any] = [category_id, category_id]
    grade_clause = ""
    if grade:
        grade_clause = "AND qe.grade=?"
        params.append(grade)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT qe.id, qe.model, qe.storage, qe.grade, qe.price, qe.source
            FROM quote_entries qe
            WHERE qe.category_id=? AND qe.enabled=1
              AND qe.batch_id=({_LATEST_BATCH_SQL}) {grade_clause}
            ORDER BY qe.model, qe.storage, qe.grade
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def quote_status(database: Database) -> list[dict[str, Any]]:
    values = []
    for category_id, category_name in APPLE_PHONE_CATEGORIES.items():
        batch = latest_downloaded_batch(database, category_id)
        today = datetime.now().astimezone().date().isoformat()
        values.append(
            {
                "fresh": bool(batch) and is_fresh(batch, today),
                "category_id": category_id,
                "category_name": category_name,
                "batch_id": batch.get("id") if batch else None,
                "image_available": bool(batch) and Path(batch["image_path"]).is_file(),
                "quote_date": batch.get("quote_date") if batch else None,
                "fetched_at": batch.get("fetched_at") if batch else None,
                "parse_status": batch.get("parse_status") if batch else None,
                "parse_error": batch.get("parse_error", "") if batch else "",
                "entry_count": batch.get("entry_count", 0) if batch else 0,
                "deduction_count": batch.get("deduction_count", 0) if batch else 0,
            }
        )
    return values


def grouped_apple_products(database: Database) -> list[dict[str, Any]]:
    """按机型 + 容量聚合无保 / 有保报价，成色为二级分类。"""
    products: dict[tuple[str, str], dict[str, Any]] = {}
    for category_id, key in (
        (APPLE_NO_WARRANTY_CATEGORY, "no_warranty"),
        (APPLE_WARRANTY_CATEGORY, "warranty"),
    ):
        for entry in latest_entries(database, category_id):
            product = products.setdefault(
                (entry["model"], entry["storage"]),
                {"model": entry["model"], "storage": entry["storage"], "grades": {}},
            )
            product["grades"].setdefault(entry["grade"], {})[key] = {
                "id": entry["id"],
                "price": entry["price"],
                "source": entry["source"],
            }
    for product in products.values():
        for values in product["grades"].values():
            no_warranty, warranty = values.get("no_warranty"), values.get("warranty")
            values["warranty_premium"] = (
                float(warranty["price"]) - float(no_warranty["price"])
                if no_warranty and warranty
                else None
            )
    return list(products.values())


# ---------------------------------------------------------------------------
# 扣费项
# ---------------------------------------------------------------------------

def _save_deductions(connection: Any, batch_id: int, category_id: str, items: list[QuoteDeduction]) -> None:
    connection.execute("DELETE FROM quote_deductions WHERE batch_id=?", (batch_id,))
    connection.executemany(
        """
        INSERT INTO quote_deductions(
            batch_id, category_id, model, column_index, position, text, name,
            amount_min, amount_max, open_ended, kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (batch_id, category_id, item.model, item.column, item.position, item.text, item.name,
             item.amount_min, item.amount_max, int(item.open_ended), item.kind)
            for item in items
        ],
    )


def latest_deductions(database: Database, model: str) -> dict[str, list[dict[str, Any]]]:
    """某机型在最新无保 / 有保报价中的扣费项。"""
    result: dict[str, list[dict[str, Any]]] = {}
    for category_id, key in ((APPLE_NO_WARRANTY_CATEGORY, "no_warranty"), (APPLE_WARRANTY_CATEGORY, "warranty")):
        batch = latest_batch(database, category_id)
        if not batch:
            result[key] = []
            continue
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, column_index, position, text, name, amount_min, amount_max, open_ended, kind
                FROM quote_deductions WHERE batch_id=? AND model=?
                ORDER BY column_index, position
                """,
                (batch["id"], model),
            ).fetchall()
        result[key] = [dict(row) | {"open_ended": bool(row["open_ended"])} for row in rows]
    return result


_STORAGE_MENTION = re.compile(r"(64|128|256|512|1T|2T)(?:GB|G)?", re.I)


def deduction_storages(name: str) -> list[str]:
    """扣费项名称里提到的容量，例如「512/1T银色黑色」→ ["512", "1T"]；没提到时为空（适用所有容量）。"""
    prefix = re.match(r"^(?:水货)?((?:64|128|256|512|[12]T)(?:GB|G)?(?:/(?:64|128|256|512|[12]T)(?:GB|G)?)*)", str(name), re.I)
    return [match.group(1).upper() for match in _STORAGE_MENTION.finditer(prefix.group(1))] if prefix else []


def quote_reference(
    database: Database, model: str, storage: str, category_id: str, grade: str
) -> dict[str, Any] | None:
    """推送用的完整参考价：该机型容量各成色报价、另一份报价的同成色价、适用的扣费项和说明。"""
    batch = latest_batch(database, category_id)
    if not batch:
        return None
    prices = {
        entry["grade"]: entry["price"]
        for entry in latest_entries(database, category_id)
        if entry["model"] == model and entry["storage"] == storage
    }
    grades = [{"grade": name, "price": prices[name]} for name in GRADE_COLUMNS if name in prices]
    other_id = APPLE_WARRANTY_CATEGORY if category_id == APPLE_NO_WARRANTY_CATEGORY else APPLE_NO_WARRANTY_CATEGORY
    other_price = next(
        (entry["price"] for entry in latest_entries(database, other_id, grade)
         if entry["model"] == model and entry["storage"] == storage),
        None,
    )
    key = "no_warranty" if category_id == APPLE_NO_WARRANTY_CATEGORY else "warranty"
    items = latest_deductions(database, model).get(key, [])
    deductions = []
    for item in items:
        if item["kind"] != "deduct" or item["amount_min"] is None:
            continue
        specs = deduction_storages(item["name"])
        if specs and storage.upper() not in specs:
            continue
        deductions.append({
            "name": item["name"], "amount_min": item["amount_min"],
            "amount_max": item["amount_max"], "open_ended": item["open_ended"],
        })
    return {
        "category_name": APPLE_PHONE_CATEGORIES.get(category_id, category_id),
        "quote_date": batch.get("quote_date"),
        "grade": grade,
        "grades": grades,
        "other_category_name": APPLE_PHONE_CATEGORIES.get(other_id, other_id),
        "other_price": other_price,
        "deductions": deductions,
        "notes": [item["name"] for item in items if item["kind"] != "deduct"],
    }
