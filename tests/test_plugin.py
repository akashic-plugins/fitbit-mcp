from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    UI_SLOTS,
    CompositionRoot,
    PluginProactiveComponents,
    PluginRuntime,
    PluginUiSlots,
)
from agent.plugin_composition.mcp_slots import (
    PluginMcpServers,
    _freeze_plugin_mcp_servers,
)
from agent.plugin_composition.proactive import _freeze_plugin_proactive_components
from agent.plugin_composition.process_slots import (
    PluginManagedProcesses,
    _freeze_plugin_managed_processes,
)
from agent.plugins.composable import ComposablePlugin
from agent.plugins import manager as manager_module
from agent.plugins.static_manifest import load_static_plugin_manifest

import plugin as plugin_module
from plugin import FitbitConfig, _mobile_ui_query


ROOT = Path(__file__).resolve().parents[1]

def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin_module.api_version == 3
    assert plugin_module.name == "fitbit"
    assert plugin_module.version == "3.0.0"
    assert tuple(inspect.signature(plugin_module.apply).parameters) == ("ctx", "config")
    assert ComposablePlugin.from_module(plugin_module).dashboard_module == "dashboard.py"


@pytest.mark.asyncio
async def test_apply_registers_exact_runtime_sources_and_mobile_ui(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("fitbit:test")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    components = PluginProactiveComponents(root.instance_token)
    ui_slots = PluginUiSlots()
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(PROACTIVE_COMPONENTS, components)
    await root.context.provide(UI_SLOTS, ui_slots)
    data_dir = tmp_path / "plugin-data"
    await root.mount(
        ComposablePlugin.from_module(plugin_module),
        name="fitbit",
        runtime=PluginRuntime(
            plugin_id="fitbit",
            plugin_dir=ROOT,
            data_dir=data_dir,
            workspace=tmp_path / "workspace",
            config=FitbitConfig(),
        ),
    )

    process = _freeze_plugin_managed_processes(
        processes,
        root.instance_token,
    )["monitor"].definition
    mcp = _freeze_plugin_mcp_servers(
        servers,
        root.instance_token,
    )["fitbit"].definition
    proactive = _freeze_plugin_proactive_components(
        components,
        root.instance_token,
        {"fitbit": "fitbit:test"},
    )
    mobile = ui_slots.freeze()["fitbit"]
    assert process.cwd == "."
    assert process.port_env == "FITBIT_MONITOR_PORT"
    assert mcp.candidate_env == {"FITBIT_BACKEND": "recording"}
    assert [item.definition.name for item in proactive.sources.values()] == [
        "health_alerts",
        "sleep_context",
    ]
    assert mobile.descriptor.navigation_label == "健康状态"
    assert not data_dir.exists()
    await root.dispose()


@pytest.mark.asyncio
async def test_disabled_proactive_omits_sources(tmp_path: Path) -> None:
    root = CompositionRoot("fitbit:disabled")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    components = PluginProactiveComponents(root.instance_token)
    ui_slots = PluginUiSlots()
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(PROACTIVE_COMPONENTS, components)
    await root.context.provide(UI_SLOTS, ui_slots)
    await root.mount(
        ComposablePlugin.from_module(plugin_module),
        name="fitbit",
        runtime=PluginRuntime(
            plugin_id="fitbit",
            plugin_dir=ROOT,
            data_dir=tmp_path / "plugin-data",
            workspace=tmp_path / "workspace",
            config=FitbitConfig.model_validate({"proactive": {"enabled": False}}),
        ),
    )
    catalog = _freeze_plugin_proactive_components(
        components,
        root.instance_token,
        {"fitbit": "fitbit:disabled"},
    )
    assert catalog.sources == {}
    await root.dispose()


def test_static_manifest_freezes_runtime_and_candidate_exclusions() -> None:
    manifest = load_static_plugin_manifest(ROOT)
    assert manifest.name == "fitbit"
    assert manifest.version == "3.0.0"
    assert manifest.requirements == ("requirements.txt",)
    assert len(manifest.managed_processes) == 1
    assert manifest.managed_processes[0].formal_port == 18765
    assert manifest.mcp_servers[0].candidate_env == (("FITBIT_BACKEND", "recording"),)
    assert "tokens.json" in manifest.exclude_data_paths
    assert "monitor.config.local.toml" in manifest.exclude_data_paths


def test_candidate_copy_omits_fitbit_credentials(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "plugin-data" / "fitbit-github"
    target = tmp_path / "candidate-data"
    source.mkdir(parents=True)
    (source / "tokens.json").write_text('{"access":"candidate-secret"}\n')
    (source / "monitor.config.local.toml").write_text("token='candidate-secret'\n")
    (source / "mobile_sleep_projection.json").write_text('{"status":"ok"}\n')
    manifest = load_static_plugin_manifest(ROOT)

    inventory = manager_module._copy_validation_data(  # pyright: ignore[reportPrivateUsage]
        source,
        target,
        manifest.exclude_data_paths,
    )

    assert inventory == ()
    assert not (target / "tokens.json").exists()
    assert not (target / "monitor.config.local.toml").exists()
    assert b"candidate-secret" not in b"".join(
        path.read_bytes() for path in target.rglob("*") if path.is_file()
    )


def test_mobile_health_panel_uses_reader_and_rejects_unknown_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current: dict[str, object] = {"current": {"heart_rate": 72}}
    history: dict[str, object] = {"sleep_days": []}

    class Reader:
        def get_current(self) -> dict[str, object]:
            return current

        def get_sleep_history(self) -> dict[str, object]:
            return history

    monkeypatch.setattr(plugin_module, "FitbitMobileDashboardReader", Reader)
    current_result = _mobile_ui_query(
        "fitbit.current",
        {},
        session_id=None,
        turn_id=None,
    )
    history_result = _mobile_ui_query(
        "fitbit.sleep_history",
        {},
        session_id=None,
        turn_id=None,
    )
    assert current_result == current
    assert history_result == history
    with pytest.raises(ValueError, match="未知 fitbit 移动方法"):
        _mobile_ui_query(
            "fitbit.write",
            {},
            session_id=None,
            turn_id=None,
        )
