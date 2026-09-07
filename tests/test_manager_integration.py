from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from agent.plugins.generation import PluginGeneration
from agent.plugins.manager import PluginManager
from agent.plugins.python_environment import ENVIRONMENT_FILE, PythonEnvironments
from agent.plugins.static_manifest import load_static_plugin_manifest
from agent.plugins.snapshot import RuntimeSnapshot
from bus.event_bus import EventBus
from plugins.eventmail import plugin as content_plugin


ROOT = Path(__file__).resolve().parents[1]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _stage_plugin(tmp_path: Path) -> Path:
    """复制可执行 artifact，并挂载调用方明确选择的依赖环境。"""

    fixture_python = Path(os.environ["AKASHIC_PLUGIN_FIXTURE_PYTHON"])
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
    (source / ".venv").symlink_to(fixture_python.parent.parent, target_is_directory=True)
    content_source = Path(content_plugin.__file__).resolve().parent
    content_target = source.parent / "content"
    shutil.copytree(content_source, content_target)
    return source


def _prepare_python_environment(source: Path, workspace: Path) -> None:
    """通过安装 owner 为测试 artifact 固定独立 Python 环境。"""

    manifest = load_static_plugin_manifest(source)
    environments = PythonEnvironments(workspace)
    refs = {
        item.runtime_root: environments.prepare(source, item)
        for item in manifest.python
    }
    (source / ENVIRONMENT_FILE).write_text(json.dumps(refs), encoding="utf-8")


def test_stage_plugin_uses_explicit_fixture_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_python = tmp_path / "artifact" / ".venv" / "bin" / "python"
    monkeypatch.setenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", str(artifact_python))

    plugin_root = _stage_plugin(tmp_path / "stage")

    assert (plugin_root / ".venv").readlink() == artifact_python.parent.parent


def test_stage_plugin_requires_explicit_fixture_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", raising=False)

    with pytest.raises(KeyError, match="AKASHIC_PLUGIN_FIXTURE_PYTHON"):
        _stage_plugin(tmp_path)

    assert not (tmp_path / "plugins").exists()


def test_ci_creates_and_exports_absolute_fixture_python_before_pytest() -> None:
    workflow = (ROOT / ".github/workflows/plugin-api-v3.yml").read_text(
        encoding="utf-8"
    )

    create_runtime = workflow.index("python -m venv .venv")
    export_runtime = workflow.index(
        "AKASHIC_PLUGIN_FIXTURE_PYTHON: ${{ github.workspace }}/.venv/bin/python"
    )
    run_pytest = workflow.index("run: .venv/bin/python -m pytest -q tests/")

    assert create_runtime < export_runtime < run_pytest


@pytest.mark.asyncio
async def test_manager_rebuilds_fitbit_runtime_on_exact_formal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 formal boot 与 candidate 重建共享声明而不共享 Root owner。"""

    # 1. 正式启动真实 monitor/MCP handshake，但不调用 Fitbit 外部 API。
    plugin_root = _stage_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    _prepare_python_environment(plugin_root, workspace)
    manager = PluginManager(
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
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
        await stable_route.aclose()
        formal_data = tmp_path / "workspace/plugin-data/fitbit-builtin"
        formal_digest = _tree_digest(formal_data)

        # 2. 新版本先在隔离 Root 中验证，再重建 formal Root。
        for relative in ("plugin.py", "akashic.plugin.toml"):
            path = plugin_root / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace("3.2.1", "3.2.2"),
                encoding="utf-8",
            )
        _prepare_python_environment(plugin_root, workspace)
        candidate = await manager.prepare_candidate("fitbit")
        assert candidate is not None and candidate.runtime_snapshot is not None
        assert candidate.validation_workspace is not None
        validation_root = candidate.validation_workspace.parent
        candidate_snapshot = candidate.runtime_snapshot
        assert candidate_snapshot.composition_root is not None
        assert candidate_snapshot.composition_root.receipt().optional_pending == ()
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
            assert candidate_runtime.processes is not None
            candidate_port = candidate_runtime.processes.endpoint("monitor").port
            assert candidate_port != 18765
            candidate_server = candidate_runtime.mcp.server("fitbit")
            assert set(candidate_server.tool_names) == {
                "fitbit_health_snapshot",
                "fitbit_sleep_report",
            }
            async with candidate_server.route() as candidate_route:
                result = await candidate_route.call("fitbit_health_snapshot", {})
                assert result.success
                assert '"available"' in result.output
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
