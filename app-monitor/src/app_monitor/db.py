from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .model_names import normalize_model, strip_hardware_code

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS quote_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    quote_date TEXT,
    image_url TEXT NOT NULL,
    image_path TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parse_error TEXT NOT NULL DEFAULT '',
    UNIQUE(category_id, image_sha256)
);

CREATE TABLE IF NOT EXISTS quote_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES quote_batches(id) ON DELETE CASCADE,
    category_id TEXT NOT NULL,
    model TEXT NOT NULL,
    storage TEXT NOT NULL,
    grade TEXT NOT NULL,
    price REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'ocr',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, model, storage, grade)
);

CREATE TABLE IF NOT EXISTS observed_items (
    item_id TEXT PRIMARY KEY,
    keyword TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    publish_time TEXT,
    display_region TEXT,
    seller_name TEXT NOT NULL DEFAULT '',
    is_fish_shop INTEGER,
    tags_json TEXT NOT NULL DEFAULT '[]',
    image_urls_json TEXT NOT NULL DEFAULT '[]',
    item_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_row_id INTEGER,
    raw_index INTEGER
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL UNIQUE REFERENCES observed_items(item_id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    storage TEXT NOT NULL,
    grade TEXT NOT NULL,
    quote_category TEXT NOT NULL,
    quote_entry_id INTEGER REFERENCES quote_entries(id) ON DELETE SET NULL,
    quote_price REAL NOT NULL,
    item_price REAL NOT NULL,
    expected_margin REAL NOT NULL,
    threshold_price REAL NOT NULL,
    region TEXT,
    reason TEXT NOT NULL,
    matched_at TEXT NOT NULL,
    notification_status TEXT NOT NULL DEFAULT 'pending',
    notification_result TEXT
);

CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    keyword TEXT NOT NULL,
    region TEXT,
    trigger TEXT NOT NULL DEFAULT 'schedule',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    observed INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    region_applied INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    storage TEXT,
    filter_applied INTEGER NOT NULL DEFAULT 0,
    passes TEXT
);

-- 每次搜索看到的每件商品及其判价结论（同一商品在不同搜索中各记一行）
CREATE TABLE IF NOT EXISTS run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    pass_label TEXT,
    position INTEGER,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    display_region TEXT,
    item_url TEXT NOT NULL,
    publish_time TEXT,
    model TEXT,
    storage TEXT,
    quote_price REAL,
    threshold_price REAL,
    expected_margin REAL,
    matched INTEGER NOT NULL DEFAULT 0,
    code TEXT,
    decision TEXT,
    new_deal INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, item_id)
);

-- 报价图中每款机型的扣费项（随报价批次保存，重新识别时整体替换）
CREATE TABLE IF NOT EXISTS quote_deductions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES quote_batches(id) ON DELETE CASCADE,
    category_id TEXT NOT NULL,
    model TEXT NOT NULL,
    column_index INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_min REAL,
    amount_max REAL,
    open_ended INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'deduct'
);
CREATE INDEX IF NOT EXISTS idx_quote_deductions_lookup ON quote_deductions(batch_id, model);

