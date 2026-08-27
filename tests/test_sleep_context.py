from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.sleep_context import FitbitAdapterStore


NOW = datetime(2026, 8, 23, 8, tzinfo=UTC)


def _store(path: Path) -> FitbitAdapterStore:
    store = FitbitAdapterStore(path)
    store.initialize(NOW)
    store.commit_snapshot(
        {
            "state": "sleeping",
            "prob": 0.92,
            "prob_source": "model",
            "data_lag_min": 3,
        },
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        next_due=NOW + timedelta(minutes=5),
    )
    return store


def test_sleep_cache_overwrites_one_current_projection(tmp_path: Path) -> None:
    store = _store(tmp_path / "adapter.sqlite3")
    store.commit_snapshot(
        {
            "state": "awake",
            "prob": 0.05,
            "prob_source": "model",
            "data_lag_min": 1,
        },
        observed_at=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=15),
        next_due=NOW + timedelta(minutes=10),
    )

    current = store.current_sleep(NOW + timedelta(minutes=6))
    assert current is not None and current["state"] == "awake"


def test_expired_sleep_projection_is_not_returned(tmp_path: Path) -> None:
    store = _store(tmp_path / "adapter.sqlite3")
    assert store.current_sleep(NOW + timedelta(minutes=10)) is None
