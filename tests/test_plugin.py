from __future__ import annotations

import pytest

import plugin as plugin_module
from plugin import FitbitConfig, FitbitPlugin


def test_declares_mcp_and_both_proactive_channels() -> None:
    plugin = FitbitPlugin()
    plugin.context = type("Context", (), {"config": FitbitConfig()})()

    assert [server.name for server in plugin.mcp_servers()] == ["fitbit"]
    services = plugin.managed_services()
    assert [(service.id, service.cwd) for service in services] == [
        ("monitor", "monitor")
    ]
    sources = plugin.proactive_sources()
    assert [source.id for source in sources] == ["health_alerts", "sleep_context"]
    assert [source.channels for source in sources] == [("alert",), ("context",)]
    assert all(not hasattr(source, "poll_interval_seconds") for source in sources)


def test_proactive_can_be_disabled() -> None:
    plugin = FitbitPlugin()
    plugin.context = type(
        "Context",
        (),
        {"config": FitbitConfig.model_validate({"proactive": {"enabled": False}})},
    )()

    assert plugin.proactive_sources() == []


def test_declares_plugin_owned_mobile_health_panel() -> None:
    contribution = FitbitPlugin.mobile_ui()
    assert contribution.module == "mobile_panel.js"
    assert contribution.stylesheet == "mobile_panel.css"
    assert contribution.navigation.label == "健康状态"


def test_mobile_health_panel_uses_reader_and_rejects_unknown_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {"current": {"heart_rate": 72}}
    history = {"sleep_days": []}

    class Reader:
        def get_current(self) -> dict[str, object]:
            return current

        def get_sleep_history(self) -> dict[str, object]:
            return history

    monkeypatch.setattr(plugin_module, "FitbitMobileDashboardReader", Reader)
    plugin = FitbitPlugin()

    current_result = plugin.mobile_ui_query(
        "fitbit.current",
        {},
        session_id=None,
        turn_id=None,
    )
    history_result = plugin.mobile_ui_query(
        "fitbit.sleep_history",
        {},
        session_id=None,
        turn_id=None,
    )
    assert current_result == current
    assert history_result == history
    with pytest.raises(ValueError, match="未知 fitbit 移动方法"):
        plugin.mobile_ui_query(
            "fitbit.write",
            {},
            session_id=None,
            turn_id=None,
        )
