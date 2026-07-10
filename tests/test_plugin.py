from __future__ import annotations

from plugin import FitbitConfig, FitbitPlugin


def test_declares_mcp_and_both_proactive_channels() -> None:
    plugin = FitbitPlugin()
    plugin.context = type("Context", (), {"config": FitbitConfig()})()

    assert [server.name for server in plugin.mcp_servers()] == ["fitbit"]
    sources = plugin.proactive_sources()
    assert [source.id for source in sources] == ["health_alerts", "sleep_context"]
    assert [source.channels for source in sources] == [("alert",), ("context",)]


def test_proactive_can_be_disabled() -> None:
    plugin = FitbitPlugin()
    plugin.context = type(
        "Context",
        (),
        {"config": FitbitConfig.model_validate({"proactive": {"enabled": False}})},
    )()

    assert plugin.proactive_sources() == []
