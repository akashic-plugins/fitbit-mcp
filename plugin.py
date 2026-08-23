from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from agent.lifecycle.composition import CONTEXT_PREPARED_EVENT
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
    ServiceKey,
)
from src.content_adapter import (
    BoundContentSource,
    FitbitContentRuntime,
    FitbitMonitorClient,
)
from src.mobile_reader import mobile_ui_query
from src.sleep_context import FitbitAdapterStore, SleepContextAppender


class ContentSourceServices(Protocol):
    def bind(self, source_id: str) -> BoundContentSource: ...


CONTENT_SOURCE = ServiceKey[ContentSourceServices]("content.source.v1")


class FitbitContentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: int = Field(default=300, ge=1)
    sleep_ttl_seconds: int = Field(default=600, ge=1)


class FitbitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: FitbitContentConfig = Field(default_factory=FitbitContentConfig)


api_version = 3
name = "fitbit"
version = "3.1.0"
desc = "Fitbit health monitor, Content source, and sleep context"
Config = FitbitConfig
inject = (MANAGED_PROCESSES, MCP_SERVERS, TIMERS, CONTENT_SOURCE, UI_SLOTS)
dashboard_module = "dashboard.py"


async def apply(ctx: Context, config: FitbitConfig) -> None:
    """装配 monitor、工具、Content 采集、睡眠上下文与移动界面。"""

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

    # 2. 绑定唯一正式来源；candidate Root 不会收到 STARTED
    store = FitbitAdapterStore(ctx.data_root / "adapter.sqlite3")
    store.initialize(datetime.now(UTC))
    runtime = FitbitContentRuntime(
        store,
        ctx.require(TIMERS),
        ctx.require(CONTENT_SOURCE).bind("fitbit-health-alerts"),
        FitbitMonitorClient(),
        poll_interval=timedelta(seconds=config.content.poll_interval_seconds),
        sleep_ttl=timedelta(seconds=config.content.sleep_ttl_seconds),
    )

    def setup() -> object:
        return runtime.close

    _ = await ctx.effect(setup, label="fitbit-content-runtime")

    async def start(_event: object) -> None:
        await runtime.start()

    async def stop(_event: object) -> None:
        await runtime.close()

    _ = await ctx.on(RUNTIME_STARTED, start)
    _ = await ctx.on(RUNTIME_STOPPING, stop)
    _ = await ctx.on(CONTEXT_PREPARED_EVENT, SleepContextAppender(store).prepare)

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
