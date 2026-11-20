from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from plugins.tools.contract import TOOLS
from importlib import import_module
from .tools import register_tools

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import CREDENTIALS, RUNTIME_STARTED, RUNTIME_STOPPING, Context, CredentialRef
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from plugins.managed_processes.contract import MANAGED_PROCESSES, ManagedProcessDefinition, ManagedProcessHandle
from plugins.mcp.contract import MCP_SERVERS, EndpointEnv, McpServerDefinition
from plugins.timer.contract import TIMERS
from plugins.onboarding.contract import ONBOARDING, Ability, PreviewLine
from plugins.plugin_settings.contract import (
    SETTINGS_FORMS, Authorize, SettingsField, SettingsForm, Setup, declare_settings, declare_setup,
)
from plugins.ui.contract import UI
from plugins.watch.contract import WATCH_EVENTS
from .src.content_adapter import (
    BorrowedWatchEvents,
    FitbitWakeRuntime,
    FitbitMonitorClient,
)
from .src.adapter_store import FitbitAdapterStore


class FitbitContentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: int = Field(default=300, ge=1)


class FitbitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: FitbitContentConfig = Field(default_factory=FitbitContentConfig)
    # Google Cloud 的 OAuth 客户端；没填时 monitor 回到自己的 monitor.config*.toml。
    google_client_id: str | None = None
    google_client_secret: CredentialRef | None = None


SETTINGS_FORM = SettingsForm(
    title="Fitbit 健康数据", description="经 Google Health API 读取 Fitbit 的睡眠、心率和运动，交给值班，也供聊天时查询。",
    fields=(
        SettingsField("google_client_id", "Google OAuth 客户端 ID",
                      hint="Google Cloud 控制台新建「Web 应用」客户端，启用 Google Health API"),
        SettingsField("google_client_secret", "客户端密钥", kind="secret",
                      hint="客户端的重定向 URI 填 http://localhost:18765/oauth/callback"),
    ),
    setup=Setup("health", "健康数据", Ability(
        pitch="留意你的睡眠和身体状态",
        benefit="接上 Fitbit 后，值班会留意睡眠、心率这类变化，聊天时也能查最近的睡眠和运动。",
        preview=(PreviewLine("你", "我昨晚睡得怎么样？"),
                 PreviewLine("Akashic", "一点半睡、七点四十醒，深睡 1 小时 10 分，比这周平均少一点。"))),
    ),
)


api_version = 3
name = "fitbit"
version = "3.7.0"
desc = "Fitbit health events and sleep state source for watch"
Config = FitbitConfig
inject = (
    TOOLS,
    MANAGED_PROCESSES,
    MCP_SERVERS,
    TIMERS,
    UI,
    CREDENTIALS,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# monitor 私有回环接口上的授权：start 取它生成的 Google 授权跳转，finish 把回调参数交回它换令牌。
class MonitorAuthorization:
    def __init__(self, ctx: Context, monitor: ManagedProcessHandle) -> None:
        self._ctx, self._monitor = ctx, monitor

    async def status(self) -> Mapping[str, object]:
        try:
            async with asyncio.timeout(5), self._monitor.borrow(self._ctx) as port:
                body = await asyncio.to_thread(_read_json, f"http://127.0.0.1:{port}/api/auth/status")
        except (OSError, ValueError, RuntimeError, TimeoutError):
            return {"ready": False, "reason": "健康数据服务还没启动好，稍后刷新"}
        if not body.get("client_configured"):
            return {"ready": False, "reason": "还需要填写 Google OAuth 客户端 ID 和密钥"}
        if not body.get("authorized"):
            return {"ready": False, "reason": "还没有授权 Google Health：保存客户端后点下方「获取授权链接」"}
        return {"ready": True, "reason": ""}

    async def start(self) -> str:
        async with self._monitor.borrow(self._ctx) as port:
            status = await asyncio.to_thread(_read_json, f"http://127.0.0.1:{port}/api/auth/status")
            if not status.get("client_configured"):
                raise ValueError("还没有填写 Google OAuth 客户端 ID 和密钥")
            location = await asyncio.to_thread(_redirect_location, f"http://127.0.0.1:{port}/auth/start")
        parsed = urlsplit(location)
        if parsed.scheme != "https" or parsed.netloc != "accounts.google.com":
            raise ValueError("健康数据服务没有给出 Google 授权地址，先确认客户端 ID 和密钥已保存")
        return location

    async def finish(self, callback: str) -> None:
        query = urlsplit(callback).query
        if "code=" not in query:
            raise ValueError("网址里没有 code 参数，请复制授权完成后地址栏里的完整网址")
        async with self._monitor.borrow(self._ctx) as port:
            status = await asyncio.to_thread(_get_status, f"http://127.0.0.1:{port}/oauth/callback?{query}")
        if status != 200:
            raise ValueError("授权没有完成：链接可能已过期或服务重启过，请重新获取授权链接")


def _read_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=8) as response:
        body = json.loads(response.read())
    if not isinstance(body, dict):
        raise ValueError("monitor 返回的不是对象")
    return body


