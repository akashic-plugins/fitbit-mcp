"""供用户显式查询健康与睡眠的 Fitbit MCP 工具。"""

from __future__ import annotations

import json
import logging
import os

import requests
from mcp.server.fastmcp import FastMCP


logger = logging.getLogger(__name__)
HOST = os.getenv("FITBIT_MONITOR_HOST", "127.0.0.1")
PORT = os.getenv("FITBIT_MONITOR_PORT", "18765")
BASE_URL = f"http://{HOST}:{PORT}"


def _get_json(path: str, *, params: dict[str, str | int] | None = None, timeout: float = 10) -> str:
    try:
        response = requests.get(
            f"{BASE_URL}{path}",
            params=params,
            timeout=timeout,
        )
        if response.status_code == 401:
            return json.dumps(
                {"error": "Fitbit 未授权，请先完成 OAuth 授权。"},
                ensure_ascii=False,
            )
        response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)
    except requests.exceptions.ConnectionError as error:
        logger.warning("fitbit-monitor 未运行 (%s)", BASE_URL)
        return json.dumps(
            {"error": f"无法连接 Fitbit monitor：{error}"},
            ensure_ascii=False,
        )
    except requests.RequestException as error:
        logger.error("%s 失败: %s", path, error)
        return json.dumps({"error": str(error)}, ensure_ascii=False)


def create_mcp_server() -> FastMCP:
    """暴露由用户显式调用的 Fitbit 只读健康与运动工具。"""

    mcp = FastMCP("fitbit-mcp")

    @mcp.tool()
    def fitbit_health_snapshot() -> str:
        """获取当前 Fitbit 健康状态快照。"""
        return _get_json("/api/tool/fitbit_health_snapshot", timeout=5)

    @mcp.tool()
    def fitbit_sleep_report(days: int = 7) -> str:
        """获取最近 N 天 Fitbit 睡眠质量报告。"""
        bounded_days = max(1, min(int(days), 30))
        return _get_json("/api/sleep_report", params={"days": bounded_days}, timeout=10)

    @mcp.tool()
    def fitbit_workouts(days: int = 14, exercise_type: str = "") -> str:
        """获取最近 N 天 Fitbit 运动记录（类型、起止时间、时长、距离、热量、心率与配速）。"""
        bounded_days = max(1, min(int(days), 90))
        params: dict[str, str | int] = {"days": bounded_days}
        if exercise_type.strip():
            params["exercise_type"] = exercise_type.strip()
        return _get_json("/api/tool/fitbit_workouts", params=params, timeout=15)

    @mcp.tool()
    def fitbit_activity_report(days: int = 7) -> str:
        """获取最近 N 天日常活动（步数、距离、活跃分钟、热量、久坐时段）及静息心率、呼吸率、日血氧等指标。"""
        bounded_days = max(1, min(int(days), 30))
        return _get_json("/api/tool/fitbit_activity_report", params={"days": bounded_days}, timeout=20)

    return mcp
