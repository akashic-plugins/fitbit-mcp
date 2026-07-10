from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from agent.plugins import McpServerSpec, Plugin, ProactiveSourceSpec


class FitbitProactiveConfig(BaseModel):
    enabled: bool = True
    poll_interval_seconds: int = Field(default=300, ge=1)


class FitbitConfig(BaseModel):
    proactive: FitbitProactiveConfig = Field(default_factory=FitbitProactiveConfig)


class FitbitPlugin(Plugin):
    name = "fitbit"
    version = "1.0.1"
    desc = "Fitbit health monitor and sleep model"
    ConfigModel = FitbitConfig

    def __init__(self) -> None:
        self._monitor: subprocess.Popen[bytes] | None = None

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [McpServerSpec(name="fitbit", command=("python", "run_mcp.py"))]

    def proactive_sources(self) -> list[ProactiveSourceSpec]:
        config = cast(FitbitConfig, self.context.config)
        if not config.proactive.enabled:
            return []
        interval = config.proactive.poll_interval_seconds
        return [
            ProactiveSourceSpec(
                id="health_alerts",
                channels=("alert",),
                server="fitbit",
                fetch_tool="get_proactive_events",
                ack_tool="acknowledge_events",
                poll_interval_seconds=interval,
            ),
            ProactiveSourceSpec(
                id="sleep_context",
                channels=("context",),
                server="fitbit",
                fetch_tool="get_sleep_context",
                poll_interval_seconds=interval,
            ),
        ]

    async def initialize(self) -> None:
        data_dir = self.context.data_dir
        if data_dir is None:
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_state(data_dir)
        if await asyncio.to_thread(_monitor_available):
            return
        python = self.context.plugin_dir / ".venv" / "bin" / "python"
        env = {**os.environ, "AKA_PLUGIN_DATA_DIR": str(data_dir), "PYTHONUNBUFFERED": "1"}
        self._monitor = subprocess.Popen(
            [str(python), str(self.context.plugin_dir / "monitor" / "server.py")],
            cwd=self.context.plugin_dir / "monitor",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            if await asyncio.to_thread(_monitor_available):
                return
            if self._monitor.poll() is not None:
                raise RuntimeError("Fitbit monitor 启动失败")
            await asyncio.sleep(0.5)
        raise RuntimeError("Fitbit monitor 启动超时")

    async def terminate(self) -> None:
        monitor = self._monitor
        if monitor is None or monitor.poll() is not None:
            return
        monitor.terminate()
        try:
            await asyncio.to_thread(monitor.wait, 5)
        except subprocess.TimeoutExpired:
            monitor.kill()
            await asyncio.to_thread(monitor.wait)

    def _migrate_legacy_state(self, data_dir: Path) -> None:
        workspace = self.context.workspace
        if workspace is None:
            return
        legacy = workspace / "mcp" / "fitbit-mcp" / "monitor"
        if not legacy.is_dir():
            return
        for name in (
            "monitor.config.toml",
            "monitor.config.local.toml",
            "tokens.json",
            "sleep_log.jsonl",
            "sleep_labels.json",
            "sleep_model.pkl",
            "stat_events.json",
            "stat_events_v2.json",
        ):
            source = legacy / name
            target = data_dir / name
            if source.exists() and not target.exists():
                shutil.copy2(source, target)


def _monitor_available() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18765/api/data", timeout=1):
            return True
    except OSError:
        return False
