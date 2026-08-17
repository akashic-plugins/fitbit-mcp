from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import migrate_v2_data as migration


def _source(workspace: Path) -> Path:
    source = workspace / "mcp" / "fitbit-mcp" / "monitor"
    source.mkdir(parents=True)
    (source / "monitor.config.toml").write_text("[server]\nport=18765\n")
    (source / "tokens.json").write_text('{"access":"secret"}\n')
    return source


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_process_failure_rolls_back_new_targets_and_retains_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    before = {path.name: _digest(path) for path in source.iterdir()}
    original_replace = migration.os.replace
    calls = 0

    def fail_second(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        original_replace(source_path, target_path)

    monkeypatch.setattr(migration.os, "replace", fail_second)

    with pytest.raises(OSError, match="injected publish failure"):
        _ = migration.migrate_v2_data(workspace, "github")

    target = workspace / "plugin-data" / "fitbit-github"
    assert not target.exists()
    assert {path.name: _digest(path) for path in source.iterdir()} == before


def test_process_crash_restarts_from_partial_publication(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    before = {path.name: _digest(path) for path in source.iterdir()}
    repo = Path(__file__).resolve().parents[1]
    core = Path(os.environ["AKASHIC_AGENT_ROOT"])
    code = """
import os
from pathlib import Path
from scripts import migrate_v2_data as migration

real_replace = migration.os.replace
calls = 0
def crash_second(source, target):
    global calls
    calls += 1
    if calls == 2:
        os._exit(137)
    real_replace(source, target)
migration.os.replace = crash_second
migration.migrate_v2_data(Path(os.environ['FITBIT_TEST_WORKSPACE']), 'github')
"""
    environment = {
        **os.environ,
        "AKASHIC_AGENT_ROOT": str(core),
        "FITBIT_TEST_WORKSPACE": str(workspace),
        "PYTHONPATH": os.pathsep.join((str(repo), str(core))),
    }
    crashed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo,
        env=environment,
        check=False,
    )
    assert crashed.returncode == 137

    target = workspace / "plugin-data" / "fitbit-github"
    assert (target / "monitor.config.toml").is_file()
    assert not (target / ".fitbit-v2-migration.json").exists()
    receipt = migration.migrate_v2_data(workspace, "github")

    assert receipt["source_retained"] is True
    assert (target / ".fitbit-v2-migration.json").is_file()
    assert {path.name: _digest(path) for path in source.iterdir()} == before
    assert not list((workspace / "plugin-data").glob(".fitbit-v2-migrate-*"))


def test_invalid_receipt_identity_fails_without_touching_data(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    receipt = migration.migrate_v2_data(workspace, "github")
    target = workspace / "plugin-data" / "fitbit-github"
    before_source = {path.name: _digest(path) for path in source.iterdir()}
    before_target = {
        path.name: _digest(path)
        for path in target.iterdir()
        if path.name != ".fitbit-v2-migration.json"
    }
    receipt["target"] = "plugin-data/another-plugin"
    (target / ".fitbit-v2-migration.json").write_text(
        json.dumps(receipt),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="receipt 身份无效"):
        _ = migration.migrate_v2_data(workspace, "github")

    assert {path.name: _digest(path) for path in source.iterdir()} == before_source
    assert {
        path.name: _digest(path)
        for path in target.iterdir()
        if path.name != ".fitbit-v2-migration.json"
    } == before_target
