"""本插件迁移使用的 workspace 边界与进程锁。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class WorkspaceInstanceLock:
    """保证离线迁移与 runtime 不会同时写同一 workspace。"""

    def __init__(self, workspace: Path) -> None:
        self.path = workspace / ".instance.lock"
        self._stream: IO[str] | None = None

    def acquire(self) -> None:
        """非阻塞取得 workspace 锁，并保留 owner 诊断信息。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.seek(0)
            owner = stream.read().strip() or "unknown"
            stream.close()
            raise RuntimeError(
                f"workspace 已由其他 runtime 占用: {self.path} owner={owner}"
            ) from exc
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        self._stream = stream

    def release(self) -> None:
        """释放当前进程持有的锁；锁文件本身保留作诊断。"""

        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def validate_workspace_plugin_data_path(path: Path, workspace: Path) -> None:
    """拒绝越出 workspace 或穿过已有符号链接的数据路径。"""

    root = workspace.expanduser().resolve(strict=False)
    candidate = path.expanduser()
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"插件数据目录越界: {path}") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"插件数据目录不能穿过符号链接: {current}")


def ensure_workspace_plugin_data_dir(path: Path, workspace: Path) -> None:
    """在 workspace 内创建数据目录，并在创建前后复核路径边界。"""

    validate_workspace_plugin_data_path(path, workspace)
    path.mkdir(parents=True, exist_ok=True)
    validate_workspace_plugin_data_path(path, workspace)
