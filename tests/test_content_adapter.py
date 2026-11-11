from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from plugins.timer.timer import AsyncioOneShotTimer
from plugins.timer.contract import TimerReceipt, TimerStatus
from agent.plugin_composition import CompositionRoot
from plugins.watch.contract import WatchEvents
from src.content_adapter import (
    FitbitMonitorClient,
    FitbitWakeRuntime,
    parse_health_events,
)
from src.adapter_store import FitbitAdapterStore


NOW = datetime(2026, 8, 23, 8, tzinfo=UTC)
SNAPSHOT: dict[str, object] = {
    "health_events": [
        {
            "id": "fitbit:event-1",
            "type": "hr_elevated_rest",
            "message": "静息心率持续偏高",
            "severity": "high",
            "created_at": "2026-08-23 08:00",
            "suggested_tone": "先询问感受",
            "metrics": {"heart_rate": 105},
        }
    ],
    "sleep": {
        "state": "sleeping",
        "prob": 0.92,
        "prob_source": "model",
        "data_lag_min": 3,
    },
    "sleep_24h": {"00:00-08:00": "sleeping"},
    "last_updated": NOW.isoformat(),
}


class RecordingMonitor:
    def __init__(self) -> None:
        self.acknowledged: list[str] = []

    def snapshot(self) -> Mapping[str, object]:
        return SNAPSHOT

    def ensure_not_pending(self, event_id: str) -> None:
        self.acknowledged.append(event_id)


class RecordingEvents:
    def __init__(self) -> None:
        self.emitted: list[dict[str, object]] = []
        self.order: list[str] = []

    async def emit(self, source: str, **kwargs: object) -> str:
        self.emitted.append({"source": source, **kwargs})
        self.order.append(f"emit:{source}")
        return "1"


class OrderedMonitor(RecordingMonitor):
    def __init__(self, events: RecordingEvents) -> None:
        super().__init__()
        self._events = events

    def ensure_not_pending(self, event_id: str) -> None:
        self._events.order.append(f"ack:{event_id}")
        super().ensure_not_pending(event_id)


def _runtime(
    tmp_path: Path,
    events: RecordingEvents,
    monitor: RecordingMonitor,
    *,
    legacy: bool = False,
) -> tuple[FitbitWakeRuntime, FitbitAdapterStore]:
    path = tmp_path / "adapter.sqlite3"
    if legacy:
        _write_legacy_store(path)
    store = FitbitAdapterStore(path)
    store.initialize(NOW)
    return (
        FitbitWakeRuntime(
            store,
            AsyncioOneShotTimer(),
            cast(WatchEvents, events),
            cast(FitbitMonitorClient, monitor),
            poll_interval=timedelta(minutes=5),
            now=lambda: NOW,
        ),
        store,
    )


# 写出 3.3.x（EventMail 版本）留下的存储结构。
def _write_legacy_store(path: Path) -> None:
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


@pytest.mark.asyncio
async def test_health_event_is_emitted_then_acknowledged_and_sleep_state_emitted(
    tmp_path: Path,
) -> None:
    events = RecordingEvents()
    monitor = OrderedMonitor(events)
    runtime, store = _runtime(tmp_path, events, monitor)

    await runtime.tick()

    assert events.emitted == [
        {
            "source": "fitbit-health-alerts",
            "title": "hr_elevated_rest",
            "text": "静息心率持续偏高",
            "at": datetime(2026, 8, 23, 8).astimezone(),
            "key": "fitbit:event-1",
        },
        {"source": "fitbit-sleep", "text": "睡眠状态：睡着了", "state": True},
    ]
    assert events.order == [
        "emit:fitbit-health-alerts",
        "ack:fitbit:event-1",
        "emit:fitbit-sleep",
    ]
    assert store.next_due() == NOW + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_unknown_sleep_state_is_not_emitted(tmp_path: Path) -> None:
    class UnknownMonitor(RecordingMonitor):
        def snapshot(self) -> Mapping[str, object]:
            return {**SNAPSHOT, "sleep": {"state": "unknown"}}

    events = RecordingEvents()
    runtime, _ = _runtime(tmp_path, events, UnknownMonitor())

    await runtime.tick()

    assert [item["source"] for item in events.emitted] == ["fitbit-health-alerts"]


