from __future__ import annotations

import inspect
from pathlib import Path

from plugins.tools.plugin import TOOLS, ToolCatalog
from fitbit_test_plugin.tools import FITBIT_TOOLS

import pytest
from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    TIMERS,
    UI_SLOTS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
    PluginUiSlots,
)
from agent.plugin_composition.mcp_slots import (
    PluginMcpServers,
    _freeze_plugin_mcp_servers,
)
from agent.plugin_composition.process_slots import (
    PluginManagedProcesses,
    _freeze_plugin_managed_processes,
)
from agent.plugins import manager as manager_module
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from fitbit_test_plugin import plugin as plugin_module  # pyright: ignore[reportMissingImports]
from fitbit_test_plugin.plugin import FitbitConfig  # pyright: ignore[reportMissingImports]
from fitbit_test_plugin.src.eventmail import (  # pyright: ignore[reportMissingImports]
    EVENTMAIL_ALERT_SOURCE,
    EVENTMAIL_CONTEXT_SOURCE,
)
from src import mobile_reader
from src.mobile_reader import mobile_ui_query


ROOT = Path(__file__).resolve().parents[1]


async def _mount_services(root: CompositionRoot, tmp_path: Path) -> None:
    class Sources:
        def bind(self, source_id: str) -> object:
            return object()

    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    _ = await root.context.provide(EVENTMAIL_ALERT_SOURCE, Sources())
    _ = await root.context.provide(EVENTMAIL_CONTEXT_SOURCE, Sources())


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin_module.api_version == 3
    assert plugin_module.name == "fitbit"
    assert plugin_module.version == "3.2.3"
    assert tuple(inspect.signature(plugin_module.apply).parameters) == ("ctx", "config")
    assert (
        ComposablePlugin.from_module(plugin_module).dashboard_module == "dashboard.py"
    )
    assert "PROACTIVE_COMPONENTS" not in ROOT.joinpath("plugin.py").read_text()


@pytest.mark.asyncio
async def test_apply_registers_wake_runtime_tools_and_mobile_ui(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("fitbit:test")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    ui_slots = PluginUiSlots()
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TOOLS, ToolCatalog(root.context))
    await root.context.provide(UI_SLOTS, ui_slots)
    await _mount_services(root, tmp_path)
    data_dir = tmp_path / "plugin-data"

    plugin = ComposablePlugin.from_module(plugin_module)
    await root.mount(
        plugin.apply,
        name="fitbit",
        inject=plugin.inject,
        runtime=PluginRuntime(
            plugin_id="fitbit",
            generation_id="fitbit:test",
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
    mobile = ui_slots.freeze()["fitbit"]
    assert process.cwd == "."
    assert process.port_env == "FITBIT_MONITOR_PORT"
    assert mcp.required_tools == ("fitbit_health_snapshot", "fitbit_sleep_report")
    assert mcp.candidate_env == {"FITBIT_BACKEND": "recording"}
    assert mobile.descriptor.navigation_label == "健康状态"
    assert data_dir.joinpath("adapter.sqlite3").is_file()
    assert any(item["name"].startswith("mcp_fitbit__") for item in (ref.description for ref in root.context.require(FITBIT_TOOLS).refs))
    await root.dispose()


@pytest.mark.asyncio
async def test_apply_keeps_tools_and_mobile_ui_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("fitbit:without-eventmail")
    processes = PluginManagedProcesses(root.instance_token)
    servers = PluginMcpServers(root.instance_token)
    ui_slots = PluginUiSlots()
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TOOLS, ToolCatalog(root.context))
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    await root.context.provide(UI_SLOTS, ui_slots)
    plugin = ComposablePlugin.from_module(plugin_module)
    await root.mount(
        plugin.apply,
        name="fitbit",
        inject=plugin.inject,
        runtime=PluginRuntime(
            plugin_id="fitbit",
            generation_id="fitbit:without-eventmail",
            plugin_dir=ROOT,
            data_dir=tmp_path / "plugin-data",
            workspace=tmp_path / "workspace",
            config=FitbitConfig(),
        ),
    )

    assert "fitbit" in _freeze_plugin_mcp_servers(servers, root.instance_token)
    assert "fitbit" in ui_slots.freeze()
    assert not (tmp_path / "plugin-data/adapter.sqlite3").exists()
    assert any(item["name"].startswith("mcp_fitbit__") for item in (ref.description for ref in root.context.require(FITBIT_TOOLS).refs))
    await root.dispose()


def test_static_manifest_freezes_runtime_and_candidate_exclusions() -> None:
    manifest = load_static_plugin_manifest(ROOT)
    assert manifest.name == "fitbit"
    assert manifest.version == "3.2.3"
    assert manifest.requirements == ("requirements.txt",)
    assert len(manifest.managed_processes) == 1
    assert manifest.managed_processes[0].formal_port == 18765
    assert manifest.mcp_servers[0].required_tools == (
        "fitbit_health_snapshot",
        "fitbit_sleep_report",
    )
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

    inventory = manager_module._copy_validation_tree(  # pyright: ignore[reportPrivateUsage]
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

    monkeypatch.setattr(mobile_reader, "FitbitMobileDashboardReader", Reader)
    assert (
        mobile_ui_query("fitbit.current", {}, session_id=None, turn_id=None) == current
    )
    assert (
        mobile_ui_query("fitbit.sleep_history", {}, session_id=None, turn_id=None)
        == history
    )
    with pytest.raises(ValueError, match="未知 fitbit 移动方法"):
        mobile_ui_query("fitbit.write", {}, session_id=None, turn_id=None)