def _redirect_location(url: str) -> str:
    try:
        build_opener(_NoRedirect).open(url, timeout=8)
    except HTTPError as redirect:
        location = redirect.headers.get("location")
        if redirect.code in (302, 307) and location:
            return location
    raise ValueError("健康数据服务没有给出授权跳转")


def _get_status(url: str) -> int:
    try:
        with urlopen(Request(url), timeout=30) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except URLError as error:
        raise ValueError(f"健康数据服务不可用：{error.reason}") from error


async def _client_env(ctx: Context, config: FitbitConfig) -> dict[str, str]:
    secret = config.google_client_secret
    if config.google_client_id is None or secret is None:
        return {}
    async with ctx.require(CREDENTIALS).open(ctx, {"google_client_secret": secret}) as client:
        return {"GOOGLE_HEALTH_CLIENT_ID": config.google_client_id,
                "GOOGLE_HEALTH_CLIENT_SECRET": client.credential(secret)}


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
            env=await _client_env(ctx, config),
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
            required_tools=(
                "fitbit_health_snapshot",
                "fitbit_sleep_report",
                "fitbit_workouts",
                "fitbit_activity_report",
            ),
            candidate_read_only_tools=(
                "fitbit_health_snapshot",
                "fitbit_sleep_report",
                "fitbit_workouts",
                "fitbit_activity_report",
            ),
            endpoint_env=(EndpointEnv("FITBIT_MONITOR_PORT", monitor),),
            candidate_env={"FITBIT_BACKEND": "recording"},
        ),
    )

    await register_tools(ctx, description=desc)

    # 设置与引导是可选依赖；就绪看 monitor 是否已有客户端和令牌，授权也经它完成。
    authorization = MonitorAuthorization(ctx, monitor)
    authorize = Authorize("Google Health 授权", "保存客户端后授权一次；之后令牌自动续期",
                          authorization.start, authorization.finish)

    async def settings(child: Context) -> None:
        _ = await declare_settings(child, FitbitConfig, SETTINGS_FORM, status=authorization.status, authorize=authorize)

    async def setup(child: Context) -> None:
        await declare_setup(child, FitbitConfig, SETTINGS_FORM, status=authorization.status)

    _ = await ctx.inject((SETTINGS_FORMS, PLUGIN_CONFIG), settings, name="settings")
    _ = await ctx.inject((SETTINGS_FORMS, PLUGIN_CONFIG, ONBOARDING), setup, name="onboarding")

    async def monitor_ready() -> None:
        async with monitor.borrow(ctx):
            return

    # 2. 值班存在时，独立子 Fiber 才启动健康来源。
    async def apply_watch(source_ctx: Context) -> None:
        store = FitbitAdapterStore(source_ctx.data_root / "adapter.sqlite3")
        store.initialize(datetime.now(UTC))
        runtime = FitbitWakeRuntime(
            store,
            source_ctx.require(TIMERS),
            BorrowedWatchEvents(source_ctx),
            FitbitMonitorClient(),
            monitor_ready=monitor_ready,
            poll_interval=timedelta(seconds=config.content.poll_interval_seconds),
        )

        def setup() -> object:
            return runtime.close

        _ = await source_ctx.effect(setup, label="fitbit-watch-runtime")
        poll_health = await source_ctx.health("fitbit-watch-poll")

        async def start(_event: object) -> None:
            await runtime.start(source_ctx, poll_health)

        async def stop(_event: object) -> None:
            await runtime.close()

        _ = await source_ctx.on(RUNTIME_STARTED, start)
        _ = await source_ctx.on(RUNTIME_STOPPING, stop)

    _ = await ctx.inject(
        (TIMERS, WATCH_EVENTS),
        apply_watch,
        name="fitbit-watch-source",
    )