CREATE TABLE IF NOT EXISTS smart_facts (
    fingerprint TEXT PRIMARY KEY, facts_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS smart_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE, snapshot_json TEXT NOT NULL, config_json TEXT NOT NULL,
    facts_json TEXT NOT NULL, result_json TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'model', reviewed INTEGER NOT NULL DEFAULT 0,
    notification_status TEXT NOT NULL DEFAULT 'none', notification_result TEXT
);
CREATE INDEX IF NOT EXISTS idx_smart_item ON smart_assessments(item_id,id DESC);
CREATE TABLE IF NOT EXISTS smart_notifications (
    item_id TEXT NOT NULL, tier TEXT NOT NULL, status TEXT NOT NULL,
    updated_at TEXT NOT NULL, result TEXT,
    PRIMARY KEY(item_id,tier)
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quote_entries_lookup
ON quote_entries(category_id, model, storage, grade, enabled);
CREATE INDEX IF NOT EXISTS idx_deals_time ON deals(matched_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id);
CREATE INDEX IF NOT EXISTS idx_search_runs_model ON search_runs(model, id DESC);
"""


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            _add_columns(connection, "quote_batches", {"parse_error": "TEXT NOT NULL DEFAULT ''"})
            # 旧版本数据库的增量列；旧的 monitor_tasks / matches 表保留不动，不再使用。
            _add_columns(
                connection,
                "observed_items",
                {
                    "model": "TEXT", "storage": "TEXT", "decision": "TEXT",
                    "matched": "INTEGER NOT NULL DEFAULT 0", "image_urls_json": "TEXT NOT NULL DEFAULT '[]'",
                    "seller_name": "TEXT NOT NULL DEFAULT ''", "is_fish_shop": "INTEGER",
                },
            )
            _add_columns(connection, "run_items", {
                "assessment_id": "INTEGER", "quote_grade": "TEXT", "quote_basis": "TEXT",
                "valuation_json": "TEXT",
            })
            _add_columns(
                connection,
                "search_runs",
                {"storage": "TEXT", "filter_applied": "INTEGER NOT NULL DEFAULT 0", "passes": "TEXT"},
            )
            # 修正旧记录里误把短列表编号写成商品 ID 的详情页链接。
            connection.execute(
                """
                UPDATE observed_items
                SET item_url='https://www.goofish.com/item?id=' || item_id
                WHERE length(item_id) >= 8
                  AND item_id NOT GLOB '*[^0-9]*'
                  AND item_url <> 'https://www.goofish.com/item?id=' || item_id
                """
            )
            _remove_hardware_codes(connection)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def state_get(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else default

    def state_set(self, key: str, value: Any) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )


def _remove_hardware_codes(connection: sqlite3.Connection) -> None:
    """修复已入库的 OCR 型号代码；保留报价手动修正和命中的报价引用。"""
    for row in connection.execute("SELECT * FROM quote_entries").fetchall():
        if strip_hardware_code(row["model"]) == row["model"]:
            continue
        model = normalize_model(row["model"])
        target = connection.execute(
            "SELECT * FROM quote_entries WHERE batch_id=? AND model=? AND storage=? AND grade=?",
            (row["batch_id"], model, row["storage"], row["grade"]),
        ).fetchone()
        if target:
            if row["source"] == "manual" and target["source"] != "manual":
                connection.execute(
                    "UPDATE quote_entries SET price=?,source=?,enabled=?,updated_at=? WHERE id=?",
                    (row["price"], row["source"], row["enabled"], row["updated_at"], target["id"]),
                )
            connection.execute("UPDATE deals SET quote_entry_id=? WHERE quote_entry_id=?", (target["id"], row["id"]))
            connection.execute("DELETE FROM quote_entries WHERE id=?", (row["id"],))
        else:
            connection.execute("UPDATE quote_entries SET model=? WHERE id=?", (model, row["id"]))
    for table in ("quote_deductions", "search_runs", "run_items", "observed_items", "deals"):
        names = connection.execute(f"SELECT DISTINCT model FROM {table} WHERE model IS NOT NULL").fetchall()
        for row in names:
            old = row["model"]
            if strip_hardware_code(old) == old:
                continue
            model = normalize_model(old)
            connection.execute(f"UPDATE {table} SET model=? WHERE model=?", (model, old))
            if table in ("search_runs", "observed_items"):
                connection.execute(
                    f"UPDATE {table} SET keyword=? WHERE model=? AND keyword=?",
                    (f"iPhone {model}", model, f"iPhone {old}"),
                )
    row = connection.execute("SELECT value FROM app_state WHERE key='rules'").fetchone()
    if row:
        rules = json.loads(row["value"])
        disabled = rules.get("disabled_models", [])
        cleaned = []
        for key in disabled:
            model, sep, storage = key.partition("|")
            cleaned.append((normalize_model(model) if strip_hardware_code(model) != model else model) + sep + storage)
        if cleaned != disabled:
            rules["disabled_models"] = sorted(set(cleaned))
            connection.execute("UPDATE app_state SET value=? WHERE key='rules'", (json.dumps(rules, ensure_ascii=False),))


def _add_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