@pytest.mark.asyncio
async def test_emit_failure_leaves_event_pending_and_deadline_unchanged(
    tmp_path: Path,
) -> None:
    class FailingEvents(RecordingEvents):
        async def emit(self, source: str, **kwargs: object) -> str:
            raise RuntimeError("watch unavailable")

    monitor = RecordingMonitor()
    runtime, store = _runtime(tmp_path, FailingEvents(), monitor)

    with pytest.raises(RuntimeError):
        await runtime.tick()

    assert monitor.acknowledged == []
    assert store.next_due() == NOW


@pytest.mark.asyncio
async def test_first_tick_after_upgrade_acknowledges_backlog_without_emitting(
    tmp_path: Path,
) -> None:
    events = RecordingEvents()
    monitor = RecordingMonitor()
    runtime, store = _runtime(tmp_path, events, monitor, legacy=True)
    assert store.cutover_pending()

    await runtime.tick()
    assert monitor.acknowledged == ["fitbit:event-1"]
    assert [item["source"] for item in events.emitted] == ["fitbit-sleep"]
    assert not store.cutover_pending()

    await runtime.tick()
    assert [item["source"] for item in events.emitted] == [
        "fitbit-sleep",
        "fitbit-health-alerts",
        "fitbit-sleep",
    ]


def test_parse_health_events_has_stable_identity() -> None:
    first = parse_health_events(SNAPSHOT)
    assert first == parse_health_events(dict(SNAPSHOT))
    assert first[0].id == "fitbit:event-1"
    assert first[0].title == "hr_elevated_rest"


@pytest.mark.asyncio
async def test_transient_monitor_error_rearms_and_recovers(tmp_path: Path) -> None:
    waits = 0
    finished = asyncio.Event()

    async def sleeper(_delay: float) -> None:
        nonlocal waits
        waits += 1
        if waits < 3:
            return
        finished.set()
        await asyncio.Future()

    class TimerHandle:
        id = "fitbit-retry"

        def __init__(self, deadline):
            self.deadline = deadline
            self.task = asyncio.create_task(sleeper(0))

        async def result(self):
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                if not self.task.cancelled():
                    raise
                status = TimerStatus.CANCELLED
            else:
                status = TimerStatus.FIRED
            return TimerReceipt(self.id, self.deadline, NOW, status)

        async def cancel(self):
            self.task.cancel()
            return await self.result()

        async def cleanup(self):
            await self.cancel()

    class Timer:
        def schedule(self, deadline):
            return TimerHandle(deadline)

    class FailOnceMonitor(RecordingMonitor):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def snapshot(self) -> Mapping[str, object]:
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary monitor read failure")
            return super().snapshot()

    events = RecordingEvents()
    monitor = FailOnceMonitor()
    store = FitbitAdapterStore(tmp_path / "adapter.sqlite3")
    store.initialize(NOW)
    runtime = FitbitWakeRuntime(
        store,
        Timer(),
        cast(WatchEvents, events),
        cast(FitbitMonitorClient, monitor),
        poll_interval=timedelta(minutes=5),
        now=lambda: NOW,
    )
    root = CompositionRoot("fitbit-retry")
    health = await root.context.health("fitbit-wake-poll")

    await runtime.start(root.context, health)
    await finished.wait()

    assert monitor.calls == 2
    assert health.healthy
    assert len([e for e in events.emitted if e["source"] == "fitbit-health-alerts"]) == 1
    assert any(
        incident.kind == "fitbit_content_retry" for incident in root.receipt().incidents
    )
    await runtime.close()
    await root.dispose()
