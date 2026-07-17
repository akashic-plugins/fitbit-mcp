from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import plugin
from plugin import FitbitMobileDashboardReader


class Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise plugin.requests.HTTPError(
                f"HTTP {self.status_code}",
                response=SimpleNamespace(status_code=self.status_code),
            )

    def json(self) -> object:
        return self._payload


CURRENT_PAYLOAD = {
    "available": True,
    "last_updated": "10:36:17",
    "data_lag_min": 3,
    "spo2_lag_min": 99,
    "heart_rate": 110,
    "spo2": 93.8,
    "steps": 3409,
    "sleep_state": "awake",
    "sleep_prob": 0.005,
    "sleep_24h": {
        "23:00-07:00": "sleeping",
        "07:00-10:00": "awake",
    },
}

HISTORY_PAYLOAD = {
    "summary": {
        "days_with_data": 7,
        "avg_duration_min": 372.7,
        "avg_efficiency": 96.1,
        "avg_deep_min": 95.3,
    },
    "days": [
        {
            "date": "2026-07-16",
            "duration_min": 313,
            "efficiency": 97,
            "deep_min": 88,
            "no_data": False,
        }
    ],
}


def test_current_projection_only_reads_health_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def get(url: str, **_: Any) -> Response:
        calls.append(url)
        return Response(CURRENT_PAYLOAD)

    monkeypatch.setattr(plugin.requests, "get", get)
    monkeypatch.setattr(plugin, "_MONITOR_URL", "http://monitor")
    overview = FitbitMobileDashboardReader().get_current()

    assert calls == ["http://monitor/api/tool/fitbit_health_snapshot"]
    assert overview["freshness"] == {
        "last_updated": "10:36:17",
        "data_lag_min": 3,
        "spo2_lag_min": 99,
    }
    assert overview["current"] == {
        "heart_rate": 110,
        "spo2": 93.8,
        "steps": 3409,
        "sleep_state": "awake",
        "sleep_prob": 0.005,
    }
    assert overview["sleep_24h"] == [
        {"range": "23:00-07:00", "state": "sleeping", "duration_min": 480},
        {"range": "07:00-10:00", "state": "awake", "duration_min": 180},
    ]


def test_sleep_history_projection_only_reads_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def get(url: str, **kwargs: Any) -> Response:
        calls.append((url, kwargs.get("params")))
        return Response(HISTORY_PAYLOAD)

    monkeypatch.setattr(plugin.requests, "get", get)
    monkeypatch.setattr(plugin, "_MONITOR_URL", "http://monitor")
    overview = FitbitMobileDashboardReader().get_sleep_history()

    assert calls == [("http://monitor/api/sleep_report", {"days": 7})]
    assert overview["sleep_days"] == [
        {
            "date": "2026-07-16",
            "duration_min": 313,
            "efficiency": 97,
            "deep_min": 88,
            "no_data": False,
        }
    ]
    assert overview["available"] is True


def test_sleep_history_projects_expired_oauth_as_a_panel_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        plugin.requests,
        "get",
        lambda *args, **kwargs: Response({"error": "unauthorized"}, 401),
    )

    assert FitbitMobileDashboardReader().get_sleep_history() == {
        "available": False,
        "reason": "fitbit_oauth_required",
        "sleep_summary": {
            "days_with_data": 0,
            "avg_duration_min": None,
            "avg_efficiency": None,
            "avg_deep_min": None,
        },
        "sleep_days": [],
    }


def test_current_minute_sleep_segment_keeps_a_visible_duration() -> None:
    assert plugin._range_duration_minutes("03:08-03:08") == 1


@pytest.mark.parametrize("value", ["24:00-01:00", "23:60-01:00", "-1:00-01:00"])
def test_sleep_segment_rejects_out_of_range_clock(value: str) -> None:
    with pytest.raises(ValueError, match="睡眠时间段无效"):
        plugin._range_duration_minutes(value)


def test_sleep_timeline_rejects_unknown_monitor_state() -> None:
    payload = {**CURRENT_PAYLOAD, "sleep_24h": {"23:00-07:00": "restless"}}

    with pytest.raises(TypeError, match="状态无效: restless"):
        plugin._sleep_segments(payload)


def test_malformed_current_payload_does_not_couple_sleep_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get(url: str, **_: Any) -> Response:
        if url.endswith("fitbit_health_snapshot"):
            return Response([])
        return Response(HISTORY_PAYLOAD)

    monkeypatch.setattr(plugin.requests, "get", get)

    with pytest.raises(TypeError, match="返回非对象"):
        FitbitMobileDashboardReader().get_current()
    assert FitbitMobileDashboardReader().get_sleep_history()["sleep_summary"] == {
        "days_with_data": 7,
        "avg_duration_min": 372.7,
        "avg_efficiency": 96.1,
        "avg_deep_min": 95.3,
    }
