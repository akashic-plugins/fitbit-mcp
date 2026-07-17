from __future__ import annotations

import asyncio
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import requests
from pydantic import BaseModel, Field

from agent.plugins import ManagedServiceSpec, McpServerSpec, Plugin, ProactiveSourceSpec


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

        # 1. 在独立 HTTP 失败域取得七天报告
        report = self._get_json("/api/sleep_report", params={"days": 7})

        # 2. 只投影移动端历史浏览需要的字段
        summary = _mapping(report, "summary")
        days = _list_of_mappings(report, "days")
        return {
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


class FitbitPlugin(Plugin):
    name = "fitbit"
    version = "1.2.0"
    desc = "Fitbit health monitor and sleep model"
    ConfigModel = FitbitConfig

    @classmethod
    def mobile_ui_module(cls) -> str | None:
        return "mobile_panel.js"

    @classmethod
    def mobile_ui_stylesheet(cls) -> str | None:
        return "mobile_panel.css"

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

    async def mobile_ui_call(
        self,
        method: str,
        payload: dict[str, object],
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> dict[str, object]:
        """按数据源独立返回当前健康或睡眠历史投影。"""

        # 1. 插件边界只暴露两种只读投影
        _ = payload, session_id, turn_id
        readers = {
            "fitbit.current": FitbitMobileDashboardReader.get_current,
            "fitbit.sleep_history": FitbitMobileDashboardReader.get_sleep_history,
        }
        reader_method = readers.get(method)
        if reader_method is None:
            raise ValueError(f"未知 fitbit 移动方法: {method}")

        # 2. 本地 monitor HTTP 调用离开事件循环
        reader = FitbitMobileDashboardReader()
        return await asyncio.to_thread(reader_method, reader)

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
