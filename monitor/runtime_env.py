from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import TextIO


RUNTIME_LOG_MAX_BYTES = 1_048_576
RUNTIME_LOG_BACKUPS = 3


class RotatingTextLog:
    """Keep one diagnostic text log within fixed byte and generation limits."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = RUNTIME_LOG_MAX_BYTES,
        backups: int = RUNTIME_LOG_BACKUPS,
    ) -> None:
        if max_bytes <= 0 or backups < 0:
            raise ValueError("runtime log rotation limits 无效")
        self.path = path
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._open()
        self._size = path.stat().st_size if path.exists() else 0
        if self._size >= self._max_bytes:
            self._rotate()

    def write(self, text: str) -> int:
        input_length = len(text)
        data = text.encode("utf-8")
        with self._lock:
            if self._size and self._size + len(data) > self._max_bytes:
                self._rotate()
            if len(data) > self._max_bytes:
                data = data[-self._max_bytes :]
                while data and (data[0] & 0xC0) == 0x80:
                    data = data[1:]
                text = data.decode("utf-8")
            self._stream.write(text)
            self._size += len(data)
        return input_length

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            self._stream.close()

    def _rotate(self) -> None:
        self._stream.close()
        if self._backups > 0:
            oldest = self.path.with_name(f"{self.path.name}.{self._backups}")
            oldest.unlink(missing_ok=True)
            for index in range(self._backups - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    os.replace(
                        source,
                        self.path.with_name(f"{self.path.name}.{index + 1}"),
                    )
            if self.path.exists():
                os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink(missing_ok=True)
        self._stream = self._open()
        self._size = 0

    def _open(self) -> TextIO:
        return self.path.open("a", encoding="utf-8", buffering=1)


def resolve_server_port(configured_port: int) -> int:
    """Return the candidate-isolated monitor port when the runtime provides one."""

    raw_port = os.environ.get("FITBIT_MONITOR_PORT")
    if raw_port is None:
        return configured_port
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("FITBIT_MONITOR_PORT 必须在 1..65535")
    return port
