from __future__ import annotations

import pytest
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


def test_dashboard_projection_rejects_malformed_monitor_payload() -> None:
    with pytest.raises(HTTPException, match="summary 必须是对象"):
        dashboard._project_overview({**DATA, "summary": []}, SNAPSHOT)


def test_dashboard_projection_rejects_unknown_sleep_state() -> None:
    with pytest.raises(HTTPException, match="sleep_24h 条目无效"):
        dashboard._project_overview(DATA, {"sleep_24h": {"23:00-07:00": "restless"}})


def test_dashboard_projection_rejects_invalid_prediction_state() -> None:
    with pytest.raises(HTTPException, match="sleep_log state 无效"):
        dashboard._project_overview(DATA, SNAPSHOT, [{"state": "restless"}])
