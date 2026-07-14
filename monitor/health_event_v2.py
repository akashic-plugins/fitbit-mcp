"""
health_event_v2.py

离线/旁路健康事件生成器（v2）。
设计目标：
1. 不影响现有 stat_engine 主链路；
2. 只依赖 sleep_log.jsonl 现有字段；
3. 用个体化基线 + 多信号融合，生成更稳定的上游 health events。
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _parse_dt(v: Any) -> datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    fmts = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(s[:19], fmt)
        except Exception:
            pass
    return None


def _parse_hms(v: Any) -> tuple[int, int, int] | None:
    s = str(v or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) < 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2]) if len(parts) >= 3 else 0
    except Exception:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    return hh, mm, ss


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(median(vals))


def _mad(vals: list[float], center: float) -> float:
    if not vals:
        return 0.0
    dev = [abs(x - center) for x in vals]
    return float(median(dev))


def _effective_sleeping(state: Any) -> bool:
    s = str(state or "").strip().lower()
    return s in {"sleeping", "uncertain"}


@dataclass
class V2Event:
    event_id: str
    type: str
    severity: str  # high | medium
    confidence: float  # 0-1
    message: str
    created_at: str
    source_type: str = "health_event"
    source_name: str = "fitbit_v2_offline"
    suggested_tone: str = "关心语气，先询问感受，不做医疗诊断"
    metrics: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["confidence"] = round(float(self.confidence), 3)
        return d


class HealthEventV2Engine:
    """
    仅离线回放使用：
    - 急性静息心血管压力（acute_cardio_stress）
    - 心肺耦合压力（cardio_respiratory_strain）
    """

    def __init__(self) -> None:
        self._events: list[V2Event] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=20000)
        self._cooldown_until: dict[str, datetime] = {}
        self._hr_cusum = 0.0
        self._hr_windows: deque[datetime] = deque(maxlen=12)
        self._hr_peak_z = 0.0
        self._hr_episode_active = False
        self._last_hr_evidence_id: str | None = None
        self._strain_run = 0
        self._spo2_seen: set[str] = set()
        self._active_sleep_session_key: str | None = None
        self._last_sleep_session_key: str | None = None
        self._last_sleep_session_end_dt: datetime | None = None

    def process(self, rows: list[dict[str, Any]]) -> list[V2Event]:
        for row in rows:
            self._process_row(row)
        return list(self._events)

    def ingest_row(self, row: dict[str, Any]) -> list[V2Event]:
        before = len(self._events)
        self._process_row(row)
        return self._events[before:]

    def _sync_sleep_session_state(self, dt: datetime, sleeping_like: bool) -> None:
        if sleeping_like:
            if self._active_sleep_session_key is None:
                self._active_sleep_session_key = dt.strftime("%Y-%m-%d %H:%M:%S")
            return
        if self._active_sleep_session_key is not None:
            self._last_sleep_session_key = self._active_sleep_session_key
            self._last_sleep_session_end_dt = dt
            self._active_sleep_session_key = None

    def _resolve_spo2_session_key(self, dt: datetime) -> str | None:
        if self._active_sleep_session_key is not None:
            return self._active_sleep_session_key
        if (
            self._last_sleep_session_key is not None
            and self._last_sleep_session_end_dt is not None
            and 0.0 <= (dt - self._last_sleep_session_end_dt).total_seconds() / 60.0 <= 180.0
        ):
            return self._last_sleep_session_key
        return None

    def _process_row(self, row: dict[str, Any]) -> None:
        dt = _parse_dt(row.get("poll_time"))
        if dt is None:
            return
        signals = row.get("signals") or {}
        state = str(row.get("state") or "")
        sleeping_like = _effective_sleeping(state)
        self._sync_sleep_session_state(dt, sleeping_like)
        lag = _safe_float(row.get("data_lag_min"))
        hr = _safe_float(signals.get("hr_avg"))
        if hr is None:
            hr = _safe_float(row.get("heart_rate"))
        spo2 = _safe_float(row.get("spo2"))
        spo2_lag_min = _safe_float(row.get("spo2_lag_min"))
        zero_steps = int(signals.get("zero_steps_count", 0) or 0)
        sustained_zero_min = int(signals.get("sustained_zero_min", 0) or 0)
        is_resting = zero_steps >= 16
        quality = self._quality_score(lag)
        evidence_id = str(signals.get("evidence_id") or "").strip()
        if not evidence_id:
            data_time = str(row.get("data_time") or "").strip()
            evidence_id = f"{dt:%Y-%m-%d} {data_time}" if data_time else f"{dt:%Y-%m-%d %H:%M}"
        is_new_hr_evidence = evidence_id != self._last_hr_evidence_id
        if is_new_hr_evidence:
            self._last_hr_evidence_id = evidence_id

        dkey = dt.strftime("%Y-%m-%d")
        spo2_time = row.get("spo2_time")
        spo2_hms = _parse_hms(spo2_time)
        session_key = None
        if spo2 is not None and spo2_hms is not None:
            spo2_key = f"{dkey} {spo2_hms[0]:02d}:{spo2_hms[1]:02d}:{spo2_hms[2]:02d}"
            if spo2_key not in self._spo2_seen:
                self._spo2_seen.add(spo2_key)
                session_key = self._resolve_spo2_session_key(dt)

        spo2_for_history = spo2 if session_key is not None else None
        spo2_for_strain = (
            spo2
            if session_key is not None
            and spo2_lag_min is not None
            and spo2_lag_min <= 180
            else None
        )

        base_hr_med, base_hr_scale = self._hr_baseline(dt)
        base_spo2_med = self._spo2_baseline(dt)
        hr_z = None
        if hr is not None and base_hr_med is not None and base_hr_scale > 0:
            hr_z = (hr - base_hr_med) / base_hr_scale
        spo2_threshold = (
            max(88.0, min(94.0, base_spo2_med - 2.0))
            if base_spo2_med is not None
            else 91.5
        )

        self._detect_acute_cardio_stress(
            dt=dt,
            hr=hr,
            hr_z=hr_z,
            base_hr=base_hr_med,
            is_resting=is_resting,
            sustained_zero_min=sustained_zero_min,
            state=state,
            quality=quality,
            is_new_evidence=is_new_hr_evidence,
        )
        self._detect_cardio_respiratory_strain(
            dt=dt,
            hr=hr,
            hr_z=hr_z,
            spo2=spo2_for_strain,
            spo2_threshold=spo2_threshold,
            is_resting=is_resting,
            state=state,
            quality=quality,
        )

        self._history.append(
            {
                "dt": dt,
                "state": state,
                "lag": lag,
                "quality": quality,
                "is_resting": is_resting,
                "evidence_id": evidence_id,
                "hr": hr,
                "spo2": spo2_for_history,
            }
        )

    def _quality_score(self, lag_min: float | None) -> float:
        if lag_min is None:
            return 0.75
        if lag_min <= 10:
            return 1.0
        if lag_min <= 20:
            return 0.9
        if lag_min <= 30:
            return 0.75
        if lag_min <= 45:
            return 0.55
        return 0.35

    def _hr_baseline(self, now_dt: datetime) -> tuple[float | None, float]:
        """计算过去 28 天同一时段的去重静息心率基线。"""

        # 1. 选取同一昼夜时段，排除最近 24 小时的自相关数据
        vals: list[float] = []
        seen_evidence: set[str] = set()
        cutoff_recent = now_dt - timedelta(hours=24)
        cutoff_old = now_dt - timedelta(days=28)
        now_minute = now_dt.hour * 60 + now_dt.minute
        for e in self._history:
            dt = e["dt"]
            if dt < cutoff_old or dt >= cutoff_recent:
                continue
            minute = dt.hour * 60 + dt.minute
            clock_distance = abs(minute - now_minute)
            clock_distance = min(clock_distance, 24 * 60 - clock_distance)
            if clock_distance > 90:
                continue
            if e["quality"] < 1.0:
                continue
            if not e["is_resting"]:
                continue
            if e["state"] == "sleeping":
                continue
            evidence_id = str(e["evidence_id"])
            if evidence_id in seen_evidence:
                continue
            seen_evidence.add(evidence_id)
            hr = e["hr"]
            if hr is not None:
                vals.append(float(hr))

        # 2. 用中位数和 MAD 构造抗离群值基线
        if len(vals) < 20:
            return None, 5.0
        med = _median(vals)
        if med is None:
            return None, 5.0
        mad = _mad(vals, med)
        scale = max(1.4826 * mad, 5.0)
        return med, scale

    def _spo2_baseline(self, now_dt: datetime) -> float | None:
        vals: list[float] = []
        cutoff_recent = now_dt - timedelta(hours=24)
        cutoff_old = now_dt - timedelta(days=21)
        for e in self._history:
            dt = e["dt"]
            if dt < cutoff_old or dt >= cutoff_recent:
                continue
            if e["quality"] < 0.75:
                continue
            s = e["spo2"]
            if s is not None:
                vals.append(float(s))
        if len(vals) < 30:
            return None
        return _median(vals)

    def _cooldown_ok(self, event_type: str, now_dt: datetime, minutes: int) -> bool:
        until = self._cooldown_until.get(event_type)
        if until is not None and now_dt < until:
            return False
        self._cooldown_until[event_type] = now_dt + timedelta(minutes=minutes)
        return True

    def _make_event_id(
        self,
        event_type: str,
        created_at: str,
        msg: str,
        metrics: dict[str, Any],
    ) -> str:
        # 带上关键指标指纹，避免同分钟同文案事件 ID 冲突。
        metrics_fingerprint = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
        raw = f"{event_type}|{created_at}|{msg}|{metrics_fingerprint}"
        return "v2_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _append_event(
        self,
        *,
        event_type: str,
        severity: str,
        confidence: float,
        message: str,
        dt: datetime,
        metrics: dict[str, Any],
        reasons: list[str],
        tone: str,
    ) -> None:
        created_at = dt.strftime("%Y-%m-%d %H:%M")
        ev = V2Event(
            event_id=self._make_event_id(event_type, created_at, message, metrics),
            type=event_type,
            severity=severity,
            confidence=max(0.0, min(1.0, confidence)),
            message=message,
            created_at=created_at,
            suggested_tone=tone,
            metrics=metrics,
            reasons=reasons,
        )
        self._events.append(ev)

    def _detect_acute_cardio_stress(
        self,
        *,
        dt: datetime,
        hr: float | None,
        hr_z: float | None,
        base_hr: float | None,
        is_resting: bool,
        sustained_zero_min: int,
        state: str,
        quality: float,
        is_new_evidence: bool,
    ) -> None:
        """用去重窗口和单侧 CUSUM 检测持续静息心率升高。"""

        # 1. 重复轮询不能增加持续时长或累计证据
        if not is_new_evidence:
            if quality < 1.0 or state == "sleeping" or not is_resting:
                self._reset_hr_episode()
            return

        # 2. 只接纳新鲜、持续静止且非睡眠的心率窗口
        if (
            hr is None
            or hr_z is None
            or base_hr is None
            or state == "sleeping"
            or not is_resting
            or sustained_zero_min < 20
            or quality < 1.0
        ):
            self._reset_hr_episode()
            return

        hr_delta = hr - base_hr
        if hr_z < 2.5 or hr_delta < 15.0:
            self._reset_hr_episode()
            return

        # 3. 累积连续偏高程度，并要求三个窗口覆盖至少 25 分钟
        if self._hr_windows and (dt - self._hr_windows[-1]).total_seconds() > 20 * 60:
            self._reset_hr_episode()
        self._hr_cusum = max(0.0, self._hr_cusum + hr_z - 1.0)
        self._hr_windows.append(dt)
        self._hr_peak_z = max(self._hr_peak_z, hr_z)
        if self._hr_episode_active or len(self._hr_windows) < 3:
            return
        window_span_min = (self._hr_windows[-1] - self._hr_windows[0]).total_seconds() / 60.0
        if window_span_min < 25 or self._hr_cusum < 6.0:
            return

        # 4. 同一段连续异常只生成一次提醒
        self._hr_episode_active = True
        severe = hr >= 120 or self._hr_peak_z >= 4.0
        severity = "high" if severe else "medium"
        confidence = min(
            0.98,
            0.62
            + 0.04 * min(8.0, self._hr_cusum)
            + 0.06 * min(3.0, max(0.0, self._hr_peak_z - 2.5)),
        )
        self._append_event(
            event_type="acute_cardio_stress",
            severity=severity,
            confidence=confidence,
            message=f"静息状态心率持续偏高（至少 {round(window_span_min)} 分钟）",
            dt=dt,
            metrics={
                "hr": round(hr, 1),
                "baseline_hr": round(base_hr, 1),
                "hr_delta": round(hr_delta, 1),
                "hr_z": round(hr_z, 2),
                "cusum": round(self._hr_cusum, 2),
                "unique_windows": len(self._hr_windows),
                "window_span_min": round(window_span_min, 1),
            },
            reasons=[
                "低活动状态（zero_steps_count>=16）",
                "持续静止至少 20 分钟",
                "排除睡眠状态",
                "同一时段个体基线偏离达到持续阈值",
            ],
            tone="先关心体感与是否在活动/紧张，不直接下结论",
        )

    def _reset_hr_episode(self) -> None:
        self._hr_cusum = 0.0
        self._hr_windows.clear()
        self._hr_peak_z = 0.0
        self._hr_episode_active = False

    def _detect_cardio_respiratory_strain(
        self,
        *,
        dt: datetime,
        hr: float | None,
        hr_z: float | None,
        spo2: float | None,
        spo2_threshold: float,
        is_resting: bool,
        state: str,
        quality: float,
    ) -> None:
        if (
            hr is None
            or hr_z is None
            or spo2 is None
            or not is_resting
            or state == "sleeping"
            or quality < 0.8
            or hr_z < 2.0
            or spo2 > spo2_threshold
        ):
            self._strain_run = 0
            return
        self._strain_run += 1
        if self._strain_run < 2:
            return
        if not self._cooldown_ok("cardio_respiratory_strain", dt, minutes=360):
            return
        confidence = min(0.99, 0.68 + 0.10 * min(4, self._strain_run) + 0.07 * quality)
        self._append_event(
            event_type="cardio_respiratory_strain",
            severity="high",
            confidence=confidence,
            message="心率偏高与血氧偏低同时出现（低活动状态）",
            dt=dt,
            metrics={
                "hr": round(hr, 1),
                "hr_z": round(hr_z, 2),
                "spo2": round(spo2, 1),
                "spo2_threshold": round(spo2_threshold, 1),
                "run_polls": self._strain_run,
            },
            reasons=[
                "心率与血氧异常同窗出现",
                "低活动状态下更值得关注",
            ],
            tone="优先关怀，建议先休息补水并观察状态变化",
        )

def load_log_rows(
    *,
    log_path: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    start_dt = _parse_dt(f"{start_date} 00:00:00") if start_date else None
    end_dt = _parse_dt(f"{end_date} 23:59:59") if end_date else None
    rows: list[dict[str, Any]] = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            dt = _parse_dt(row.get("poll_time"))
            if dt is None:
                continue
            if start_dt and dt < start_dt:
                continue
            if end_dt and dt > end_dt:
                continue
            rows.append(row)
    rows.sort(key=lambda x: str(x.get("poll_time", "")))
    return rows


class HealthEventV2Runtime:
    """
    运行时包装器：复用 v2 检测策略，并提供与 StatEngine 兼容的接口。
    - update(log_entry, history)
    - get_pending_events()
    - acknowledge(event_id)
    """

    EXPIRY_HOURS: dict[str, int] = {
        "acute_cardio_stress": 6,
        "cardio_respiratory_strain": 10,
    }
    ACK_KEEP_MAX = 5000

    def __init__(self, state_path: Path, *, max_event_age_hours: int = 12) -> None:
        self._path = Path(state_path)
        self._pending: dict[str, dict[str, Any]] = {}
        self._acked: list[str] = []
        self._max_event_age_hours = max(1, int(max_event_age_hours))
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            pending = raw.get("pending", [])
            if isinstance(pending, list):
                for e in pending:
                    if not isinstance(e, dict):
                        continue
                    # 升级后不再恢复旧版本遗留的恢复债事件。
                    if e.get("type") == "recovery_debt":
                        continue
                    eid = str(e.get("id", "")).strip()
                    if eid:
                        self._pending[eid] = dict(e)
            acked = raw.get("acked_ids", [])
            if isinstance(acked, list):
                self._acked = [str(x) for x in acked if str(x).strip()]
        except Exception:
            # 状态文件损坏时降级为空，不影响主流程
            self._pending = {}
            self._acked = []

    def _save(self) -> None:
        try:
            payload = {
                "pending": list(self._pending.values()),
                "acked_ids": self._acked[-self.ACK_KEEP_MAX :],
            }
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _purge_expired(self) -> None:
        now = datetime.now()
        now_ts = now.timestamp()
        remove_ids: list[str] = []
        for eid, e in self._pending.items():
            exp = _safe_float(e.get("expires_at_ts"))
            created = _parse_dt(e.get("created_at"))
            too_old = (
                created is not None
                and (now - created).total_seconds() > self._max_event_age_hours * 3600
            )
            if too_old or (exp is not None and exp <= now_ts):
                remove_ids.append(eid)
        for eid in remove_ids:
            self._pending.pop(eid, None)

    def update(self, log_entry: dict[str, Any], history: list[dict[str, Any]]) -> None:
        # 每轮基于历史重算，确保重启后也能稳定得到同一批 v2 事件。
        detector = HealthEventV2Engine()
        all_events = [e.to_dict() for e in detector.process(list(history))]
        acked_set = set(self._acked)
        self._purge_expired()

        for ev in all_events:
            eid = str(ev.get("event_id", "")).strip()
            if not eid or eid in self._pending or eid in acked_set:
                continue
            created = _parse_dt(ev.get("created_at"))
            if created is None:
                created = _parse_dt(log_entry.get("poll_time")) or datetime.now()
            if (datetime.now() - created).total_seconds() > self._max_event_age_hours * 3600:
                continue
            etype = str(ev.get("type") or "unknown")
            exp_h = int(self.EXPIRY_HOURS.get(etype, 8))
            exp_ts = (created + timedelta(hours=exp_h)).timestamp()
            if exp_ts <= datetime.now().timestamp():
                continue
            self._pending[eid] = {
                "id": eid,
                "type": etype,
                "severity": str(ev.get("severity") or "medium"),
                "message": str(ev.get("message") or ""),
                "created_at": str(ev.get("created_at") or ""),
                "suggested_tone": str(
                    ev.get("suggested_tone") or "关心语气，先询问感受，不做医疗诊断"
                ),
                "confidence": _safe_float(ev.get("confidence")),
                "metrics": ev.get("metrics"),
                "expires_at_ts": exp_ts,
            }

        self._purge_expired()
        self._save()

    def get_pending_events(self) -> list[dict[str, Any]]:
        self._purge_expired()
        events = list(self._pending.values())
        events.sort(key=lambda x: str(x.get("created_at", "")))
        return [
            {
                "id": e.get("id"),
                "type": e.get("type"),
                "severity": e.get("severity"),
                "message": e.get("message"),
                "created_at": e.get("created_at"),
                "suggested_tone": e.get("suggested_tone"),
                "confidence": e.get("confidence"),
                "metrics": e.get("metrics"),
            }
            for e in events
        ]

    def acknowledge(self, event_id: str) -> bool:
        eid = str(event_id or "").strip()
        if not eid:
            return False
        existed = eid in self._pending
        self._pending.pop(eid, None)
        self._acked.append(eid)
        if len(self._acked) > self.ACK_KEEP_MAX:
            self._acked = self._acked[-self.ACK_KEEP_MAX :]
        self._save()
        return existed
