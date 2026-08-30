from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from agent.plugin_composition import DashboardContext
from fastapi.routing import APIRoute
from fastapi import HTTPException

import dashboard


DATA = {
    "summary": {"heart_rate": 72, "steps": 4823, "spo2": 97.4},
    "sleep": {
        "state": "awake",
        "reason": "高概率清醒",
        "since": "2026-08-01 08:00:00",
    },
    "signals": {
        "sleep_prob": 0.18,
        "prob_source": "ml",
        "hr_avg": 71.5,
        "zero_steps_count": 3,
        "sustained_zero_min": 4,
    },
    "data_meta": {
        "data_lag_min": 3,
        "spo2_lag_min": 12,
        "poll_time": "2026-08-01 13:27:00",
    },
    "heart_rate": [{"time": "13:26:00", "value": 72}],
    "steps": [{"time": "13:26:00", "value": 0}],
    "last_updated": "13:27:01",
    "stale": False,
}

SNAPSHOT = {
    "sleep_24h": {
        "23:00-07:00": "sleeping",
        "07:00-13:27": "awake",
    }
}

HISTORY = [
    {
        "poll_time": "2026-08-01 13:27:00",
        "state": "awake",
        "reason": "Viterbi 判定清醒",
        "changed": True,
        "sleep_prob": 0.18,
        "signals": {"prob_source": "ml", "sleep_prob": 0.18},
    }
]

MONITOR_SNAPSHOT = {
    "data": DATA,
    "sleep_24h": SNAPSHOT["sleep_24h"],
    "prediction_events": HISTORY,
}


def test_projects_only_dashboard_first_screen_fields() -> None:
    overview = dashboard._project_overview(DATA, SNAPSHOT, HISTORY)

    assert overview["current"] == {
        "heart_rate": 72,
        "spo2": 97.4,
        "steps": 4823,
        "sleep_state": "awake",
        "sleep_reason": "高概率清醒",
        "sleep_since": "2026-08-01 08:00:00",
        "sleep_prob": 0.18,
    }
    assert overview["sleep_24h"] == [
        {"range": "23:00-07:00", "state": "sleeping"},
        {"range": "07:00-13:27", "state": "awake"},
    ]
    assert overview["prediction_events"] == [
        {
            "time": "2026-08-01 13:27:00",
            "source": "ml",
            "sleep_probability": 0.18,
            "final_state": "awake",
            "reason": "Viterbi 判定清醒",
            "changed": True,
        }
    ]
    assert "sleep_report" not in overview
    assert "health_context" not in overview


def test_registered_overview_reads_one_compact_monitor_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = dashboard.FastAPI()
    calls: list[str] = []

    def monitor_json(path: str):
        calls.append(path)
        return MONITOR_SNAPSHOT

    monkeypatch.setattr(dashboard, "_monitor_json", monitor_json)
    dashboard.register(
        app,
        DashboardContext(
            plugin_id="fitbit",
            plugin_dir=Path(dashboard.__file__).resolve().parent,
            data_root=Path("/tmp/fitbit-dashboard-test"),
            validation=False,
        ),
    )
    overview_route = cast(
        APIRoute,
        next(
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and route.path == "/api/dashboard/fitbit/overview"
        ),
    )

    assert overview_route.endpoint()["current"]["heart_rate"] == 72
    assert calls == ["/api/dashboard/snapshot"]


def test_monitor_authorization_redirect_becomes_a_plugin_owned_dto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization_url = "https://www.fitbit.com/oauth2/authorize?client_id=fitbit"
    calls: list[tuple[str, int, bool]] = []

    class Response:
        is_redirect = True
        headers = {"location": authorization_url}

        def raise_for_status(self) -> None:
            return None

    def get(url: str, *, timeout: int, allow_redirects: bool) -> Response:
        calls.append((url, timeout, allow_redirects))
        return Response()

    monkeypatch.setattr(dashboard.requests, "get", get)
    assert dashboard._monitor_authorization_url() == authorization_url
    assert calls == [("http://127.0.0.1:18765/auth/start", 8, False)]


def test_compact_snapshot_boundary_rejects_missing_prediction_events() -> None:
    with pytest.raises(HTTPException, match="prediction_events 必须是数组"):
        dashboard._project_dashboard_snapshot(
            {"data": DATA, "sleep_24h": SNAPSHOT["sleep_24h"]}
        )


def test_dashboard_projection_rejects_malformed_monitor_payload() -> None:
    with pytest.raises(HTTPException, match="summary 必须是对象"):
        dashboard._project_overview({**DATA, "summary": []}, SNAPSHOT)


def test_dashboard_projection_rejects_unknown_sleep_state() -> None:
    with pytest.raises(HTTPException, match="sleep_24h 条目无效"):
        dashboard._project_overview(DATA, {"sleep_24h": {"23:00-07:00": "restless"}})


def test_dashboard_projection_rejects_invalid_prediction_state() -> None:
    with pytest.raises(HTTPException, match="sleep_log state 无效"):
        dashboard._project_overview(DATA, SNAPSHOT, [{"state": "restless"}])
