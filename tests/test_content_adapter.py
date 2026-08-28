from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from agent.control.timer import AsyncioOneShotTimer
from agent.plugin_composition import CompositionRoot, PluginTimers
from fitbit_test_plugin.src.eventmail import (  # pyright: ignore[reportMissingImports]
    BoundAlertSource,
    BoundContextSource,
)
from src.content_adapter import (
    FitbitMonitorClient,
    FitbitWakeRuntime,
    normalize_health_events,
)
from src.sleep_context import FitbitAdapterStore


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


class RecordingAlerts:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []
        self.statuses: dict[str, str] = {}

    def report(self, **kwargs: object) -> Mapping[str, object]:
        self.reports.append(dict(kwargs))
        return {"accepted": True}

    def status(self, *, event_id: str) -> str | None:
        return self.statuses.get(event_id)


class RecordingContext:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []

    def report(self, **kwargs: object) -> Mapping[str, object]:
        self.reports.append(dict(kwargs))
        return {"changed": True}


def _runtime(
    tmp_path: Path,
    alerts: RecordingAlerts,
    context: RecordingContext,
    monitor: RecordingMonitor,
) -> tuple[FitbitWakeRuntime, FitbitAdapterStore]:
    store = FitbitAdapterStore(tmp_path / "adapter.sqlite3")
    store.initialize(NOW)
    return (
        FitbitWakeRuntime(
            store,
            PluginTimers.candidate_validation(),
            cast(BoundAlertSource, alerts),
            cast(BoundContextSource, context),
            cast(FitbitMonitorClient, monitor),
            poll_interval=timedelta(minutes=5),
            sleep_ttl=timedelta(minutes=10),
            now=lambda: NOW,
        ),
        store,
    )


def test_health_reports_alert_and_sleep_reports_expiring_context(
    tmp_path: Path,
) -> None:
    alerts = RecordingAlerts()
    context = RecordingContext()
    runtime, store = _runtime(tmp_path, alerts, context, RecordingMonitor())

    runtime.tick()

    assert alerts.reports[0]["event_id"] == "fitbit:event-1"
    assert context.reports == [
        {
                "event_id": "current",
            "payload": store.current_sleep(NOW),
            "observed_at": NOW,
            "expires_at": NOW + timedelta(minutes=10),
        }
    ]
    assert store.next_due() == NOW + timedelta(minutes=5)


@pytest.mark.parametrize("status", ["delivered", "skipped", "expired"])
def test_terminal_alert_is_acknowledged_instead_of_reported(
    tmp_path: Path, status: str
) -> None:
    alerts = RecordingAlerts()
    alerts.statuses["fitbit:event-1"] = status
    context = RecordingContext()
    monitor = RecordingMonitor()
    runtime, _ = _runtime(tmp_path, alerts, context, monitor)

    runtime.tick()

    assert monitor.acknowledged == ["fitbit:event-1"]
    assert alerts.reports == []
    assert len(context.reports) == 1


def test_revised_alert_replaces_pending_payload_and_acks_once(tmp_path: Path) -> None:
    class RevisedMonitor(RecordingMonitor):
        def __init__(self) -> None:
            super().__init__()
            self.current = SNAPSHOT

        def snapshot(self) -> Mapping[str, object]:
            return self.current

    alerts = RecordingAlerts()
    context = RecordingContext()
    monitor = RevisedMonitor()
    runtime, _ = _runtime(tmp_path, alerts, context, monitor)
    runtime.tick()
    revised = {
        **SNAPSHOT,
        "health_events": [
            {**cast(list[dict[str, object]], SNAPSHOT["health_events"])[0],
             "message": "静息心率已经恢复"}
        ],
    }
    monitor.current = revised
    runtime.tick()

    assert [report["event_id"] for report in alerts.reports] == [
        "fitbit:event-1",
        "fitbit:event-1",
    ]
    alerts.statuses["fitbit:event-1"] = "delivered"
    runtime.tick()
    assert monitor.acknowledged == ["fitbit:event-1"]


def test_normalization_has_stable_identity_and_revision() -> None:
    first = normalize_health_events(SNAPSHOT)
    second = normalize_health_events(dict(SNAPSHOT))
    assert first == second
    assert first[0]["item_id"] == "fitbit:event-1"
    assert first[0]["requires_ack"] is True


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

    class FailOnceMonitor(RecordingMonitor):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def snapshot(self) -> Mapping[str, object]:
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary monitor read failure")
            return super().snapshot()

    alerts = RecordingAlerts()
    context = RecordingContext()
    monitor = FailOnceMonitor()
    store = FitbitAdapterStore(tmp_path / "adapter.sqlite3")
    store.initialize(NOW)
    runtime = FitbitWakeRuntime(
        store,
        PluginTimers(AsyncioOneShotTimer(clock=lambda: NOW, sleeper=sleeper)),
        cast(BoundAlertSource, alerts),
        cast(BoundContextSource, context),
        cast(FitbitMonitorClient, monitor),
        poll_interval=timedelta(minutes=5),
        sleep_ttl=timedelta(minutes=10),
        now=lambda: NOW,
    )
    root = CompositionRoot("fitbit-retry")
    health = await root.context.health("fitbit-wake-poll")

    await runtime.start(root.context, health)
    await finished.wait()

    assert monitor.calls == 2
    assert health.healthy
    assert len(alerts.reports) == 1
    assert any(
        incident.kind == "fitbit_content_retry" for incident in root.receipt().incidents
    )
    await runtime.close()
    await root.dispose()
