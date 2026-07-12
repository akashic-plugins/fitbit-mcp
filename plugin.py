from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from agent.plugins import ManagedServiceSpec, McpServerSpec, Plugin, ProactiveSourceSpec


class FitbitProactiveConfig(BaseModel):
    enabled: bool = True


class FitbitConfig(BaseModel):
    proactive: FitbitProactiveConfig = Field(default_factory=FitbitProactiveConfig)


class FitbitPlugin(Plugin):
    name = "fitbit"
    version = "1.1.1"
    desc = "Fitbit health monitor and sleep model"
    ConfigModel = FitbitConfig

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [McpServerSpec(name="fitbit", command=("python", "run_mcp.py"))]

    @classmethod
    def managed_services(cls) -> list[ManagedServiceSpec]:
        return [
            ManagedServiceSpec(
                id="monitor",
                command=("python", "monitor/server.py"),
                cwd="monitor",
                readiness_url="http://127.0.0.1:18765/api/data",
                startup_timeout_seconds=15,
            )
        ]

    def proactive_sources(self) -> list[ProactiveSourceSpec]:
        config = cast(FitbitConfig, self.context.config)
        if not config.proactive.enabled:
            return []
        return [
            ProactiveSourceSpec(
                id="health_alerts",
                channels=("alert",),
                server="fitbit",
                fetch_tool="get_proactive_events",
                ack_tool="acknowledge_events",
            ),
            ProactiveSourceSpec(
                id="sleep_context",
                channels=("context",),
                server="fitbit",
                fetch_tool="get_sleep_context",
            ),
        ]

    async def initialize(self) -> None:
        data_dir = self.context.data_dir
        if data_dir is None:
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_state(data_dir)

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
