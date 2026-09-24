from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from agent.plugin_composition.tasks import TaskAdmission
from agent.control.timer import AsyncioOneShotTimer
from agent.plugin_composition.host import HOST_INFO, HostInfo
from fitbit_test_plugin.tools import FITBIT_TOOLS  # pyright: ignore[reportMissingImports]  # conftest 注册测试包。

import pytest
from agent.host_bridge.plugin_execution import CodeOwner, ExecutionAccess
from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    TIMERS,
    UI_SLOTS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
)
from agent.plugin_composition.execution import EXECUTION
from agent.plugin_composition.process_slots import ManagedProcessDefinition
from agent.plugin_composition.ui import DASHBOARD_ROUTES, UI
from contextlib import asynccontextmanager
from plugins.mcp.plugin import McpServers
from plugins.ui.mobile import MobileUiSlots
from plugins.ui.plugin import Ui
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


class _ProcessHandle:
    """记录式进程句柄；单测只固定声明，不启动真实 monitor。"""

    def port(self, ctx: object) -> int:
        return 18765

    @asynccontextmanager
    async def borrow(self, ctx: object):
        yield 18765

    async def aclose(self) -> None:
        return None


class _Processes:
    def __init__(self) -> None:
        self.definitions: dict[str, ManagedProcessDefinition] = {}

    async def register(self, ctx: object, definition: ManagedProcessDefinition) -> _ProcessHandle:
        self.definitions[definition.name] = definition
        return _ProcessHandle()


def _execution(root: CompositionRoot, generation_id: str) -> ExecutionAccess:
    return ExecutionAccess(
        root.instance_token,
        {"fitbit": CodeOwner(generation_id, ROOT, lambda command, cwd: command)},
        candidate=True,
    )


async def _mount_services(root: CompositionRoot, tmp_path: Path) -> None:
    class Sources:
        def bind(self, source_id: str) -> object:
            class Bound:
                def close(self) -> None:
                    return None

            return Bound()

    await root.context.provide(TIMERS, PluginTimers(AsyncioOneShotTimer()))
    _ = await root.context.provide(EVENTMAIL_ALERT_SOURCE, Sources())
    _ = await root.context.provide(EVENTMAIL_CONTEXT_SOURCE, Sources())


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin_module.api_version == 3
    assert plugin_module.name == "fitbit"
    assert plugin_module.version == "3.2.4"
    assert tuple(inspect.signature(plugin_module.apply).parameters) == ("ctx",)
    loaded = ComposablePlugin.from_module(
        plugin_module, load_static_plugin_manifest(ROOT),
    )
    assert UI in loaded.inject and UI_SLOTS in loaded.inject
    assert "PROACTIVE_COMPONENTS" not in ROOT.joinpath("plugin.py").read_text()


@pytest.mark.asyncio
async def test_apply_registers_wake_runtime_tools_and_mobile_ui(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("fitbit:test")
    processes = _Processes()
    servers = McpServers(root.context)
    ui_slots = MobileUiSlots(root.context)
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(EXECUTION, _execution(root, "fitbit:test"))
    await root.context.provide(TOOLS, ToolCatalog(root.context, cast(TaskAdmission, None)))
    await root.context.provide(UI, Ui(root.context))
    await root.context.provide(DASHBOARD_ROUTES, ())
    await root.context.provide(HOST_INFO, HostInfo(boot_id="fitbit-test", validation=False))
    await root.context.provide(UI_SLOTS, ui_slots)
    await _mount_services(root, tmp_path)
    data_dir = tmp_path / "plugin-data"

    plugin = ComposablePlugin.from_module(
        plugin_module, load_static_plugin_manifest(ROOT),
    )
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
            config=FitbitConfig().model_dump(mode="json"),
        ),
    )

    assert root.receipt().ready, root.receipt().incidents
    process = processes.definitions["monitor"]
    mcp = servers._entries["fitbit"].definition
    mobile = ui_slots._registrations["fitbit"].descriptor
    assert process.cwd == "."
    assert process.port_env == "FITBIT_MONITOR_PORT"
    assert mcp.required_tools == ("fitbit_health_snapshot", "fitbit_sleep_report")
    assert mcp.candidate_env == {"FITBIT_BACKEND": "recording"}
    assert mobile.navigation_label == "健康状态"
    assert data_dir.joinpath("adapter.sqlite3").is_file()
    assert any(item["name"].startswith("mcp_fitbit__") for item in (ref.description for ref in root.context.require(FITBIT_TOOLS).refs))
    await root.dispose()


@pytest.mark.asyncio
async def test_apply_keeps_tools_and_mobile_ui_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("fitbit:without-eventmail")
    processes = _Processes()
    servers = McpServers(root.context)
    ui_slots = MobileUiSlots(root.context)
    await root.context.provide(MANAGED_PROCESSES, processes)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(EXECUTION, _execution(root, "fitbit:without-eventmail"))
    await root.context.provide(TOOLS, ToolCatalog(root.context, cast(TaskAdmission, None)))
    await root.context.provide(TIMERS, PluginTimers(AsyncioOneShotTimer()))
    await root.context.provide(UI, Ui(root.context))
    await root.context.provide(DASHBOARD_ROUTES, ())
    await root.context.provide(HOST_INFO, HostInfo(boot_id="fitbit-test", validation=False))
    await root.context.provide(UI_SLOTS, ui_slots)
    plugin = ComposablePlugin.from_module(
        plugin_module, load_static_plugin_manifest(ROOT),
    )
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
            config=FitbitConfig().model_dump(mode="json"),
        ),
    )

    assert root.receipt().ready, root.receipt().incidents
    assert "fitbit" in servers._entries
    assert "fitbit" in ui_slots._registrations
    assert not (tmp_path / "plugin-data/adapter.sqlite3").exists()
    assert any(item["name"].startswith("mcp_fitbit__") for item in (ref.description for ref in root.context.require(FITBIT_TOOLS).refs))
    await root.dispose()


def test_static_manifest_freezes_runtime_and_candidate_exclusions() -> None:
    manifest = load_static_plugin_manifest(ROOT)
    assert manifest.name == "fitbit"
    assert manifest.version == "3.2.4"
    assert manifest.requirements == ("requirements.txt",)
