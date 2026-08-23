from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent.lifecycle.types import BeforeTurnCtx

from src.sleep_context import FitbitAdapterStore, SleepContextAppender


NOW = datetime(2026, 8, 23, 8, tzinfo=UTC)


def _ctx(channel: str, now: datetime) -> BeforeTurnCtx:
    return BeforeTurnCtx(
        session_key=f"{channel}:fitbit",
        channel=channel,
        chat_id="fitbit",
        content="continuation",
        timestamp=now,
        retrieved_memory_block="",
        retrieval_trace_raw=None,
        history_messages=(),
    )


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


@pytest.mark.asyncio
async def test_wake_duty_and_fresh_sleep_hint_coexist(tmp_path: Path) -> None:
    ctx = _ctx("wake", NOW + timedelta(minutes=2))
    ctx.extra_hints.append("Content duty: Fitbit health alert")

    await SleepContextAppender(_store(tmp_path / "adapter.sqlite3")).prepare(ctx)

    assert ctx.extra_hints[0] == "Content duty: Fitbit health alert"
    assert "state=sleeping" in ctx.extra_hints[1]
    assert ctx.abort is False
    assert ctx.content == "continuation"


@pytest.mark.asyncio
async def test_passive_turn_has_no_sleep_hint(tmp_path: Path) -> None:
    ctx = _ctx("mobile", NOW + timedelta(minutes=2))
    await SleepContextAppender(_store(tmp_path / "adapter.sqlite3")).prepare(ctx)
    assert ctx.extra_hints == []


@pytest.mark.asyncio
async def test_stale_sleep_cache_has_no_wake_hint(tmp_path: Path) -> None:
    ctx = _ctx("wake", NOW + timedelta(minutes=11))
    await SleepContextAppender(_store(tmp_path / "adapter.sqlite3")).prepare(ctx)
    assert ctx.extra_hints == []
