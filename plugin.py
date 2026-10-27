from __future__ import annotations

from datetime import UTC, datetime, timedelta
from plugins.tools.contract import TOOLS
from importlib import import_module
from .tools import register_tools

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import RUNTIME_STARTED, RUNTIME_STOPPING, Context
from plugins.managed_processes.contract import MANAGED_PROCESSES, ManagedProcessDefinition
from plugins.mcp.contract import MCP_SERVERS, EndpointEnv, McpServerDefinition
from plugins.timer.contract import TIMERS
from plugins.ui.contract import UI
from .src.content_adapter import (
    FitbitWakeRuntime,
    FitbitMonitorClient,
)
from plugins.eventmail.contract import EVENTMAIL_ALERT_SOURCE, EVENTMAIL_CONTEXT_SOURCE
from .src.sleep_context import FitbitAdapterStore


class FitbitContentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: int = Field(default=300, ge=1)
    sleep_ttl_seconds: int = Field(default=600, ge=1)


class FitbitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: FitbitContentConfig = Field(default_factory=FitbitContentConfig)


api_version = 3
name = "fitbit"
version = "3.3.0"
desc = "Fitbit health Alert and sleep Context source"
Config = FitbitConfig
inject = (
    TOOLS,
    MANAGED_PROCESSES,
    MCP_SERVERS,
    TIMERS,
    UI,
)


async def apply(ctx: Context) -> None:
    """装配 monitor、工具、Wake 来源和 Dashboard。"""

    config = FitbitConfig.model_validate(ctx.config)
    await ctx.require(UI).register(
        ctx, dashboard=lambda: import_module(".dashboard", __package__),
    )

    # 1. 登记现有 monitor 与用户显式调用的普通 MCP 工具
    monitor = await ctx.require(MANAGED_PROCESSES).register(
        ctx,
        ManagedProcessDefinition(
            name="monitor",
            command=("python", "monitor/server.py"),
            cwd=".",
            port_env="FITBIT_MONITOR_PORT",
            formal_port=18765,
            readiness_path="/api/data",
            # 首次加载模型与科学计算库时，为并行启动留出余量。
            startup_timeout_seconds=60.0,
            # 模型加载要数秒，不挡宿主就绪；工具经 MCP 借用、轮询先等就绪。
            background_ready=True,
        ),
    )
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="fitbit",
            command=("python", "run_mcp.py"),
            required_tools=("fitbit_health_snapshot", "fitbit_sleep_report"),
            candidate_read_only_tools=(
                "fitbit_health_snapshot",
                "fitbit_sleep_report",
            ),
            endpoint_env=(EndpointEnv("FITBIT_MONITOR_PORT", monitor),),
            candidate_env={"FITBIT_BACKEND": "recording"},
        ),
    )

    await register_tools(ctx, description=desc)

    async def monitor_ready() -> None:
        async with monitor.borrow(ctx):
            return

    # 2. EventMail 存在时，独立子 Fiber 才启动健康来源。
    async def apply_eventmail(source_ctx: Context) -> None:
        store = FitbitAdapterStore(source_ctx.data_root / "adapter.sqlite3")
        store.initialize(datetime.now(UTC))
        alert_source = source_ctx.require(EVENTMAIL_ALERT_SOURCE).bind("fitbit-health-alerts")
        try:
            _ = await source_ctx.effect(lambda: alert_source.close, label="fitbit-alert-source-binding")
        except BaseException:
            alert_source.close()
            raise
        context_source = source_ctx.require(EVENTMAIL_CONTEXT_SOURCE).bind("fitbit-sleep")
        try:
            _ = await source_ctx.effect(lambda: context_source.close, label="fitbit-context-source-binding")
        except BaseException:
            context_source.close()
            raise
        runtime = FitbitWakeRuntime(
            store,
            source_ctx.require(TIMERS),
            alert_source,
            context_source,
            FitbitMonitorClient(),
            monitor_ready=monitor_ready,
            poll_interval=timedelta(seconds=config.content.poll_interval_seconds),
            sleep_ttl=timedelta(seconds=config.content.sleep_ttl_seconds),
        )

        def setup() -> object:
            return runtime.close

        _ = await source_ctx.effect(setup, label="fitbit-eventmail-runtime")
        poll_health = await source_ctx.health("fitbit-eventmail-poll")

        async def start(_event: object) -> None:
            await runtime.start(source_ctx, poll_health)

        async def stop(_event: object) -> None:
            await runtime.close()

        _ = await source_ctx.on(RUNTIME_STARTED, start)
        _ = await source_ctx.on(RUNTIME_STOPPING, stop)

    _ = await ctx.inject(
        (TIMERS, EVENTMAIL_ALERT_SOURCE, EVENTMAIL_CONTEXT_SOURCE),
        apply_eventmail,
        name="fitbit-eventmail-source",
    )
