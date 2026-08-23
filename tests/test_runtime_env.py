from __future__ import annotations

from pathlib import Path

import pytest

from monitor.runtime_env import RotatingTextLog, resolve_server_port


def test_server_port_uses_config_without_runtime_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FITBIT_MONITOR_PORT", raising=False)

    assert resolve_server_port(18765) == 18765


def test_server_port_uses_candidate_isolation_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FITBIT_MONITOR_PORT", "28765")

    assert resolve_server_port(18765) == 28765


@pytest.mark.parametrize("value", ["0", "65536", "invalid"])
def test_server_port_rejects_invalid_runtime_override(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("FITBIT_MONITOR_PORT", value)

    with pytest.raises(ValueError):
        resolve_server_port(18765)


def test_runtime_log_rotates_without_touching_authoritative_state(tmp_path: Path) -> None:
    log_path = tmp_path / "monitor.runtime.log"
    protected = {
        tmp_path / "stat_events.json": b"pending-events",
        tmp_path / "content.sqlite3": b"content-fact",
        tmp_path / "sessions.db": b"session-fact",
    }
    for path, payload in protected.items():
        path.write_bytes(payload)

    log = RotatingTextLog(log_path, max_bytes=12, backups=2)
    for payload in ("A" * 10, "B" * 10, "C" * 10, "D" * 10):
        _ = log.write(payload)
        log.flush()
    log.close()

    assert log_path.read_text(encoding="utf-8") == "D" * 10
    assert log_path.with_name("monitor.runtime.log.1").read_text() == "C" * 10
    assert log_path.with_name("monitor.runtime.log.2").read_text() == "B" * 10
    assert not log_path.with_name("monitor.runtime.log.3").exists()
    assert sum(
        path.stat().st_size
        for path in tmp_path.glob("monitor.runtime.log*")
    ) <= 36
    assert {path: path.read_bytes() for path in protected} == protected
