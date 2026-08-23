from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest
from agent.plugin_composition import TIMERS
from agent.plugins.generation import PluginGeneration
from agent.plugins.manager import PluginManager
from agent.plugins.snapshot import RuntimeSnapshot
from bus.event_bus import EventBus
from plugins.content import plugin as content_plugin


ROOT = Path(__file__).resolve().parents[1]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _stage_plugin(tmp_path: Path) -> Path:
    """复制可执行 artifact，并复用当前测试解释器的依赖环境。"""

    source = tmp_path / "plugins" / "fitbit"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".akashic-core",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "node_modules",
        ),
    )
    (source / ".venv").symlink_to(
        Path(sys.executable).parent.parent,
        target_is_directory=True,
    )
    content_source = Path(content_plugin.__file__).resolve().parent
    content_target = source.parent / "content"
    shutil.copytree(content_source, content_target)
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
    stable_snapshot = None
    validation_root = None
    try:
        await manager.load_all()
        stable_snapshot = manager.current_snapshot
        assert stable_snapshot is not None
        assert stable_snapshot.composition_root is not None
        assert stable_snapshot.mcp_server_registry is not None
        assert stable_snapshot.managed_process_registry is not None
        assert stable_snapshot.mobile_ui_registry is not None
        stable_generation = stable_snapshot.generations["fitbit"]
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
        formal_data = tmp_path / "workspace/plugin-data/fitbit-builtin"
        formal_digest = _tree_digest(formal_data)

        # 2. 新版本先在隔离 Root 中验证，再重建 formal Root。
        for relative in ("plugin.py", "akashic.plugin.toml"):
            path = plugin_root / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace("3.1.0", "3.1.1"),
                encoding="utf-8",
            )
        candidate = await manager.prepare_candidate("fitbit")
        assert candidate is not None and candidate.runtime_snapshot is not None
        assert candidate.validation_workspace is not None
        validation_root = candidate.validation_workspace.parent
        candidate_snapshot = candidate.runtime_snapshot
        assert candidate_snapshot.composition_root is not None
        assert candidate_snapshot.composition_root.context.require(TIMERS).formal is False
        assert candidate.validation_workspace != tmp_path / "workspace"
        assert _tree_digest(formal_data) == formal_digest
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
                    "fitbit_health_snapshot",
                    "fitbit_sleep_report",
                }
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
        assert not validation_root.exists()
    finally:
        await manager.terminate_all()

    # 3. Manager 终止后进程、MCP 与 Root effects 全部归零。
    assert stable_snapshot is not None and stable_snapshot.composition_root is not None
    assert stable_snapshot.composition_root.receipt().effects == ()
    assert stable_snapshot.composition_root.topology_view().listeners == ()
