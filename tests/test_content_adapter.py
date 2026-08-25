from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Generator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import unquote

import pytest
from agent.control.timer import AsyncioOneShotTimer
from agent.plugin_composition import CompositionRoot, PluginTimers
from plugins.content import plugin as content_plugin
from plugins.content.store import ContentIdentityConflict, ContentStore

from src.content_adapter import (
    BoundContentSource,
    FitbitContentRuntime,
    FitbitMonitorClient,
    normalize_health_events,
    stable_batch_id,
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
    def __init__(self, snapshot: Mapping[str, object]) -> None:
        self.current = dict(snapshot)
        self.acknowledged: list[str] = []

    def snapshot(self) -> Mapping[str, object]:
        return self.current

    def ensure_not_pending(self, event_id: str) -> None:
        self.acknowledged.append(event_id)


def _bound(store: ContentStore, changed=lambda: None) -> BoundContentSource:
    return content_plugin._SourceServices(store, changed).bind(
        "fitbit-health-alerts"
    )


def _runtime(
    tmp_path: Path,
    content: BoundContentSource,
    monitor: object,
) -> tuple[FitbitContentRuntime, FitbitAdapterStore]:
    store = FitbitAdapterStore(tmp_path / "adapter.sqlite3")
    store.initialize(NOW)
    runtime = FitbitContentRuntime(
        store,
        PluginTimers.candidate_validation(),
        content,
        cast(FitbitMonitorClient, monitor),
        poll_interval=timedelta(minutes=5),
        sleep_ttl=timedelta(minutes=10),
        now=lambda: NOW,
    )
    return runtime, store


def test_submit_commits_before_private_deadline_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    content_store = ContentStore(tmp_path / "content.sqlite3")
    content_store.initialize()
    calls = 0

    def fail_after_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("post-commit hint failed")

    first, adapter = _runtime(
        tmp_path,
        _bound(content_store, fail_after_commit),
        RecordingMonitor(SNAPSHOT),
    )
    with pytest.raises(RuntimeError, match="post-commit hint failed"):
        first.tick()

    assert content_store.state_counts() == {"pending": 1}
    assert adapter.next_due() == NOW
    assert adapter.current_sleep(NOW) is None

    second, adapter = _runtime(
        tmp_path,
        _bound(content_store),
        RecordingMonitor(SNAPSHOT),
    )
    second.tick()
    assert content_store.state_counts() == {"pending": 1}
    assert adapter.next_due() == NOW + timedelta(minutes=5)
    sleep = adapter.current_sleep(NOW)
    assert sleep is not None and sleep["state"] == "sleeping"


def test_health_content_and_sleep_cache_have_separate_owners(tmp_path: Path) -> None:
    content_store = ContentStore(tmp_path / "content.sqlite3")
    content_store.initialize()
    runtime, adapter = _runtime(
        tmp_path,
        _bound(content_store),
        RecordingMonitor(SNAPSHOT),
    )

    runtime.tick()

    snapshot = content_store.snapshot(NOW)
    snapshot_items = cast(Sequence[Mapping[str, object]], snapshot["items"])
    assert len(snapshot_items) == 1
    payload = cast(Mapping[str, object], snapshot_items[0]["payload"])
    assert payload["upstream_event_id"] == "fitbit:event-1"
    assert "sleep" not in payload
    sleep = adapter.current_sleep(NOW)
    assert sleep is not None and sleep["state"] == "sleeping"


def test_normalization_has_stable_batch_and_revision() -> None:
    first = normalize_health_events(SNAPSHOT)
    second = normalize_health_events(json.loads(json.dumps(SNAPSHOT)))
    assert first == second
    assert stable_batch_id(first) == stable_batch_id(second)
    assert first[0]["requires_ack"] is True


def test_batch_identity_does_not_depend_on_monitor_queue_order() -> None:
    first = normalize_health_events(SNAPSHOT)[0]
    second = {
        **first,
        "item_id": "fitbit:event-2",
        "revision": "revision-2",
    }
    assert stable_batch_id((first, second)) == stable_batch_id((second, first))


def test_reordered_monitor_batch_replays_identical_content_sequence(tmp_path) -> None:
    second_event = {
        "id": "fitbit:event-2",
        "type": "spo2_low",
        "message": "血氧偏低",
        "severity": "high",
        "created_at": "2026-08-23 08:01",
        "suggested_tone": "先确认状态",
        "metrics": {"spo2": 89},
    }
    first_snapshot = {
        **SNAPSHOT,
        "health_events": [
            cast(list[Mapping[str, object]], SNAPSHOT["health_events"])[0],
            second_event,
        ],
    }
    reordered = {
        **first_snapshot,
        "health_events": list(reversed(first_snapshot["health_events"])),
    }
    first_items = normalize_health_events(first_snapshot)
    second_items = normalize_health_events(reordered)
    assert second_items == first_items

    control_store = ContentStore(tmp_path / "conflict-control.sqlite3")
    control_store.initialize()
    control = _bound(control_store)
    batch_id = stable_batch_id(first_items)
    _ = control.submit(batch_id, first_items)
    with pytest.raises(ContentIdentityConflict):
        _ = control.submit(batch_id, tuple(reversed(first_items)))

    content_store = ContentStore(tmp_path / "content.sqlite3")
    content_store.initialize()
    bound = _bound(content_store)
    first_receipt = bound.submit(batch_id, first_items)
    replay_receipt = bound.submit(batch_id, second_items)

    assert replay_receipt == first_receipt
    assert content_store.state_counts() == {"pending": 2}


@pytest.mark.asyncio
async def test_reload_has_only_one_real_timer_wait(tmp_path: Path) -> None:
    active = 0
    maximum = 0
    entered = asyncio.Event()

    async def sleeper(_delay: float) -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        try:
            await asyncio.Future()
        finally:
            active -= 1

    content_store = ContentStore(tmp_path / "content.sqlite3")
    content_store.initialize()
    adapter = FitbitAdapterStore(tmp_path / "adapter.sqlite3")
    adapter.initialize(NOW)
    timers = PluginTimers(AsyncioOneShotTimer(clock=lambda: NOW, sleeper=sleeper))

    def make_runtime() -> FitbitContentRuntime:
        return FitbitContentRuntime(
            adapter,
            timers,
            _bound(content_store),
            cast(FitbitMonitorClient, RecordingMonitor(SNAPSHOT)),
            poll_interval=timedelta(minutes=5),
            sleep_ttl=timedelta(minutes=10),
            now=lambda: NOW,
        )

    first = make_runtime()
    first_root = CompositionRoot("fitbit-reload:first")
    first_health = await first_root.context.health("fitbit-content-poll")
    await first.start(first_root.context, first_health)
    await entered.wait()
    assert active == 1
    await first.close()
    assert active == 0
    await first_root.dispose()

    entered.clear()
    second = make_runtime()
    second_root = CompositionRoot("fitbit-reload:second")
    second_health = await second_root.context.health("fitbit-content-poll")
    await second.start(second_root.context, second_health)
    await entered.wait()
    assert active == 1
    assert maximum == 1
    await second.close()
    await second_root.dispose()


@pytest.mark.asyncio
async def test_transient_oserror_rearms_and_recovers_without_partial_state(
    tmp_path: Path,
) -> None:
    sleeper_calls = 0
    third_wait = asyncio.Event()

    async def sleeper(_delay: float) -> None:
        nonlocal sleeper_calls
        sleeper_calls += 1
        if sleeper_calls <= 2:
            return
        third_wait.set()
        await asyncio.Future()

    class FailOnceMonitor(RecordingMonitor):
        def __init__(self) -> None:
            super().__init__(SNAPSHOT)
            self.snapshot_calls = 0

        def snapshot(self) -> Mapping[str, object]:
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                raise OSError("temporary monitor read failure")
            return super().snapshot()

    content_store = ContentStore(tmp_path / "content.sqlite3")
    content_store.initialize()
    adapter = FitbitAdapterStore(tmp_path / "adapter.sqlite3")
    adapter.initialize(NOW)
    monitor = FailOnceMonitor()
    runtime = FitbitContentRuntime(
        adapter,
        PluginTimers(AsyncioOneShotTimer(clock=lambda: NOW, sleeper=sleeper)),
        _bound(content_store),
        cast(FitbitMonitorClient, monitor),
        poll_interval=timedelta(minutes=5),
        sleep_ttl=timedelta(minutes=10),
        now=lambda: NOW,
    )
    root = CompositionRoot("fitbit-transient-retry")
    health = await root.context.health("fitbit-content-poll")

    await runtime.start(root.context, health)
    await third_wait.wait()

    assert sleeper_calls == 3
    assert monitor.snapshot_calls == 2
    assert health.healthy
    assert content_store.state_counts() == {"pending": 1}
    assert adapter.next_due() == NOW + timedelta(minutes=5)
    assert monitor.acknowledged == []
    assert any(
        incident.kind == "fitbit_content_retry"
        and "temporary monitor read failure" in incident.message
        for incident in root.receipt().incidents
    )
    await runtime.close()
    await root.dispose()


class _MonitorState:
    def __init__(self) -> None:
        self.pending = [dict(cast(list[Mapping[str, object]], SNAPSHOT["health_events"])[0])]


class _MonitorHandler(BaseHTTPRequestHandler):
    state: _MonitorState

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/api/agent":
            self.send_error(404)
            return
        self._json({**SNAPSHOT, "health_events": self.state.pending})

    def do_POST(self) -> None:  # noqa: N802
        prefix = "/api/agent/acknowledge/"
        if not self.path.startswith(prefix):
            self.send_error(404)
            return
        event_id = unquote(self.path.removeprefix(prefix))
        before = len(self.state.pending)
        self.state.pending = [row for row in self.state.pending if row["id"] != event_id]
        self._json({"acknowledged": len(self.state.pending) < before})

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args

    def _json(self, payload: Mapping[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def monitor_server() -> Generator[tuple[str, _MonitorState], None, None]:
    state = _MonitorState()
    handler = type("MonitorHandler", (_MonitorHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _delivered_content(path: Path) -> None:
    store = ContentStore(path)
    store.initialize()
    items = normalize_health_events(SNAPSHOT)
    store.submit("fitbit-health-alerts", stable_batch_id(items), items)
    snapshot = store.snapshot(NOW)
    snapshot_items = cast(Sequence[Mapping[str, object]], snapshot["items"])
    selected = store.select(
        cast(Mapping[str, object], snapshot_items[0]["ref"]),
        cast(int, snapshot["snapshot_seq"]),
        {"session_id": "wake:fitbit", "turn_id": "turn-1"},
        NOW,
    )
    token = cast(str, selected["selection_token"])
    store.transition(token, "ready_for_delivery")
    store.transition(token, "delivered", settlement_ref="delivery:fitbit:1")


def test_ack_response_sigkill_recovers_by_desired_state(
    tmp_path: Path,
    monitor_server: tuple[str, _MonitorState],
) -> None:
    base_url, state = monitor_server
    content_path = tmp_path / "content.sqlite3"
    _delivered_content(content_path)
    script = """
import os, signal, sys
from pathlib import Path
from agent.plugin_composition import PluginTimers
from plugins.content import plugin as content_plugin
from plugins.content.store import ContentStore
from src.content_adapter import FitbitContentRuntime, FitbitMonitorClient
from src.sleep_context import FitbitAdapterStore
from datetime import UTC, datetime, timedelta

content_store = ContentStore(Path(sys.argv[1]))
content_store.initialize()
bound = content_plugin._SourceServices(content_store, lambda: None).bind('fitbit-health-alerts')
adapter = FitbitAdapterStore(Path(sys.argv[2]))
adapter.initialize(datetime.now(UTC))
runtime = FitbitContentRuntime(
    adapter, PluginTimers.candidate_validation(), bound, FitbitMonitorClient(sys.argv[3]),
    poll_interval=timedelta(minutes=5), sleep_ttl=timedelta(minutes=10),
    after_provider_ack=lambda: os.kill(os.getpid(), signal.SIGKILL),
)
runtime._drain_unsettled()
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(content_path),
            str(tmp_path / "child-adapter.sqlite3"),
            base_url,
        ],
        check=False,
    )
    assert result.returncode == -signal.SIGKILL
    assert state.pending == []
    assert ContentStore(content_path).state_counts() == {"delivered": 1}

    content_store = ContentStore(content_path)
    runtime, _adapter = _runtime(
        tmp_path,
        _bound(content_store),
        FitbitMonitorClient(base_url),
    )
    runtime._drain_unsettled()
    assert ContentStore(content_path).state_counts() == {"settled": 1}
    with sqlite3.connect(content_path) as connection:
        row = connection.execute(
            "SELECT status, settlement_ref, payload_json FROM items"
        ).fetchone()
    assert row is not None
    assert row[0] == "settled"
    assert row[1] == "delivery:fitbit:1"
    assert json.loads(row[2])["upstream_event_id"] == "fitbit:event-1"
