from datetime import UTC, datetime

from src import mcp_bridge


def test_sleep_context_exposes_wake_contract_and_preserves_payload() -> None:
    mcp_bridge._last_wake_presence = "unknown"

    context = mcp_bridge._build_sleep_context(
        {
            "sleep": {
                "state": "sleeping",
                "prob": 0.92,
                "prob_source": "model",
                "data_lag_min": 3,
            },
            "health_events": [{"id": "a"}],
        }
    )

    assert context["presence"] == "sleeping"
    assert context["interruptibility"] == 0.0
    assert context["confidence"] == 0.92
    assert context["transition"] == ""
    assert datetime.fromisoformat(context["expires_at"]) > datetime.fromisoformat(
        context["observed_at"]
    )
    assert context["payload"]["sleep"]["state"] == "sleeping"
    assert context["payload"]["health_event_count"] == 1


def test_sleep_owner_emits_generic_transition() -> None:
    mcp_bridge._last_wake_presence = "sleeping"
    observed = datetime(2026, 7, 12, 8, tzinfo=UTC)
    context = mcp_bridge._with_wake_contract(
        {"available": True, "sleep": {"state": "awake", "prob": 0.1}},
        state="awake",
        probability=0.1,
        observed_at=observed,
    )

    assert context["presence"] == "active"
    assert context["interruptibility"] == 0.85
    assert context["confidence"] == 0.9
    assert context["transition"] == "sleeping->active"
    assert context["observed_at"] == observed.isoformat()


def test_unavailable_context_still_has_complete_contract() -> None:
    context = mcp_bridge._unavailable_sleep_context("offline")

    assert context["available"] is False
    assert context["presence"] == "unknown"
    assert context["confidence"] == 0.0
    assert context["payload"]["hint"] == "offline"
