from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
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


API_ROOT = "https://api.ajhuishou.com/ajhuishou-product/api/home"
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
class OCRToken:
    text: str
    x: float
    global_y: float


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


def normalize_model(value: str) -> str:
    text = value.upper().strip()
    text = re.sub(r"\bIPHONE\b|苹果", "", text)
    text = text.replace("Ｐ", "P").replace("Ｍ", "M")
    text = re.sub(r"(?<=\d)(?=PRO\b)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\bPRO\s*MAX\b", "Pro Max", text, flags=re.I)
    text = re.sub(r"\bPRO\b", "Pro", text, flags=re.I)
    text = re.sub(r"\bPLUS\b", "Plus", text, flags=re.I)
    text = re.sub(r"\bMINI\b", "Mini", text, flags=re.I)
    text = re.sub(r"\bAIR\b", "Air", text, flags=re.I)
    text = re.sub(r"\bMAX\b", "Max", text, flags=re.I)
    if re.fullmatch(r"\d{1,2}[Ee]", text):
        return text[:-1] + "e"
    return text


class QuoteImageParser:
    """使用 macOS Vision OCR 的坐标结果解析报价表。"""

    slice_height = 1250
    slice_step = 1200

    def __init__(self, script: Path | None = None):
        self.script = script or ROOT / "scripts" / "vision_ocr.swift"

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

                command = ["swift", str(self.script), *[str(item[0]) for item in slices]]
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=180, check=False
                )
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
                    tokens.append(OCRToken(text=text, x=x, global_y=center_y))
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

    def parse_tokens(self, tokens: Iterable[OCRToken]) -> list[QuoteEntry]:
        values = list(tokens)
        capacity_rows = [
            (token, normalize_storage(token.text))
            for token in values
            if 0.215 <= token.x <= 0.29 and normalize_storage(token.text)
        ]
        models = self._model_tokens(values)
        if not capacity_rows or not models:
            return []

        entries: list[QuoteEntry] = []
        for capacity_token, storage in capacity_rows:
            model = min(models, key=lambda token: abs(token.global_y - capacity_token.global_y))
            for grade, (left, right) in GRADE_COLUMNS.items():
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
        self.parser = QuoteImageParser()

    async def categories(self) -> list[dict[str, str]]:
        payload = await self._get_json(f"{API_ROOT}/categoryTree")
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
        payload = await self._get_json(
            f"{API_ROOT}/getQuoteImage", params={"categoryId": category_id}
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
            if existing:
                return {"updated": False, "batch": dict(existing), "entry_count": self._entry_count(existing["id"])}

        fetched_at = utc_now()
        quote_date = self._date_from_url(image_url) or fetched_at[:10]
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".png"
        directory = self.settings.quote_dir / category_id / quote_date
        directory.mkdir(parents=True, exist_ok=True)
        image_path = directory / f"{digest[:16]}{suffix}"
        image_path.write_bytes(image)

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

        try:
            entries = await asyncio.to_thread(self.parser.parse, image_path)
            status = "parsed" if entries else "needs_review"
            now = utc_now()
            with self.database.connect() as connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO quote_entries(
                        batch_id, category_id, model, storage, grade, price,
                        source, enabled, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ocr', 1, ?)
                    """,
                    [
                        (
                            batch_id,
                            category_id,
                            entry.model,
                            entry.storage,
                            entry.grade,
                            entry.price,
                            now,
                        )
                        for entry in entries
                    ],
                )
                connection.execute(
                    "UPDATE quote_batches SET parse_status=? WHERE id=?",
                    (status, batch_id),
                )
        except Exception:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE quote_batches SET parse_status='failed' WHERE id=?", (batch_id,)
                )
            raise

        return {
            "updated": True,
            "batch_id": batch_id,
            "image_url": image_url,
            "image_path": str(image_path),
            "entry_count": len(entries),
            "parse_status": status,
        }

    async def sync_all(self) -> list[dict[str, Any]]:
        """同步苹果无保与有保两份报价。"""
        results = []
        for index, (category_id, category_name) in enumerate(APPLE_PHONE_CATEGORIES.items()):
            if index:
                await asyncio.sleep(3)
            try:
                results.append({"category_id": category_id, **await self.sync(category_id, category_name)})
            except Exception as exc:
                results.append({"category_id": category_id, "error": str(exc)})
        return results

    async def sync_if_stale(self, today: str | None = None) -> list[dict[str, Any]]:
        """只同步今天还没有拿到新报价的分类；供每日定时和启动补偿使用。"""
        today = today or datetime.now().astimezone().date().isoformat()
        results = []
        for category_id, category_name in APPLE_PHONE_CATEGORIES.items():
            latest = latest_batch(self.database, category_id)
            if latest and is_fresh(latest, today):
                results.append({"category_id": category_id, "updated": False, "fresh": True})
                continue
            try:
                results.append({"category_id": category_id, **await self.sync(category_id, category_name)})
            except Exception as exc:
                results.append({"category_id": category_id, "error": str(exc)})
        self.database.state_set("quote_last_check", {"at": utc_now(), "results": results})
        return results

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
                    response = await client.get(
                        url, params=params, headers={"User-Agent": "GoofishAppMonitor/0.1"}
                    )
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
            SELECT qb.*, (SELECT COUNT(*) FROM quote_entries qe WHERE qe.batch_id=qb.id) AS entry_count
            FROM quote_batches qb WHERE qb.id=({_LATEST_BATCH_SQL})
            """,
            (category_id,),
        ).fetchone()
    return dict(row) if row else None


def is_fresh(batch: dict[str, Any], today: str) -> bool:
    """今天拿到过新图片即视为最新；报价图片 URL 上的日期可能比实际晚一天，不能单独依赖。"""
    return str(batch.get("fetched_at") or "")[:10] == today or batch.get("quote_date") == today


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
        batch = latest_batch(database, category_id)
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
                "entry_count": batch.get("entry_count", 0) if batch else 0,
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
