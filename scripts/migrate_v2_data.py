#!/usr/bin/env python3
"""把 Fitbit v2 workspace 数据非破坏迁移到 v3 plugin-data。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from agent.plugins.manifest import (
    ensure_workspace_plugin_data_dir,
    validate_workspace_plugin_data_path,
)
from bootstrap.workspace_lock import WorkspaceInstanceLock


_DATA_FILES = (
    "monitor.config.toml",
    "monitor.config.local.toml",
    "tokens.json",
    "sleep_log.jsonl",
    "sleep_labels.json",
    "sleep_model.pkl",
    "stat_events.json",
    "stat_events_v2.json",
)
_RECEIPT = ".fitbit-v2-migration.json"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _remove_crash_staging(workspace: Path) -> None:
    """清理上次 Core 进程崩溃留下的未发布 staging。"""

    parent = workspace / "plugin-data"
    if parent.is_symlink():
        raise ValueError(f"Fitbit plugin-data 根不得是符号链接: {parent}")
    if not parent.is_dir():
        return
    for path in parent.glob(".fitbit-v2-migrate-*"):
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"Fitbit migration staging 无效: {path}")
        shutil.rmtree(path)


def _read_receipt(path: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Fitbit migration receipt 不是普通文件: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"Fitbit migration receipt 无效: {path}")
    return raw


def _verify_published(
    target: Path,
    receipt: dict[str, object],
) -> dict[str, object]:
    """验证既有 receipt 与全部已发布目标仍完全一致。"""

    files = receipt.get("files")
    if (
        receipt.get("source") != "mcp/fitbit-mcp/monitor"
        or receipt.get("target") != f"plugin-data/{target.name}"
        or receipt.get("source_retained") is not True
        or not isinstance(files, list)
        or not files
    ):
        raise ValueError("Fitbit migration receipt 身份无效")
    seen: set[str] = set()
    for raw in files:
        if not isinstance(raw, dict):
            raise ValueError("Fitbit migration receipt file 条目无效")
        name = raw.get("name")
        expected = raw.get("sha256")
        size = raw.get("size")
        if (
            not isinstance(name, str)
            or name not in _DATA_FILES
            or name in seen
            or not isinstance(expected, str)
            or len(expected) != 64
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError("Fitbit migration receipt file 条目无效")
        seen.add(name)
        path = target / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != size
            or _digest(path) != expected
        ):
            raise ValueError(f"Fitbit migration 目标内容漂移: {path}")
    return receipt


def _stage(source: Path, staging: Path) -> list[dict[str, object]]:
    """复制 v2 权威文件到隔离 staging，并冻结内容证据。"""

    files: list[dict[str, object]] = []
    for name in _DATA_FILES:
        source_file = source / name
        if not source_file.exists() and not source_file.is_symlink():
            continue
        if source_file.is_symlink() or not source_file.is_file():
            raise ValueError(f"Fitbit v2 数据不是普通文件: {source_file}")
        staged = staging / name
        shutil.copy2(source_file, staged)
        files.append(
            {
                "name": name,
                "sha256": _digest(staged),
                "size": staged.stat().st_size,
            }
        )
    if not files:
        raise FileNotFoundError("Fitbit v2 数据目录没有可迁移文件")
    return files


def _publish(
    staging: Path,
    target: Path,
    files: list[dict[str, object]],
    receipt: dict[str, object],
) -> None:
    """发布本次新增文件，进程内失败时完整回滚。"""

    published: list[Path] = []
    receipt_path = target / _RECEIPT
    try:
        for item in files:
            name = str(item["name"])
            destination = target / name
            if destination.is_symlink():
                raise ValueError(f"Fitbit v3 目标不得是符号链接: {destination}")
            if destination.exists():
                if not destination.is_file() or _digest(destination) != item["sha256"]:
                    raise FileExistsError(f"Fitbit v3 目标已存在且内容不同: {destination}")
                continue
            os.replace(staging / name, destination)
            published.append(destination)
        staged_receipt = staging / _RECEIPT
        staged_receipt.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staged_receipt, receipt_path)
    except BaseException:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        raise


def _migrate_locked(workspace: Path, marketplace: str) -> dict[str, object]:
    """在 workspace 独占区间完成一次可重入迁移。"""

    if not marketplace or not marketplace.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"Fitbit marketplace 无效: {marketplace}")
    _remove_crash_staging(workspace)
    source = workspace / "mcp" / "fitbit-mcp" / "monitor"
    if source.is_symlink() or not source.is_dir() or not source.is_relative_to(workspace):
        raise ValueError(f"Fitbit v2 数据目录不存在或不安全: {source}")
    target = workspace / "plugin-data" / f"fitbit-{marketplace}"
    validate_workspace_plugin_data_path(target, workspace)
    existing = _read_receipt(target / _RECEIPT)
    if existing is not None:
        return _verify_published(target, existing)

    parent = workspace / "plugin-data"
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".fitbit-v2-migrate-{uuid.uuid4().hex}"
    staging.mkdir()
    target_created = not target.exists()
    try:
        files = _stage(source, staging)
        ensure_workspace_plugin_data_dir(target, workspace)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "source": "mcp/fitbit-mcp/monitor",
            "target": f"plugin-data/fitbit-{marketplace}",
            "source_retained": True,
            "files": files,
        }
        _publish(staging, target, files, receipt)
        return receipt
    except BaseException:
        if target_created and target.is_dir() and not any(target.iterdir()):
            target.rmdir()
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def migrate_v2_data(workspace: Path, marketplace: str) -> dict[str, object]:
    """持有 workspace 独占锁迁移 Fitbit 数据并返回 receipt。"""

    resolved = workspace.expanduser().resolve()
    lock = WorkspaceInstanceLock(resolved)
    lock.acquire()
    try:
        return _migrate_locked(resolved, marketplace)
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--marketplace", default="github")
    args = parser.parse_args()
    print(
        json.dumps(
            migrate_v2_data(args.workspace, args.marketplace),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
