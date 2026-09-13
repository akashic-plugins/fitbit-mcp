#!/usr/bin/env python3
"""在插件自己的 Dashboard 入口中预览 Fitbit 面板。"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from agent.plugin_composition import DashboardContext

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

import dashboard


def run_preview(host: str, port: int) -> None:
    """Run the plugin-owned Dashboard routes against the local monitor."""

    # 1. The preview owns only a temporary data root; it never opens the formal workspace.
    plugin_root = _PLUGIN_ROOT
    if not (plugin_root / "web_module.js").is_file():
        raise FileNotFoundError(f"Fitbit Workbench 面板不存在: {plugin_root}")
    with tempfile.TemporaryDirectory(prefix="fitbit-dashboard-preview-") as temp:
        data_root = Path(temp) / "plugin-data"
        data_root.mkdir()
        context = DashboardContext(
            plugin_id="fitbit@preview",
            plugin_dir=plugin_root,
            data_root=data_root,
            validation=True,
        )
        app = FastAPI(title="Fitbit Dashboard Preview")
        dashboard.register(app, context)
        print(f"Fitbit Dashboard 预览: http://{host}:{port}/api/dashboard/fitbit/overview")
        uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2237)
    args = parser.parse_args()
    run_preview(args.host, args.port)


if __name__ == "__main__":
    main()
