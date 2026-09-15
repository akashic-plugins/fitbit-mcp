from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from agent.plugin_composition import MANAGED_PROCESSES, MCP_SERVERS, UI_SLOTS
from agent.plugins.generation import PluginGeneration
from agent.plugins.selection import PluginSelection
from session.log import MessageLog
from agent.plugins.manager import PluginManager
from agent.plugins.python_environment import ENVIRONMENT_FILE, PythonEnvironments
from agent.plugins.static_manifest import load_static_plugin_manifest
from agent.plugins.snapshot import RuntimeSnapshot
from bus.event_bus import EventBus
from plugins.content import plugin as content_plugin


ROOT = Path(__file__).resolve().parents[1]


def _tree_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(_tree_files(root).items()):
        digest.update(name.encode())
        digest.update(value.encode())
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
    shutil.copytree(content_source, source.parent / "eventmail")
    core_plugins = Path(os.environ["AKASHIC_AGENT_ROOT"]) / "plugins"
    for provider in ("content", "mcp", "managed_processes", "tools", "ui"):
        shutil.copytree(core_plugins / provider, source.parent / provider)
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
    log = MessageLog(tmp_path / "sessions.db")
    workspace.mkdir(exist_ok=True)
    PluginSelection(workspace).initialize()
    manager = PluginManager(
        message_log=log,
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        workspace=workspace,
        installed_cache_root=tmp_path / "home" / "cache",
    )
    stable_snapshot = None
    validation_root = None
    try:
        await manager.load_all()
        stable_snapshot = manager.current_snapshot
        assert stable_snapshot is not None
        stable_root = stable_snapshot.composition_root
        assert stable_root is not None
        stable_mcp = stable_root.context.require(MCP_SERVERS)
        stable_processes = stable_root.context.require(MANAGED_PROCESSES)
        assert stable_root.context.get(UI_SLOTS) is not None
        assert "fitbit" in stable_snapshot.generations
        stable_monitor = stable_processes._entries[("fitbit", "monitor")]
        assert (
            stable_monitor._host.endpoint(stable_monitor._id, "monitor").port
            == 18765
        )
        stable_mcp_definition = stable_mcp._entries["fitbit"].definition
        assert stable_mcp_definition.required_tools == (
            "fitbit_health_snapshot",
            "fitbit_sleep_report",
        )
        formal_data = tmp_path / "workspace/plugin-data/fitbit-builtin"
        formal_files = _tree_files(formal_data)

        # 2. 新版本先在隔离 Root 中验证，再重建 formal Root。
        plugin_path = plugin_root / "plugin.py"
        plugin_path.write_text(
            plugin_path.read_text(encoding="utf-8").replace("3.2.4", "3.2.5"),
            encoding="utf-8",
        )
        _prepare_python_environment(plugin_root, workspace)
        candidate = await manager.prepare_candidate("fitbit")
        assert candidate is not None and candidate.runtime_snapshot is not None
        assert candidate.validation_workspace is not None
        validation_root = candidate.validation_workspace.parent
        candidate_snapshot = candidate.runtime_snapshot
        assert candidate_snapshot.composition_root is not None
        # EventMail is optional in this composition; the candidate keeps the
        # dormant source pending until that provider is installed.
        assert candidate_snapshot.composition_root.receipt().optional_pending == (
            "fitbit-eventmail-source",
        )
        assert candidate.validation_workspace != tmp_path / "workspace"
        after = _tree_files(formal_data)
        # sqlite 连接开关会改变正式文件内容；候选只允许读写自己的数据目录。
        volatile = lambda name: name.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm"))
        changed = {
            name for name, value in after.items()
            if not volatile(name) and formal_files.get(name) != value
        } | {
            name for name in set(formal_files) - set(after)
            if not volatile(name)
        } | {
            name for name in set(after) - set(formal_files)
            if not volatile(name)
        }
        assert not changed
        original_invariants = manager._post_publish_invariants  # pyright: ignore[reportPrivateUsage]
        candidate_checked = False

        async def inspect_candidate_runtime(
            generation: PluginGeneration,
            snapshot: RuntimeSnapshot,
        ) -> None:
            nonlocal candidate_checked
            candidate_root = snapshot.composition_root
            assert candidate_root is not None
            candidate_mcp = candidate_root.context.require(MCP_SERVERS)
            candidate_processes = candidate_root.context.require(MANAGED_PROCESSES)
            candidate_monitor = candidate_processes._entries[("fitbit", "monitor")]
            candidate_port = candidate_monitor._host.endpoint(
                candidate_monitor._id, "monitor"
            ).port
            assert candidate_port != 18765
            candidate_definition = candidate_mcp._entries["fitbit"].definition
            assert set(candidate_definition.required_tools) == {
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
        # 验证宿主保留清理责任到显式收尾；目录证据在收尾后才回收。
        retained = [
            identity for identity, host in manager._validation_hosts.items()  # pyright: ignore[reportPrivateUsage]
            if host.root is candidate_snapshot.composition_root
        ]
        for identity in retained:
            await manager.retry_validation_cleanup(identity)
        assert not [
            host for host in manager._validation_hosts.values()  # pyright: ignore[reportPrivateUsage]
            if host.root is candidate_snapshot.composition_root
        ]
        # workspace 目录作为验证证据保留，不随收尾删除。
        assert validation_root.exists()
    finally:
        await manager.terminate_all()
        log.close()

    # 3. Manager 终止后进程、MCP 与 Root effects 全部归零。
    assert stable_snapshot is not None and stable_snapshot.composition_root is not None
    assert stable_snapshot.composition_root.receipt().effects == ()
    assert stable_snapshot.composition_root.topology_view().listeners == ()
