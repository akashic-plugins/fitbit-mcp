from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    next_due TEXT NOT NULL,
    cutover_pending INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_workouts(
    workout_id TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
"""
# EventMail 时代留下的睡眠缓存列，只为已删除的 Context 服务。
_LEGACY_COLUMNS = ("sleep_json", "sleep_observed_at", "sleep_expires_at")


class FitbitAdapterStore:
    """在一个私有文件中持久化来源 deadline 与"旧队列待收口"标记。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    # 建表；从 EventMail 版本升级时去掉睡眠缓存列，并要求第一轮直接确认积压事件。
    def initialize(self, now: datetime) -> None:
        with self._transaction(write=True) as connection:
            connection.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(source_state)")
            }
            if "sleep_json" in columns:
                connection.execute(
                    "ALTER TABLE source_state"
                    " ADD COLUMN cutover_pending INTEGER NOT NULL DEFAULT 1"
                )
                for column in _LEGACY_COLUMNS:
                    connection.execute(f"ALTER TABLE source_state DROP COLUMN {column}")
            connection.execute(
                "INSERT OR IGNORE INTO source_state(singleton, next_due) VALUES(1, ?)",
                (_aware_utc(now),),
            )

    def next_due(self) -> datetime:
        row = self._read_row()
        return datetime.fromisoformat(str(row["next_due"]))

    # 升级后的第一轮：monitor 队列里的旧事件只确认，不再交给值班。
    def cutover_pending(self) -> bool:
        return bool(self._read_row()["cutover_pending"])

    def has_seen_workout(self, workout_id: str) -> bool:
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT 1 FROM seen_workouts WHERE workout_id = ?",
                (workout_id,),
            ).fetchone()
        return row is not None

    def mark_workout_seen(self, workout_id: str, seen_at: datetime) -> None:
        with self._transaction(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO seen_workouts(workout_id, seen_at) VALUES(?, ?)",
                (workout_id, _aware_utc(seen_at)),
            )

    # 原子推进下一个来源 deadline，并清除升级标记。
    def commit_tick(self, *, next_due: datetime) -> None:
        with self._transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE source_state SET next_due = ?, cutover_pending = 0 WHERE singleton = 1",
                (_aware_utc(next_due),),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Fitbit adapter state 尚未初始化")

    def _read_row(self) -> sqlite3.Row:
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT next_due, cutover_pending FROM source_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("Fitbit adapter state 尚未初始化")
        return row

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


def _aware_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Fitbit adapter 时间必须带时区")
    return value.astimezone(UTC).isoformat()
