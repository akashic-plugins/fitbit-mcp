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


def create_mcp_server() -> FastMCP:
    """暴露两个由用户调用的普通 Fitbit 只读工具。"""

    mcp = FastMCP("fitbit-mcp")

    @mcp.tool()
    def fitbit_health_snapshot() -> str:
        """获取当前 Fitbit 健康状态快照。"""
        try:
            response = requests.get(
                f"{BASE_URL}/api/tool/fitbit_health_snapshot",
                timeout=5,
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
            logger.error("fitbit_health_snapshot 失败: %s", error)
            return json.dumps({"error": str(error)}, ensure_ascii=False)

    @mcp.tool()
    def fitbit_sleep_report(days: int = 7) -> str:
        """获取最近 N 天 Fitbit 睡眠质量报告。"""
        bounded_days = max(1, min(int(days), 30))
        try:
            response = requests.get(
                f"{BASE_URL}/api/sleep_report",
                params={"days": bounded_days},
                timeout=10,
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
            logger.error("fitbit_sleep_report 失败: %s", error)
            return json.dumps({"error": str(error)}, ensure_ascii=False)

    return mcp
