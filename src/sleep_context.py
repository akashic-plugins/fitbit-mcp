from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    next_due TEXT NOT NULL,
    sleep_json TEXT,
    sleep_observed_at TEXT,
    sleep_expires_at TEXT
);
"""


class FitbitAdapterStore:
    """在一个私有文件中持久化来源 deadline 与当前睡眠上下文。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self, now: datetime) -> None:
        with self._transaction(write=True) as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                """
                INSERT OR IGNORE INTO source_state(
                    singleton, next_due, sleep_json,
                    sleep_observed_at, sleep_expires_at
                ) VALUES(1, ?, NULL, NULL, NULL)
                """,
                (_aware_utc(now),),
            )

    def next_due(self) -> datetime:
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT next_due FROM source_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Fitbit adapter state 尚未初始化")
            return datetime.fromisoformat(str(row["next_due"]))

    def commit_snapshot(
        self,
        sleep: Mapping[str, object],
        *,
        observed_at: datetime,
        expires_at: datetime,
        next_due: datetime,
    ) -> None:
        """原子提交睡眠缓存与下一个来源 deadline。"""

        payload = json.dumps(sleep, sort_keys=True, separators=(",", ":"))
        with self._transaction(write=True) as connection:
            changed = connection.execute(
                """
                UPDATE source_state
                SET next_due = ?, sleep_json = ?,
                    sleep_observed_at = ?, sleep_expires_at = ?
                WHERE singleton = 1
                """,
                (
                    _aware_utc(next_due),
                    payload,
                    _aware_utc(observed_at),
                    _aware_utc(expires_at),
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Fitbit adapter state 尚未初始化")

    def current_sleep(self, now: datetime) -> Mapping[str, object] | None:
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT sleep_json, sleep_expires_at
                FROM source_state WHERE singleton = 1
                """
            ).fetchone()
            if row is None or row["sleep_json"] is None:
                return None
            expires_at = datetime.fromisoformat(str(row["sleep_expires_at"]))
            if expires_at <= _aware(now):
                return None
            payload = json.loads(str(row["sleep_json"]))
            if not isinstance(payload, Mapping):
                raise TypeError("Fitbit sleep cache 必须是对象")
            return cast(Mapping[str, object], payload)

    @contextmanager
    def _transaction(self, *, write: bool) -> Generator[sqlite3.Connection]:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Fitbit adapter 时间必须带时区")
    return value.astimezone(UTC)


def _aware_utc(value: datetime) -> str:
    return _aware(value).isoformat()
