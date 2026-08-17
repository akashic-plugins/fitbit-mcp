from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import requests
from pydantic import BaseModel, Field

from agent.plugin_composition import (
    MANAGED_PROCESSES,
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    UI_SLOTS,
    Context,
    EndpointEnv,
    ManagedProcessDefinition,
    McpServerDefinition,
    MobileUiDefinition,
    MobileUiNavigation,
    MobileUiRpcInvalidRequest,
    ProactiveSourceDefinition,
)


_MONITOR_URL = "http://127.0.0.1:18765"


class FitbitMobileDashboardReader:
    """读取 monitor，并生成稳定的移动健康总览。"""

    def get_current(self) -> dict[str, object]:
        """投影当前健康快照与最近睡眠节律。"""

        # 1. 在本地 HTTP 边界取得当前快照
        snapshot = self._get_json("/api/tool/fitbit_health_snapshot")

        # 2. 只投影手机快速判断需要的字段
        return {
            "available": _boolean(snapshot, "available"),
            "freshness": {
                "last_updated": _optional_string(snapshot, "last_updated"),
                "data_lag_min": _optional_number(snapshot, "data_lag_min"),
                "spo2_lag_min": _optional_number(snapshot, "spo2_lag_min"),
            },
            "current": {
                "heart_rate": _optional_number(snapshot, "heart_rate"),
                "spo2": _optional_number(snapshot, "spo2"),
                "steps": _optional_number(snapshot, "steps"),
                "sleep_state": _optional_string(snapshot, "sleep_state") or "unknown",
                "sleep_prob": _optional_number(snapshot, "sleep_prob"),
            },
            "sleep_24h": _sleep_segments(snapshot),
        }

    def get_sleep_history(self) -> dict[str, object]:
        """投影七天睡眠摘要与逐日记录。"""

        # 1. 只读后台轮询维护的本地投影，不触发 OAuth 或 Fitbit API
        report = self._get_json("/api/mobile/sleep_projection")
        if not _boolean(report, "available"):
            return {
                "available": False,
                "reason": _optional_string(report, "reason") or "projection_not_ready",
                "freshness": _mapping(report, "freshness"),
                "sleep_summary": {
                    "days_with_data": 0,
                    "avg_duration_min": None,
                    "avg_efficiency": None,
                    "avg_deep_min": None,
                },
                "sleep_days": [],
            }

        # 2. 只投影移动端历史浏览需要的字段
        summary = _mapping(report, "summary")
        days = _list_of_mappings(report, "days")
        return {
            "available": True,
            "reason": None,
            "freshness": _mapping(report, "freshness"),
            "sleep_summary": {
                "days_with_data": _optional_number(summary, "days_with_data"),
                "avg_duration_min": _optional_number(summary, "avg_duration_min"),
                "avg_efficiency": _optional_number(summary, "avg_efficiency"),
                "avg_deep_min": _optional_number(summary, "avg_deep_min"),
            },
            "sleep_days": [_sleep_day(day) for day in reversed(days)],
        }

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str | int | float] | None = None,
    ) -> Mapping[str, object]:
        response = requests.get(f"{_MONITOR_URL}{path}", params=params, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError(f"Fitbit monitor 返回非对象: {path}")
        return payload


def _sleep_segments(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw = payload.get("sleep_24h")
    if not isinstance(raw, Mapping):
        raise TypeError("Fitbit monitor sleep_24h 必须是对象")
    segments: list[dict[str, object]] = []
    for time_range, state in raw.items():
        if not isinstance(time_range, str) or not isinstance(state, str):
            raise TypeError("Fitbit monitor sleep_24h 条目无效")
        if state not in {"sleeping", "awake", "unknown"}:
            raise TypeError(f"Fitbit monitor sleep_24h 状态无效: {state}")
        segments.append(
            {
                "range": time_range,
                "state": state,
                "duration_min": _range_duration_minutes(time_range),
            }
        )
    return segments


def _range_duration_minutes(value: str) -> int:
    try:
        start, end = value.split("-", maxsplit=1)
        start_hour, start_minute = (int(part) for part in start.split(":"))
        end_hour, end_minute = (int(part) for part in end.split(":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"Fitbit monitor 睡眠时间段无效: {value}") from error
    if not (
        0 <= start_hour < 24
        and 0 <= end_hour < 24
        and 0 <= start_minute < 60
        and 0 <= end_minute < 60
    ):
        raise ValueError(f"Fitbit monitor 睡眠时间段无效: {value}")
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    duration = (end_total - start_total) % (24 * 60)
    if duration == 0:
        return 1
    return duration


def _sleep_day(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "date": _optional_string(payload, "date"),
        "duration_min": _optional_number(payload, "duration_min"),
        "efficiency": _optional_number(payload, "efficiency"),
        "deep_min": _optional_number(payload, "deep_min"),
        "no_data": _boolean(payload, "no_data"),
    }


def _mapping(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise TypeError(f"Fitbit monitor {name} 必须是对象")
    return value


def _list_of_mappings(payload: Mapping[str, object], name: str) -> list[Mapping[str, object]]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise TypeError(f"Fitbit monitor {name} 必须是对象数组")
    return value


def _boolean(payload: Mapping[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"Fitbit monitor {name} 必须是布尔值")
    return value


def _optional_number(payload: Mapping[str, object], name: str) -> int | float | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"Fitbit monitor {name} 必须是数字或 null")
    return value


def _optional_string(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Fitbit monitor {name} 必须是字符串或 null")
    return value

class FitbitProactiveConfig(BaseModel):
    enabled: bool = True


class FitbitConfig(BaseModel):
    proactive: FitbitProactiveConfig = Field(default_factory=FitbitProactiveConfig)


api_version = 3
name = "fitbit"
version = "3.0.0"
desc = "Fitbit health monitor and sleep model"
Config = FitbitConfig
inject = (MANAGED_PROCESSES, MCP_SERVERS, PROACTIVE_COMPONENTS, UI_SLOTS)
dashboard_module = "dashboard.py"


async def apply(ctx: Context, config: FitbitConfig) -> None:
    """登记 Fitbit 进程、MCP、主动源和移动端只读投影。"""

    # 1. Core 独占 monitor 端口、进程健康和 MCP endpoint 投影。
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
            required_tools=(
                "get_proactive_events",
                "get_sleep_context",
                "acknowledge_events",
            ),
            candidate_read_only_tools=(
                "get_proactive_events",
                "get_sleep_context",
            ),
            endpoint_env=(EndpointEnv("FITBIT_MONITOR_PORT", "monitor"),),
            candidate_env={"FITBIT_BACKEND": "recording"},
        ),
    )

    # 2. 主动源只消费 typed fetch/ack，不直接持有 monitor 或进程。
    if config.proactive.enabled:
        proactive = ctx.require(PROACTIVE_COMPONENTS)
        await proactive.register(
            ctx,
            ProactiveSourceDefinition(
                name="health_alerts",
                channels=("alert",),
                mcp_server="fitbit",
                fetch_tool="get_proactive_events",
                ack_tool="acknowledge_events",
            ),
        )
        await proactive.register(
            ctx,
            ProactiveSourceDefinition(
                name="sleep_context",
                channels=("context",),
                mcp_server="fitbit",
                fetch_tool="get_sleep_context",
            ),
        )

    # 3. 静态资产与同步只读查询绑定当前 exact Root。
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
        query=_mobile_ui_query,
    )


def _mobile_ui_query(
    method: str,
    payload: dict[str, object],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> dict[str, object]:
    """按数据源独立返回当前健康或睡眠历史投影。"""

    # 1. 插件边界只暴露两种只读投影。
    _ = payload, session_id, turn_id
    readers = {
        "fitbit.current": FitbitMobileDashboardReader.get_current,
        "fitbit.sleep_history": FitbitMobileDashboardReader.get_sleep_history,
    }
    reader_method = readers.get(method)
    if reader_method is None:
        raise MobileUiRpcInvalidRequest(f"未知 fitbit 移动方法: {method}")

    # 2. Core 调度器会把同步查询隔离到专用线程池。
    return reader_method(FitbitMobileDashboardReader())
