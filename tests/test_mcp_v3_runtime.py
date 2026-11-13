from __future__ import annotations

import json
from typing import cast

import pytest

from src import mcp_bridge


async def _call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    _, structured = await mcp_bridge.create_mcp_server().call_tool(name, arguments)
    result = cast(dict[str, object], cast(object, structured)).get("result")
    assert isinstance(result, str)
    payload = json.loads(result)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.asyncio
async def test_mcp_exposes_only_ordinary_fitbit_read_tools() -> None:
    tools = await mcp_bridge.create_mcp_server().list_tools()
    assert [tool.name for tool in tools] == [
        "fitbit_health_snapshot",
        "fitbit_sleep_report",
        "fitbit_workouts",
        "fitbit_activity_report",
    ]


@pytest.mark.asyncio
async def test_health_snapshot_uses_monitor_read_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"available": True, "heart_rate": 72}

    def get(url: str, **kwargs: object) -> Response:
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(mcp_bridge.requests, "get", get)
    assert await _call("fitbit_health_snapshot", {}) == {
        "available": True,
        "heart_rate": 72,
    }
    assert calls[0][0].endswith("/api/tool/fitbit_health_snapshot")


@pytest.mark.asyncio
async def test_sleep_report_bounds_days_and_preserves_unauthorized_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 401

    def get(_url: str, **kwargs: object) -> Response:
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(mcp_bridge.requests, "get", get)
    assert await _call("fitbit_sleep_report", {"days": 100}) == {
        "error": "Fitbit 未授权，请先完成 OAuth 授权。"
    }
    assert calls == [{"params": {"days": 30}, "timeout": 10}]


@pytest.mark.asyncio
async def test_workouts_and_activity_report_bound_params_and_forward_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"available": True, "count": 1}

    def get(url: str, **kwargs: object) -> Response:
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(mcp_bridge.requests, "get", get)
    assert await _call(
        "fitbit_workouts", {"days": 200, "exercise_type": " BIKING "}
    ) == {"available": True, "count": 1}
    assert await _call("fitbit_activity_report", {"days": 0}) == {
        "available": True,
        "count": 1,
    }
    assert calls[0][0].endswith("/api/tool/fitbit_workouts")
    assert calls[0][1] == {
        "params": {"days": 90, "exercise_type": "BIKING"},
        "timeout": 15,
    }
    assert calls[1][0].endswith("/api/tool/fitbit_activity_report")
    assert calls[1][1] == {"params": {"days": 1}, "timeout": 20}
