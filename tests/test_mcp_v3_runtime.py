from __future__ import annotations

import json
from typing import cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src import mcp_bridge


async def _call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    _, structured = await mcp_bridge.create_mcp_server().call_tool(name, arguments)
    result = cast(dict[str, object], cast(object, structured)).get("result")
    assert isinstance(result, str)
    payload = json.loads(result)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.asyncio
async def test_recording_backend_is_typed_empty_without_monitor_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FITBIT_BACKEND", "recording")

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(mcp_bridge.requests, "get", forbidden)
    monkeypatch.setattr(mcp_bridge.requests, "post", forbidden)

    assert await _call("get_proactive_events", {}) == {"status": "empty"}
    assert await _call("get_sleep_context", {}) == {"status": "empty"}
    assert await _call("acknowledge_events", {"event_ids": []}) == {
        "status": "skipped",
        "reason": "no_ids",
    }
    with pytest.raises(ToolError, match="recording backend 不允许确认事件"):
        _ = await _call("acknowledge_events", {"event_ids": ["event-1"]})


@pytest.mark.asyncio
async def test_formal_fetch_and_ack_encode_explicit_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FITBIT_BACKEND", raising=False)
    monkeypatch.setattr(
        mcp_bridge,
        "_fetch_agent_payload",
        lambda timeout: {
            "health_events": [
                {
                    "id": "event-1",
                    "type": "high_hr",
                    "message": "心率偏高",
                    "severity": "high",
                }
            ]
        },
    )

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"acknowledged": True}

    monkeypatch.setattr(mcp_bridge.requests, "post", lambda *args, **kwargs: Response())

    fetched = await _call("get_proactive_events", {})
    assert fetched["status"] == "items"
    items = cast(list[dict[str, object]], fetched["items"])
    assert [item["event_id"] for item in items] == ["event-1"]
    assert await _call("acknowledge_events", {"event_ids": ["event-1"]}) == {
        "status": "committed",
        "ids": ["event-1"],
    }
