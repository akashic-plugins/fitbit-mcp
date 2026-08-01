from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse


_MONITOR_URL = "http://127.0.0.1:18765"


def register(app: FastAPI, plugin_dir: object, workspace: object) -> None:
    """Expose the monitor's current snapshot through the Akashic Dashboard."""

    _ = plugin_dir, workspace

    @app.get("/api/dashboard/fitbit/overview")
    def overview() -> dict[str, object]:
        # 1. Read the poll-owned compact state once at the local HTTP boundary.
        snapshot = _monitor_json("/api/dashboard/snapshot")

        # 2. Project only the fields rendered by the Dashboard first screen.
        return _project_dashboard_snapshot(snapshot)

    @app.post("/api/dashboard/fitbit/refresh", status_code=202)
    def refresh() -> dict[str, str]:
        _monitor_json("/api/refresh")
        return {"status": "refreshing"}

    @app.get("/api/dashboard/fitbit/auth/start")
    def auth_start() -> RedirectResponse:
        return RedirectResponse(f"{_MONITOR_URL}/auth/start")


def _monitor_json(path: str) -> Mapping[str, object]:
    payload = _monitor_payload(path)
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=502, detail=f"Fitbit monitor 返回非对象: {path}")
    return payload


def _monitor_payload(path: str) -> object:
    try:
        response = requests.get(f"{_MONITOR_URL}{path}", timeout=8)
        response.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(status_code=502, detail=f"Fitbit monitor 不可用: {path}") from error
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise HTTPException(status_code=502, detail=f"Fitbit monitor 返回无效 JSON: {path}") from error
    return payload


def _project_dashboard_snapshot(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate and project the monitor-owned compact snapshot."""

    data = _mapping(payload, "data")
    sleep_24h = _mapping(payload, "sleep_24h")
    prediction_events = payload.get("prediction_events")
    if not isinstance(prediction_events, list):
        raise HTTPException(
            status_code=502,
            detail="Fitbit monitor prediction_events 必须是数组",
        )
    return _project_overview(data, {"sleep_24h": sleep_24h}, prediction_events)


def _project_overview(
    data: Mapping[str, object],
    snapshot: Mapping[str, object],
    history: list[object] | None = None,
) -> dict[str, object]:
    """Validate monitor payloads and build the Dashboard first-screen DTO."""

    # 1. Validate the external payload structure once at the plugin boundary.
    summary = _mapping(data, "summary")
    sleep = _mapping(data, "sleep")
    signals = _mapping(data, "signals", required=False)
    freshness = _mapping(data, "data_meta", required=False)
    sleep_segments = _sleep_segments(snapshot)

    # 2. Keep the Dashboard projection small, current, and read-only.
    return {
        "last_updated": _optional_string(data, "last_updated"),
        "stale": _optional_boolean(data, "stale") or False,
        "current": {
            "heart_rate": _optional_number(summary, "heart_rate"),
            "spo2": _optional_number(summary, "spo2"),
            "steps": _optional_number(summary, "steps"),
            "sleep_state": _optional_string(sleep, "state") or "unknown",
            "sleep_reason": _optional_string(sleep, "reason") or "等待首轮判断",
            "sleep_since": _optional_string(sleep, "since"),
            "sleep_prob": _optional_number(signals, "sleep_prob"),
        },
        "freshness": {
            "data_lag_min": _optional_number(freshness, "data_lag_min"),
            "spo2_lag_min": _optional_number(freshness, "spo2_lag_min"),
            "poll_time": _optional_string(freshness, "poll_time"),
        },
        "signals": {
            "prob_source": _optional_string(signals, "prob_source"),
            "hr_avg": _optional_number(signals, "hr_avg"),
            "zero_steps_count": _optional_number(signals, "zero_steps_count"),
            "sustained_zero_min": _optional_number(signals, "sustained_zero_min"),
        },
        "sleep_24h": sleep_segments,
        "prediction_events": _prediction_events(history or []),
        "heart_rate_series": _series(data, "heart_rate", limit=60),
        "steps_series": _series(data, "steps", limit=60),
    }


def _prediction_events(rows: list[object]) -> list[dict[str, object]]:
    """Project stored model outputs separately from final sleep decisions."""

    events: list[dict[str, object]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise HTTPException(status_code=502, detail="Fitbit monitor sleep_log 条目无效")
        signals = _mapping(raw, "signals", required=False)
        probability = _optional_number(raw, "sleep_prob")
        if probability is None:
            probability = _optional_number(signals, "sleep_prob")
        source = _optional_string(signals, "prob_source")
        state = _optional_string(raw, "state") or "unknown"
        if state not in {"sleeping", "awake", "uncertain", "unknown"}:
            raise HTTPException(status_code=502, detail="Fitbit monitor sleep_log state 无效")
        events.append(
            {
                "time": _optional_string(raw, "poll_time"),
                "source": source or "unavailable",
                "sleep_probability": probability,
                "final_state": state,
                "reason": _optional_string(raw, "reason"),
                "changed": _optional_boolean(raw, "changed") or False,
            }
        )
    return events


def _mapping(
    payload: Mapping[str, object], name: str, *, required: bool = True
) -> Mapping[str, object]:
    value = payload.get(name)
    if value is None and not required:
        return {}
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=502, detail=f"Fitbit monitor {name} 必须是对象")
    return value


def _series(
    payload: Mapping[str, object], name: str, *, limit: int
) -> list[dict[str, object]]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise HTTPException(status_code=502, detail=f"Fitbit monitor {name} 必须是数组")
    points: list[dict[str, object]] = []
    for raw in value[-limit:]:
        if not isinstance(raw, Mapping):
            raise HTTPException(status_code=502, detail=f"Fitbit monitor {name} 条目无效")
        points.append(
            {
                "time": _optional_string(raw, "time"),
                "value": _optional_number(raw, "value"),
            }
        )
    return points


def _sleep_segments(payload: Mapping[str, object]) -> list[dict[str, object]]:
    value = payload.get("sleep_24h")
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=502, detail="Fitbit monitor sleep_24h 必须是对象")
    segments: list[dict[str, object]] = []
    for time_range, state in value.items():
        if not isinstance(time_range, str) or state not in {"sleeping", "awake", "unknown"}:
            raise HTTPException(status_code=502, detail="Fitbit monitor sleep_24h 条目无效")
        segments.append({"range": time_range, "state": state})
    return segments


def _optional_boolean(payload: Mapping[str, object], name: str) -> bool | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise HTTPException(status_code=502, detail=f"Fitbit monitor {name} 必须是布尔值或 null")
    return value


def _optional_number(payload: Mapping[str, object], name: str) -> int | float | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HTTPException(status_code=502, detail=f"Fitbit monitor {name} 必须是数字或 null")
    return value


def _optional_string(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=502, detail=f"Fitbit monitor {name} 必须是字符串或 null")
    return value
