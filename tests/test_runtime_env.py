from __future__ import annotations

import pytest

from monitor.runtime_env import resolve_server_port


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
