import pytest

from app_monitor.db import Database, utc_now


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    return database


def add_quotes(database, entries, category_id="100025", quote_date="2026-10-19"):
    """entries: [(model, storage, grade, price), ...]"""
    now = utc_now()
    with database.connect() as connection:
        batch_id = connection.execute(
            """
            INSERT INTO quote_batches(
                category_id, category_name, quote_date, image_url, image_path,
                image_sha256, fetched_at, parse_status
            ) VALUES (?, '苹果无保', ?, 'https://example.test/q.png', '/tmp/q.png', ?, ?, 'parsed')
            """,
            (category_id, quote_date, f"sha-{category_id}-{quote_date}-{len(entries)}", now),
        ).lastrowid
        connection.executemany(
            """
            INSERT INTO quote_entries(
                batch_id, category_id, model, storage, grade, price, source, enabled, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'ocr', 1, ?)
            """,
            [(batch_id, category_id, m, s, g, p, now) for m, s, g, p in entries],
        )
    return batch_id
