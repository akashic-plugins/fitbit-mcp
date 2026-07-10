"""
fitbit-mcp — Fitbit 健康事件 MCP 服务。

对接 fitbit-monitor REST API，以标准 ProactiveEvent schema 暴露告警事件。
约定 schema（alert 通道）：
  event_id      str       上游事件 ID，用于 ack
  kind          str       固定值 "alert"
  source_type   str       固定值 "health_event"
  source_name   str       固定值 "fitbit"
  title         str       事件类型（hr_elevated_rest / sleep_spo2 / ...）
  content       str       人类可读告警消息
  severity      str       "high" | "medium"
  published_at  str|None  事件创建时间（ISO 格式）
  suggested_tone str      LLM 建议语气（可选附加字段）
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import requests
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

HOST = os.getenv("FITBIT_MONITOR_HOST", "127.0.0.1")
PORT = os.getenv("FITBIT_MONITOR_PORT", "18765")
BASE_URL = f"http://{HOST}:{PORT}"


def _monitor_available(timeout: float = 1.0) -> bool:
    try:
        resp = requests.get(f"{BASE_URL}/api/data", timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception:
        return False


def _to_standard_event(raw: dict) -> dict:
    """把 fitbit-monitor 的原始事件 dict 转换为标准 ProactiveEvent schema。"""
    created_at = raw.get("created_at")
    published_at = None
    if created_at:
        try:
            published_at = datetime.strptime(created_at, "%Y-%m-%d %H:%M").isoformat()
        except Exception:
            published_at = created_at

    return {
        "event_id": raw.get("id", ""),
        "kind": "alert",
        "source_type": "health_event",
        "source_name": "fitbit",
        "title": raw.get("type", ""),
        "content": raw.get("message", ""),
        "severity": raw.get("severity", ""),
        "published_at": published_at,
        "suggested_tone": raw.get("suggested_tone", ""),
        "metrics": raw.get("metrics", {}),
    }


def _fetch_agent_payload(timeout: int = 5) -> dict:
    resp = requests.get(f"{BASE_URL}/api/agent", timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _build_sleep_context(data: dict) -> dict:
    sleep = data.get("sleep", {}) or {}
    state = str(sleep.get("state", "unknown") or "unknown")
    prob = sleep.get("prob")
    lag = sleep.get("data_lag_min")
    prob_source = str(sleep.get("prob_source", "unavailable") or "unavailable")

    state_text = {
        "sleeping": "用户当前可能已经睡着",
        "awake": "用户当前更可能醒着",
        "uncertain": "用户当前是否睡着还不确定",
        "unknown": "暂时无法判断用户当前是否睡着",
    }.get(state, "暂时无法判断用户当前是否睡着")

    summary = state_text
    if prob is not None:
        summary += f"（概率 {prob:.2f}）"
    if lag is not None:
        summary += f"，数据延迟约 {lag} 分钟"
    summary += "。这是对用户当前睡眠状态的概率判断，不保证 100% 准确。"

    return {
        "topic": "Fitbit 睡眠状态判断",
        "summary": summary,
        "hint": (
            "这是对用户当前是否睡着的概率判断，不是事实确认，不能据此断言用户一定睡着或一定醒着。"
            "当判断用户可能正在睡觉时，应结合最近的聊天内容及时间戳，适当克制主动打扰，"
            "减少普通强度、可推可不推的内容。"
            "但如果出现你判断用户很可能会非常感兴趣的内容，仍然应该推送，"
            "不要因为“可能在睡觉”而一律压掉。"
            "拿不准时，默认更保守；但对明显强兴趣、高相关的内容，应优先保留发送机会。"
        ),
        "available": True,
        "sleep": {
            "state": state,
            "prob": prob,
            "prob_source": prob_source,
            "data_lag_min": lag,
        },
        "health_event_count": len(data.get("health_events") or []),
    }


def create_mcp_server() -> FastMCP:
    mcp = FastMCP("fitbit-mcp")

    @mcp.tool()
    def get_proactive_events() -> str:
        """获取 Fitbit 未处理的健康告警事件列表。

        返回标准 ProactiveEvent alert schema 的 JSON 数组。
        空数组表示当前无待处理告警。
        """
        try:
            data = _fetch_agent_payload(timeout=5)
            raw_events = data.get("health_events") or []
            events = [_to_standard_event(e) for e in raw_events]
            return json.dumps(events, ensure_ascii=False)
        except requests.exceptions.ConnectionError:
            logger.warning("fitbit-monitor 未运行 (%s)", BASE_URL)
            return json.dumps([])
        except Exception as e:
            logger.error("get_events 失败: %s", e)
            return json.dumps({"error": str(e)})

    @mcp.tool()
    def get_sleep_context() -> str:
        """获取 Fitbit 睡眠判断上下文，供 proactive 作为 context 注入。"""
        try:
            data = _fetch_agent_payload(timeout=5)
            return json.dumps(_build_sleep_context(data), ensure_ascii=False)
        except requests.exceptions.ConnectionError:
            logger.warning("fitbit-monitor 未运行 (%s)", BASE_URL)
            return json.dumps(
                {
                    "available": False,
                    "topic": "",
                    "summary": "",
                    "hint": "Fitbit 睡眠判断当前不可用；即使可用，它也只是概率判断，不保证 100% 准确。",
                    "sleep": {
                        "state": "unknown",
                        "prob": None,
                        "prob_source": "unavailable",
                        "data_lag_min": None,
                    },
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error("get_sleep_context 失败: %s", e)
            return json.dumps(
                {
                    "available": False,
                    "topic": "",
                    "summary": "",
                    "hint": f"Fitbit 睡眠判断拉取失败: {e}",
                    "sleep": {
                        "state": "unknown",
                        "prob": None,
                        "prob_source": "unavailable",
                        "data_lag_min": None,
                    },
                },
                ensure_ascii=False,
            )

    @mcp.tool()
    def fitbit_health_snapshot() -> str:
        """获取当前 Fitbit 健康状态快照。"""
        try:
            resp = requests.get(
                f"{BASE_URL}/api/tool/fitbit_health_snapshot",
                timeout=5,
            )
            resp.raise_for_status()
            return json.dumps(resp.json(), ensure_ascii=False)
        except requests.exceptions.ConnectionError as e:
            logger.warning("fitbit-monitor 未运行 (%s)", BASE_URL)
            return json.dumps({"error": f"无法连接 Fitbit monitor：{e}"}, ensure_ascii=False)
        except Exception as e:
            logger.error("fitbit_health_snapshot 失败: %s", e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @mcp.tool()
    def fitbit_sleep_report(days: int = 7) -> str:
        """获取最近 N 天 Fitbit 睡眠质量报告。"""
        try:
            days = max(1, min(int(days), 30))
            resp = requests.get(
                f"{BASE_URL}/api/sleep_report",
                params={"days": days},
                timeout=10,
            )
            if resp.status_code == 401:
                return json.dumps(
                    {"error": "Fitbit 未授权，请先完成 OAuth 授权。"},
                    ensure_ascii=False,
                )
            resp.raise_for_status()
            return json.dumps(resp.json(), ensure_ascii=False)
        except requests.exceptions.ConnectionError as e:
            logger.warning("fitbit-monitor 未运行 (%s)", BASE_URL)
            return json.dumps({"error": f"无法连接 Fitbit monitor：{e}"}, ensure_ascii=False)
        except Exception as e:
            logger.error("fitbit_sleep_report 失败: %s", e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @mcp.tool()
    def acknowledge_events(event_ids: list[str]) -> str:
        """标记健康告警事件为已处理，防止重复触发。

        Args:
            event_ids: 要 ack 的事件 ID 列表（来自 get_events 返回的 event_id 字段）。

        Returns:
            JSON 对象，包含每个 ID 的处理结果。
        """
        if not event_ids:
            return json.dumps({"acknowledged": [], "failed": []})

        acknowledged = []
        failed = []
        for eid in event_ids:
            try:
                resp = requests.post(
                    f"{BASE_URL}/api/agent/acknowledge/{eid}", timeout=5
                )
                if resp.status_code == 200 and resp.json().get("acknowledged"):
                    acknowledged.append(eid)
                else:
                    failed.append(eid)
            except Exception as e:
                logger.error("acknowledge %s 失败: %s", eid, e)
                failed.append(eid)

        return json.dumps({"acknowledged": acknowledged, "failed": failed})

    return mcp
