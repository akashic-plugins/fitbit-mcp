from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from agent.plugin_composition import MANAGED_PROCESSES, MCP_SERVERS, UI_SLOTS
from agent.plugins.selection import PluginSelection
from session.log import MessageLog
from agent.plugins.manager import PluginManager
from agent.plugins.python_environment import ENVIRONMENT_FILE, PythonEnvironments
from agent.plugins.static_manifest import load_static_plugin_manifest
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
async def test_manager_updates_fitbit_on_one_live_root_and_keeps_data(
    tmp_path: Path,
) -> None:
    """Load the real artifact, replace its owner, and keep the data directory."""

    plugin_root = _stage_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    PluginSelection(workspace).initialize()
    _prepare_python_environment(plugin_root, workspace)
    log = MessageLog(tmp_path / "sessions.db")
    manager = PluginManager(
        message_log=log,
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        workspace=workspace,
        installed_cache_root=tmp_path / "home" / "cache",
    )
    root = None
    try:
        await manager.load_all()
        root = manager.live_root
        assert root is not None
        old = manager.generation("fitbit")
        assert old is not None and old.fiber is not None
        assert root.context.get(UI_SLOTS) is not None
        assert root.context.require(MCP_SERVERS)._entries["fitbit"].definition.required_tools == (
            "fitbit_health_snapshot", "fitbit_sleep_report",
        )
        processes = root.context.require(MANAGED_PROCESSES)
        monitor = processes._entries[("fitbit", "monitor")]
        assert monitor._host.endpoint(monitor._id, "monitor").port == 18765

        data_dir = workspace / "plugin-data" / "fitbit-builtin"
        marker = data_dir / "retained-test-data.txt"
        marker.write_text("keep this data", encoding="utf-8")
        plugin_path = plugin_root / "plugin.py"
        plugin_path.write_text(
            plugin_path.read_text(encoding="utf-8").replace("3.2.4", "3.2.5"),
            encoding="utf-8",
        )
        _prepare_python_environment(plugin_root, workspace)
        result = next(
            item for item in await manager.reconcile_changed()
            if item["plugin_id"] == "fitbit"
        )
        assert result["publication_state"] == "active"
        assert manager.live_root is root
        new = manager.generation("fitbit")
        assert new is not None and new is not old and new.fiber is not None
        assert new.instance is not None and new.instance.version == "3.2.5"
        assert marker.read_text(encoding="utf-8") == "keep this data"
        assert root.context.require(MCP_SERVERS)._entries["fitbit"].definition.required_tools == (
            "fitbit_health_snapshot", "fitbit_sleep_report",
        )
        monitor = root.context.require(MANAGED_PROCESSES)._entries[("fitbit", "monitor")]
        assert monitor._host.endpoint(monitor._id, "monitor").port == 18765
    finally:
        await manager.terminate_all()
        log.close()
    assert root is not None
    assert root.receipt().effects == ()
    assert root.topology_view().listeners == ()
