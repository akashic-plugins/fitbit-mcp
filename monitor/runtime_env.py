from __future__ import annotations

import os


def resolve_server_port(configured_port: int) -> int:
    """Return the candidate-isolated monitor port when the runtime provides one."""

    raw_port = os.environ.get("FITBIT_MONITOR_PORT")
    if raw_port is None:
        return configured_port
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("FITBIT_MONITOR_PORT 必须在 1..65535")
    return port
