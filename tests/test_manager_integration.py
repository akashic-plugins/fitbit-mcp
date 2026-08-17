from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from agent.plugins.generation_activity_host import ActivityHost
from agent.plugins.generation_proactive_host import ProactiveActivityAdapter
from agent.plugins.generation import PluginGeneration
from agent.plugins.manager import PluginManager
from agent.plugins.snapshot import RuntimeSnapshot
from bus.event_bus import EventBus


ROOT = Path(__file__).resolve().parents[1]


def _stage_plugin(tmp_path: Path) -> Path:
    """复制可执行 artifact，并复用当前测试解释器的依赖环境。"""

    source = tmp_path / "plugins" / "fitbit"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "node_modules",
        ),
    )
    (source / ".venv").symlink_to(
        Path(sys.executable).parent.parent,
        target_is_directory=True,
    )
    return source


@pytest.mark.asyncio
async def test_manager_rebuilds_fitbit_runtime_on_exact_formal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 formal boot 与 candidate 重建共享声明而不共享 Root owner。"""

    # 1. 正式启动真实 monitor/MCP handshake，但不调用 Fitbit 外部 API。
    plugin_root = _stage_plugin(tmp_path)
    manager = PluginManager(
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=tmp_path / "workspace",
        installed_cache_root=tmp_path / "home" / "cache",
    )
    activity = ActivityHost(
        (ProactiveActivityAdapter(manager.composition_generation_host),)
    )
    manager.bind_activity_host(activity)
    stable_snapshot = None
    validation_root = None
    try:
        await manager.load_all()
        stable_snapshot = manager.current_snapshot
        assert stable_snapshot is not None
        assert stable_snapshot.composition_root is not None
        assert stable_snapshot.mcp_server_registry is not None
        assert stable_snapshot.managed_process_registry is not None
        assert stable_snapshot.proactive_component_catalog is not None
        assert stable_snapshot.mobile_ui_registry is not None
        stable_generation = next(iter(stable_snapshot.generations.values()))
        stable_runtime = manager.composition_generation_host.get(
            stable_generation.generation_id
        )
        assert stable_runtime is not None and stable_runtime.mode == "formal"
        assert stable_runtime.processes is not None
        assert stable_runtime.processes.endpoint("monitor").port == 18765
        assert stable_runtime.mcp is not None
        stable_route = stable_runtime.mcp.server("fitbit").route()
        assert stable_route.mode == "formal"
        await stable_route.aclose()

        # 2. 新版本先在隔离 Root 中验证，再重建 formal Root。
        for relative in ("plugin.py", "akashic.plugin.toml"):
            path = plugin_root / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace("3.0.0", "3.0.1"),
                encoding="utf-8",
            )
        candidate = await manager.prepare_candidate("fitbit")
        assert candidate is not None and candidate.runtime_snapshot is not None
        assert candidate.validation_workspace is not None
        validation_root = candidate.validation_workspace.parent
        candidate_snapshot = candidate.runtime_snapshot
        assert candidate_snapshot.composition_root is not None
        assert candidate_snapshot.proactive_component_catalog is not None
        assert (
            candidate_snapshot.proactive_component_catalog.root_instance_token
            is candidate_snapshot.composition_root.instance_token
        )
        original_invariants = manager._post_publish_invariants  # pyright: ignore[reportPrivateUsage]
        candidate_checked = False

        async def inspect_candidate_runtime(
            generation: PluginGeneration,
            snapshot: RuntimeSnapshot,
        ) -> None:
            nonlocal candidate_checked
            candidate_runtime = manager.composition_generation_host.get(
                generation.generation_id
            )
            assert candidate_runtime is not None
            assert candidate_runtime.mode == "candidate"
            assert candidate_runtime.mcp is not None
            async with candidate_runtime.mcp.route("fitbit") as candidate_route:
                assert set(candidate_route.tool_names) == {
                    "get_proactive_events",
                    "get_sleep_context",
                }
                proactive = await candidate_route.call("get_proactive_events", {})
                sleep = await candidate_route.call("get_sleep_context", {})
                assert json.loads(proactive.output) == {"status": "empty"}
                assert json.loads(sleep.output) == {"status": "empty"}
                with pytest.raises(PermissionError, match="未获 allowlist 授权"):
                    _ = await candidate_route.call(
                        "acknowledge_events",
                        {"event_ids": ["event-1"]},
                    )
            candidate_checked = True
            await original_invariants(generation, snapshot)

        monkeypatch.setattr(
            manager,
            "_post_publish_invariants",
            inspect_candidate_runtime,
        )
        result = await manager.publish_prepared("fitbit")
        assert result["publication_state"] == "committed"
        assert candidate_checked
        final_snapshot = manager.current_snapshot
        assert final_snapshot is not None and final_snapshot.composition_root is not None
        assert final_snapshot.composition_root is not candidate_snapshot.composition_root
        assert final_snapshot.proactive_component_catalog is not None
        assert (
            final_snapshot.proactive_component_catalog.root_instance_token
            is final_snapshot.composition_root.instance_token
        )
        assert not validation_root.exists()
    finally:
        await manager.terminate_all()

    # 3. Manager 终止后进程、MCP、Activity 与 Root effects 全部归零。
    assert activity.active is None
    assert stable_snapshot is not None and stable_snapshot.composition_root is not None
    assert stable_snapshot.composition_root.receipt().effects == ()
    assert stable_snapshot.composition_root.topology_view().listeners == ()
