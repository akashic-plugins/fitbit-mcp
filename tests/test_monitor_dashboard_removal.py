from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "monitor"))
from monitor import server


def test_monitor_root_points_to_akashic_dashboard() -> None:
    response = server.index()

    assert response.status_code == 410
    assert b"Akashic Dashboard / Fitbit" in response.body


def test_monitor_keeps_auth_without_the_sleep_diff_interface(
    tmp_path: Path, monkeypatch,
) -> None:
    token_file = tmp_path / "tokens.json"
    monkeypatch.setattr(server, "TOKEN_FILE", token_file)
    assert server.auth_status() == {"authorized": False}
    token_file.write_text("{}", encoding="utf-8")
    assert server.auth_status() == {"authorized": True}

    paths = {route.path for route in server.app.routes}
    assert "/auth/start" in paths
    assert "/oauth/callback" in paths
    assert "/sleep-diff" not in paths
    assert "/api/sleep_diff/build" not in paths
