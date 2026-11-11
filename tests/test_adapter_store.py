from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.adapter_store import FitbitAdapterStore


NOW = datetime(2026, 8, 23, 8, tzinfo=UTC)


def test_fresh_store_has_no_cutover_and_commit_advances_deadline(tmp_path: Path) -> None:
    store = FitbitAdapterStore(tmp_path / "adapter.sqlite3")
    store.initialize(NOW)
    assert store.next_due() == NOW
    assert not store.cutover_pending()

    store.commit_tick(next_due=NOW + timedelta(minutes=5))

    assert store.next_due() == NOW + timedelta(minutes=5)


def test_legacy_store_drops_sleep_columns_and_marks_cutover(tmp_path: Path) -> None:
    path = tmp_path / "adapter.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE source_state(
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            next_due TEXT NOT NULL,
            sleep_json TEXT, sleep_observed_at TEXT, sleep_expires_at TEXT
        );
        INSERT INTO source_state VALUES(1, '2026-08-23T07:55:00+00:00', '{}', NULL, NULL);
        """
    )
    connection.commit()
    connection.close()
    store = FitbitAdapterStore(path)

    store.initialize(NOW)
    store.initialize(NOW)

    assert store.next_due() == datetime(2026, 8, 23, 7, 55, tzinfo=UTC)
    assert store.cutover_pending()
    with sqlite3.connect(path) as check:
        columns = [row[1] for row in check.execute("PRAGMA table_info(source_state)")]
    assert columns == ["singleton", "next_due", "cutover_pending"]
    store.commit_tick(next_due=NOW)
    assert not store.cutover_pending()
