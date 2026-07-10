"""
StatEngine — 健康统计分析引擎

独立模块，不修改 server.py 现有告警逻辑。每次 poll 后由 server.py 调用 update()，
基于活动分档 Z-score 和睡眠 SpO2 三指标联合，产生有意义的健康事件。

事件设计原则（参考 HROS-AD、ProAgent、JCSM 2024）：
  - hr_elevated_rest : 静息 Z-score >= 2.5，持续 >= 2 个窗口（~10 min）
  - hr_recovery      : 上述条件消失，Z-score 回落 < 1.0
  - sleep_spo2       : T90/LSpO2/ODI/均值 四指标中 >= 2 个异常，整夜结束后上报
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

# ── 阈值常量 ──────────────────────────────────────────────────────────────────

HR_ZSCORE_ALERT = 2.5  # Z-score 超过此值视为偏高
HR_ZSCORE_SEVERE = 3.5  # 超过此值为 high severity
HR_ZSCORE_RECOVERY = 1.0  # 低于此值视为恢复
HR_CONSECUTIVE_NEEDED = 3  # 至少持续几个 poll（每 poll ~5 min）→ 15min

SPO2_T90_THRESHOLD = 5.0  # T90 > 5%（整夜 SpO2 < 90% 的时间占比）
SPO2_LSPO2_THRESHOLD = 88.0  # 最低 SpO2 < 88%
SPO2_ODI_THRESHOLD = 5.0  # ODI > 5 次/hr（每小时 SpO2 下降 ≥ 3% 的次数）
SPO2_AVG_THRESHOLD = 94.0  # 整夜均值 < 94%
SPO2_MIN_SESSION_POLLS = 6  # 至少 30 分钟才分析（避免短暂 uncertain 误判）

BASELINE_MIN_POINTS = 5  # 基线数据不足时用 fallback
BASELINE_EXCLUDE_HOURS = 24  # 排除最近 N 小时（避免异常数据污染基线）

# 同类事件冷却时间（秒）
COOLDOWNS: dict[str, int] = {
    "hr_elevated_rest": 2 * 3600,
    "sleep_spo2": 24 * 3600,
    "hr_recovery": 6 * 3600,
}

# 事件过期时间（秒）—— 超时未送达则丢弃
EXPIRY: dict[str, int] = {
    "hr_elevated_rest": 4 * 3600,
    "sleep_spo2": 20 * 3600,  # 睡眠事件在次日白天前有效
    "hr_recovery": 2 * 3600,
}

# LLM 提示语气
SUGGESTED_TONE: dict[str, str] = {
    "hr_elevated_rest": "关心语气，问是否身体不适，不要直接说'你心率很高'",
    "sleep_spo2": "轻询，不用恐吓，可以问是否有类似感受（如胸闷、睡眠质量差）",
    "hr_recovery": "简短确认，不用细说",
}


# ── 数据结构 ───────────────────────────────────────────────────────────────────


@dataclass
class HealthEvent:
    id: str
    type: str
    severity: str  # high | medium
    message: str
    created_at: float  # unix timestamp
    expires_at: float
    acknowledged: bool = False
    metrics: dict = field(default_factory=dict)


# ── 工具函数 ───────────────────────────────────────────────────────────────────


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _mean_std(vals: list[float]) -> tuple[float, float]:
    """均值和标准差，std 最小为 1.0 避免除零。"""
    if not vals:
        return 86.0, 8.0
    n = len(vals)
    mean = sum(vals) / n
    variance = sum((x - mean) ** 2 for x in vals) / max(n - 1, 1)
    return mean, max(math.sqrt(variance), 1.0)


def _classify_activity(zero_steps_count: int) -> str:
    """按最近 20 轮中静止轮数分档。"""
    if zero_steps_count >= 16:
        return "resting"
    if zero_steps_count >= 8:
        return "light"
    return "active"


# ── StatEngine ────────────────────────────────────────────────────────────────


class StatEngine:
    """
    每次 poll 后调用 update(log_entry, history)，检测健康异常并维护事件队列。

    log_entry 结构（来自 server._build_log_entry）：
        poll_time, data_lag_min, state, heart_rate, spo2,
        signals.zero_steps_count

    history：list[dict]，同结构，最近 30 天，包含当前 entry。
    """

    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._lock = Lock()
        self._events: list[HealthEvent] = []
        self._last_event_time: dict[str, float] = {}

        # HR 状态机
        self._hr_consecutive_high: int = 0
        self._hr_elevated_since: float | None = None
        self._hr_peak_z: float = 0.0
        self._hr_notified: bool = False

        # 睡眠 SpO2 状态机
        self._in_sleep: bool = False
        self._sleep_spo2_polls: list[float] = []

        self._load()

    # ── 持久化 ────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            now_ts = time.time()
            self._events = [
                HealthEvent(**e)
                for e in raw.get("events", [])
                if isinstance(e, dict) and e.get("expires_at", 0) > now_ts
            ]
            self._last_event_time = dict(raw.get("last_event_time", {}))
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(
                    {
                        "events": [asdict(e) for e in self._events],
                        "last_event_time": self._last_event_time,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── 事件管理 ──────────────────────────────────────────────────────────────

    def _can_emit(self, event_type: str) -> bool:
        cooldown = COOLDOWNS.get(event_type, 3600)
        last = float(self._last_event_time.get(event_type, 0.0) or 0.0)
        return time.time() - last >= cooldown

    def _emit(self, type_: str, severity: str, message: str, metrics: dict) -> None:
        if not self._can_emit(type_):
            return
        now_ts = time.time()
        exp = EXPIRY.get(type_, 4 * 3600)
        event = HealthEvent(
            id=str(uuid.uuid4())[:8],
            type=type_,
            severity=severity,
            message=message,
            created_at=now_ts,
            expires_at=now_ts + exp,
            metrics=metrics,
        )
        self._events.append(event)
        self._last_event_time[type_] = now_ts
        self._save()

    def _purge_expired(self) -> None:
        now_ts = time.time()
        self._events = [
            e for e in self._events if e.expires_at > now_ts and not e.acknowledged
        ]

    # ── 基线计算 ──────────────────────────────────────────────────────────────

    def _resting_baseline(self, history: list[dict]) -> tuple[float, float]:
        """
        从历史中取静息段（zero_steps >= 16）心率，排除近 24 小时（防自相关），
        返回 (mean, std)。
        """
        cutoff = datetime.now() - timedelta(hours=BASELINE_EXCLUDE_HOURS)
        vals: list[float] = []
        for r in history:
            try:
                t = datetime.strptime(str(r.get("poll_time", "")), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if t > cutoff:
                continue
            sig = r.get("signals") or {}
            zs = int(sig.get("zero_steps_count", 0) or 0)
            if zs < 16:
                continue
            hr = _safe_float(r.get("heart_rate"))
            lag = _safe_float(r.get("data_lag_min"))
            if hr is None or (lag is not None and lag > 15):
                continue
            vals.append(hr)
        if len(vals) < BASELINE_MIN_POINTS:
            return 86.0, 8.0  # fallback：如数据不足用经验值
        return _mean_std(vals)

    # ── 主入口 ────────────────────────────────────────────────────────────────

    def update(self, log_entry: dict, history: list[dict]) -> None:
        """每次 poll 后由 server.py 调用。"""
        with self._lock:
            self._purge_expired()
            self._update_hr(log_entry, history)
            self._update_spo2(log_entry)

    # ── HR 检测 ───────────────────────────────────────────────────────────────

    def _update_hr(self, entry: dict, history: list[dict]) -> None:
        sig = entry.get("signals") or {}
        zs = int(sig.get("zero_steps_count", 0) or 0)
        activity = _classify_activity(zs)
        hr = _safe_float(entry.get("heart_rate"))
        lag = _safe_float(entry.get("data_lag_min"))
        state = str(entry.get("state", ""))

        # 数据不新鲜、非静息、睡眠中 —— 不做静息 HR 判断
        if (
            hr is None
            or (lag is not None and lag > 15)
            or activity != "resting"
            or state == "sleeping"
        ):
            # 活动中的高心率正常，重置状态机（但保留 elevated_since 用于 recovery）
            if activity in ("active", "light"):
                self._hr_consecutive_high = 0
                self._hr_notified = False
            return

        mean, std = self._resting_baseline(history)
        z = (hr - mean) / std

        if z >= HR_ZSCORE_ALERT:
            self._hr_consecutive_high += 1
            self._hr_peak_z = max(self._hr_peak_z, z)
            if self._hr_elevated_since is None:
                self._hr_elevated_since = time.time()

            if (
                self._hr_consecutive_high >= HR_CONSECUTIVE_NEEDED
                and not self._hr_notified
            ):
                severity = "high" if z >= HR_ZSCORE_SEVERE else "medium"
                duration_min = self._hr_consecutive_high * 5
                level_desc = "明显偏高" if z >= HR_ZSCORE_SEVERE else "持续偏高"
                self._emit(
                    "hr_elevated_rest",
                    severity,
                    f"静息心率{level_desc}，已持续约 {duration_min} 分钟",
                    {
                        "hr": hr,
                        "baseline_mean": round(mean, 1),
                        "baseline_std": round(std, 1),
                        "z_score": round(z, 2),
                        "duration_min": duration_min,
                    },
                )
                self._hr_notified = True
        else:
            # Z-score 回落到正常
            if self._hr_elevated_since is not None and z < HR_ZSCORE_RECOVERY:
                duration_min = int((time.time() - self._hr_elevated_since) / 60)
                # 只有之前发过告警、且偏高时长 >= 20 分钟才发 recovery（短暂尖峰不通知恢复）
                if self._hr_notified and duration_min >= 20:
                    self._emit(
                        "hr_recovery",
                        "medium",
                        f"心率已恢复正常（此前偏高约 {duration_min} 分钟）",
                        {
                            "hr": hr,
                            "duration_elevated_min": duration_min,
                            "peak_z": round(self._hr_peak_z, 2),
                        },
                    )
                # 重置状态机
                self._hr_elevated_since = None
                self._hr_peak_z = 0.0
                self._hr_notified = False
            self._hr_consecutive_high = 0

    # ── SpO2 检测 ─────────────────────────────────────────────────────────────

    def _update_spo2(self, entry: dict) -> None:
        state = str(entry.get("state", ""))
        spo2 = _safe_float(entry.get("spo2"))
        lag = _safe_float(entry.get("data_lag_min"))
        is_sleep = state == "sleeping"

        if is_sleep and spo2 is not None and (lag is None or lag <= 15):
            if not self._in_sleep:
                self._in_sleep = True
                self._sleep_spo2_polls = []
            self._sleep_spo2_polls.append(spo2)
        else:
            if self._in_sleep:
                if len(self._sleep_spo2_polls) >= SPO2_MIN_SESSION_POLLS:
                    self._analyze_sleep_session()
                self._in_sleep = False

    def _analyze_sleep_session(self) -> None:
        vals = self._sleep_spo2_polls
        n = len(vals)
        duration_min = n * 5

        avg_spo2 = sum(vals) / n
        l_spo2 = min(vals)

        t90_count = sum(1 for v in vals if v < 90.0)
        t90_pct = t90_count / n * 100

        odi_events = sum(1 for i in range(1, n) if vals[i - 1] - vals[i] >= 3.0)
        odi_per_hour = odi_events / max(duration_min / 60, 0.5)

        flags = {
            "t90": t90_pct > SPO2_T90_THRESHOLD,
            "l_spo2": l_spo2 < SPO2_LSPO2_THRESHOLD,
            "odi": odi_per_hour > SPO2_ODI_THRESHOLD,
            "avg_low": avg_spo2 < SPO2_AVG_THRESHOLD,
        }
        score = sum(flags.values())
        if score < 2:
            return

        severity = "high" if (flags["t90"] or flags["l_spo2"]) else "medium"

        qual_parts: list[str] = []
        if flags["t90"]:
            qual_parts.append("低氧时间占比偏高")
        if flags["l_spo2"]:
            qual_parts.append("最低值明显偏低")
        if flags["odi"]:
            qual_parts.append("血氧波动频繁")
        if flags["avg_low"]:
            qual_parts.append("均值偏低")

        self._emit(
            "sleep_spo2",
            severity,
            f"睡眠期间血氧偏低（{', '.join(qual_parts)}，持续约 {duration_min} 分钟）",
            {
                "t90_pct": round(t90_pct, 1),
                "l_spo2": round(l_spo2, 1),
                "odi_per_hour": round(odi_per_hour, 2),
                "avg_spo2": round(avg_spo2, 1),
                "duration_min": duration_min,
                "flags": flags,
            },
        )

    # ── 对外接口 ──────────────────────────────────────────────────────────────

    def get_pending_events(self) -> list[dict]:
        """返回未 acknowledged 且未过期的事件，供 /api/agent 使用。"""
        with self._lock:
            self._purge_expired()
            return [
                {
                    "id": e.id,
                    "type": e.type,
                    "severity": e.severity,
                    "message": e.message,
                    "created_at": datetime.fromtimestamp(e.created_at).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "suggested_tone": SUGGESTED_TONE.get(e.type, "自然提及"),
                }
                for e in self._events
                if not e.acknowledged
            ]

    def acknowledge(self, event_id: str) -> bool:
        """标记事件已处理（LLM 发送后调用）。"""
        with self._lock:
            for e in self._events:
                if e.id == event_id:
                    e.acknowledged = True
                    self._save()
                    return True
        return False
