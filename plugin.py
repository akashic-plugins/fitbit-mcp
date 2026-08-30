from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    TIMERS,
    UI_SLOTS,
    Context,
    EndpointEnv,
    ManagedProcessDefinition,
    McpServerDefinition,
    MobileUiDefinition,
    MobileUiNavigation,
)
from .src.content_adapter import (
    FitbitWakeRuntime,
    FitbitMonitorClient,
)
from .src.eventmail import EVENTMAIL_ALERT_SOURCE, EVENTMAIL_CONTEXT_SOURCE
from .src.mobile_reader import mobile_ui_query
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
version = "3.2.2"
desc = "Fitbit health Alert and sleep Context source"
Config = FitbitConfig
inject = (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    TIMERS,
    UI_SLOTS,
)
dashboard_module = "dashboard.py"
web_module = "web_module.js"
web_requires = ("workbench.panels.v2",)
web_provides = ()
web_contract_digests = {
    "workbench.panels.v2": "fb6417c9bf532c1fdb344767d06065d5d3293da85deb64eff1e8088889a33bcb",
}


async def apply(ctx: Context, config: FitbitConfig) -> None:
    """装配 monitor、工具、Wake 来源和移动界面。"""

    # 1. 登记现有 monitor 与用户显式调用的普通 MCP 工具
    await ctx.require(MANAGED_PROCESSES).register(
        ctx,
        ManagedProcessDefinition(
            name="monitor",
            command=("python", "monitor/server.py"),
            cwd=".",
            port_env="FITBIT_MONITOR_PORT",
            formal_port=18765,
            readiness_path="/api/data",
            startup_timeout_seconds=15.0,
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
            endpoint_env=(EndpointEnv("FITBIT_MONITOR_PORT", "monitor"),),
            candidate_env={"FITBIT_BACKEND": "recording"},
        ),
    )

    # 2. EventMail 存在时，独立子 Fiber 才启动健康来源。
    async def apply_eventmail(source_ctx: Context) -> None:
        store = FitbitAdapterStore(source_ctx.data_root / "adapter.sqlite3")
        store.initialize(datetime.now(UTC))
        runtime = FitbitWakeRuntime(
            store,
            source_ctx.require(TIMERS),
            source_ctx.require(EVENTMAIL_ALERT_SOURCE).bind("fitbit-health-alerts"),
            source_ctx.require(EVENTMAIL_CONTEXT_SOURCE).bind("fitbit-sleep"),
            FitbitMonitorClient(),
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

    # 3. 在同一个 exact Root 上保留现有移动投影
    await ctx.require(UI_SLOTS).register_mobile(
        ctx,
        MobileUiDefinition(
            module="mobile_panel.js",
            stylesheet="mobile_panel.css",
            navigation=MobileUiNavigation(
                label="健康状态",
                description="查看当前心率、血氧、步数和最近睡眠节律",
            ),
        ),
        query=mobile_ui_query,
    )
