#!/usr/bin/env python3
"""
Post-train acceptance guard for sleep model.

Goal:
- Protect hard constraint: outside sleep-edge buffer, avoid awake->sleeping regressions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import json

try:
    import joblib
except Exception as e:  # pragma: no cover
    raise SystemExit(f"joblib not available: {e}")

import eval_replay
import sleep_model


def _parse_windows(labels: list[dict]) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    for w in labels:
        try:
            s = sleep_model._parse_dt(str(w["start"]))  # type: ignore[attr-defined]
            e = sleep_model._parse_dt(str(w["end"]))  # type: ignore[attr-defined]
        except Exception:
            continue
        if e <= s:
            continue
        windows.append((s, e))
    windows.sort(key=lambda x: x[0])
    return windows


def _in_buffer(
    ts: datetime, windows: list[tuple[datetime, datetime]], buffer_min: int
) -> bool:
    margin = timedelta(minutes=max(0, int(buffer_min)))
    for s, e in windows:
        if (s - margin) <= ts <= (e + margin):
            return True
    return False


def _truth_bucket(
    ts: datetime, windows: list[tuple[datetime, datetime]], edge_buffer_min: int
) -> str:
    edge_margin = timedelta(minutes=max(0, int(edge_buffer_min)))
    for s, e in windows:
        if s <= ts <= e:
            return "sleep_core"
        if (s - edge_margin) <= ts < s or e < ts <= (e + edge_margin):
            return "sleep_edge"
    return "awake_far"


def _eval_one_model(
    model,
    *,
    entries: list[dict],
    labels: list[dict],
    windows: list[tuple[datetime, datetime]],
    buffer_min: int,
    edge_buffer_min: int,
) -> dict:
    sm = eval_replay.StateMachine()
    awake_core_total = 0
    awake_core_to_sleep = 0
    awake_core_to_uncertain = 0
    sleep_total = 0
    sleep_to_awake = 0
    sleep_to_uncertain = 0
    sleep_core_total = 0
    sleep_core_to_sleep = 0
    sleep_core_to_awake = 0
    sleep_core_to_uncertain = 0
    awake_far_total = 0
    awake_far_to_sleep = 0
    awake_far_to_uncertain = 0
    states_by_window: list[list[tuple[datetime, str]]] = [[] for _ in windows]
    pred_points: list[tuple[datetime, str]] = []

    for e in entries:
        poll_time = str(e.get("poll_time", ""))
        poll_dt = e.get("_poll_dt")
        if not isinstance(poll_dt, datetime):
            poll_dt = eval_replay.parse_poll_dt(poll_time)
        if poll_dt is None:
            continue
        signals = dict(e.get("signals", {}) or {})
        lag = e.get("data_lag_min")
        lag_int = int(lag) if isinstance(lag, (int, float)) else None
        data_time = str(e.get("data_time") or "")
        evidence_id = f"{poll_time[:10]} {data_time}" if data_time else poll_time
        signals["data_lag_min"] = lag_int
        signals["evidence_id"] = evidence_id
        signals["poll_time"] = poll_time

        prob = sleep_model.predict(model, signals, lag_int)
        prob = max(0.0, min(1.0, float(prob)))
        raw_state, raw_reason = eval_replay.raw_state_from_prob(prob, signals)
        new_state, _ = sm.next_state(prob, raw_reason, signals)
        pred_points.append((poll_dt, new_state))

        truth_sleep = sleep_model.is_sleeping(poll_time, labels)
        if truth_sleep:
            sleep_total += 1
            if new_state == "awake":
                sleep_to_awake += 1
            elif new_state == "uncertain":
                sleep_to_uncertain += 1
        else:
            if not _in_buffer(poll_dt, windows, buffer_min):
                awake_core_total += 1
                if new_state == "sleeping":
                    awake_core_to_sleep += 1
                elif new_state == "uncertain":
                    awake_core_to_uncertain += 1
        bucket = _truth_bucket(poll_dt, windows, edge_buffer_min)
        if bucket == "sleep_core":
            sleep_core_total += 1
            if new_state == "sleeping":
                sleep_core_to_sleep += 1
            elif new_state == "awake":
                sleep_core_to_awake += 1
            else:
                sleep_core_to_uncertain += 1
        elif bucket == "awake_far":
            awake_far_total += 1
            if new_state == "sleeping":
                awake_far_to_sleep += 1
            elif new_state == "uncertain":
                awake_far_to_uncertain += 1
        for idx, (s, e2) in enumerate(windows):
            if s <= poll_dt <= e2:
                states_by_window[idx].append((poll_dt, new_state))
                break

    def _rate(n: int, d: int) -> float:
        if d <= 0:
            return 0.0
        return n / d

    window_total = len(windows)
    window_no_sleeping = 0
    after_first_total = 0
    after_first_sleeping = 0
    after_first_awake = 0
    after_first_uncertain = 0
    for seq in states_by_window:
        if not seq:
            continue
        first_sleeping_at = next((dt for dt, state in seq if state == "sleeping"), None)
        if first_sleeping_at is None:
            window_no_sleeping += 1
            continue
        for dt, state in seq:
            if dt < first_sleeping_at:
                continue
            after_first_total += 1
            if state == "sleeping":
                after_first_sleeping += 1
            elif state == "awake":
                after_first_awake += 1
            else:
                after_first_uncertain += 1

    return {
        "awake_core_total": awake_core_total,
        "awake_core_to_sleep": awake_core_to_sleep,
        "awake_core_to_sleep_rate": _rate(awake_core_to_sleep, awake_core_total),
        "awake_core_to_uncertain": awake_core_to_uncertain,
        "awake_core_to_uncertain_rate": _rate(
            awake_core_to_uncertain, awake_core_total
        ),
        "sleep_total": sleep_total,
        "sleep_to_awake": sleep_to_awake,
        "sleep_to_awake_rate": _rate(sleep_to_awake, sleep_total),
        "sleep_to_uncertain": sleep_to_uncertain,
        "sleep_to_uncertain_rate": _rate(sleep_to_uncertain, sleep_total),
        "sleep_core_total": sleep_core_total,
        "sleep_core_to_sleep": sleep_core_to_sleep,
        "sleep_core_sleeping_rate": _rate(sleep_core_to_sleep, sleep_core_total),
        "sleep_core_to_awake": sleep_core_to_awake,
        "sleep_core_to_awake_rate": _rate(sleep_core_to_awake, sleep_core_total),
        "sleep_core_to_uncertain": sleep_core_to_uncertain,
        "sleep_core_to_uncertain_rate": _rate(
            sleep_core_to_uncertain, sleep_core_total
        ),
        "awake_far_total": awake_far_total,
        "awake_far_to_sleep": awake_far_to_sleep,
        "awake_far_to_sleep_rate": _rate(awake_far_to_sleep, awake_far_total),
        "awake_far_to_uncertain": awake_far_to_uncertain,
        "awake_far_to_uncertain_rate": _rate(
            awake_far_to_uncertain, awake_far_total
        ),
        "sleep_window_total": window_total,
        "sleep_window_no_sleeping": window_no_sleeping,
        "sleep_window_no_sleeping_rate": _rate(window_no_sleeping, window_total),
        "core_after_first_total": after_first_total,
        "core_after_first_sleeping": after_first_sleeping,
        "core_after_first_sleeping_rate": _rate(
            after_first_sleeping, after_first_total
        ),
        "core_after_first_awake": after_first_awake,
        "core_after_first_awake_rate": _rate(after_first_awake, after_first_total),
        "core_after_first_uncertain": after_first_uncertain,
        "core_after_first_uncertain_rate": _rate(
            after_first_uncertain, after_first_total
        ),
        "pred_points": pred_points,
    }


def _edge_events(
    points: list[tuple[datetime, str]],
) -> tuple[list[datetime], list[datetime]]:
    if not points:
        return [], []
    starts: list[datetime] = []
    ends: list[datetime] = []
    prev = points[0][1]
    for dt, state in points[1:]:
        if prev != "sleeping" and state == "sleeping":
            starts.append(dt)
        if prev == "sleeping" and state != "sleeping":
            ends.append(dt)
        prev = state
    return starts, ends


def _nearest_event(
    events: list[datetime], target: datetime, left_min: int, right_min: int
) -> datetime | None:
    left = target - timedelta(minutes=abs(left_min))
    right = target + timedelta(minutes=abs(right_min))
    cands = [t for t in events if left <= t <= right]
    if not cands:
        return None
    return min(cands, key=lambda t: abs((t - target).total_seconds()))


def _event_window_metrics(
    windows: list[tuple[datetime, datetime]],
    starts: list[datetime],
    ends: list[datetime],
    *,
    start_early_min: int,
    start_late_min: int,
    wake_early_min: int,
    wake_late_min: int,
) -> dict[str, float | int]:
    total = len(windows)
    start_hit = 0
    wake_hit = 0
    both_hit = 0
    wake_late_over_60 = 0
    for s, e in windows:
        ps = _nearest_event(starts, s, left_min=180, right_min=360)
        pe = _nearest_event(ends, e, left_min=360, right_min=360)
        s_ok = False
        e_ok = False
        if ps is not None:
            se = (ps - s).total_seconds() / 60.0
            s_ok = start_early_min <= se <= start_late_min
            start_hit += int(s_ok)
        if pe is not None:
            ee = (pe - e).total_seconds() / 60.0
            e_ok = wake_early_min <= ee <= wake_late_min
            wake_hit += int(e_ok)
            wake_late_over_60 += int(ee > 60.0)
        if ps is not None and pe is not None and s_ok and e_ok:
            both_hit += 1
    rate = (lambda n: (n / total) if total > 0 else 0.0)
    return {
        "event_total": total,
        "start_hit": start_hit,
        "wake_hit": wake_hit,
        "both_hit": both_hit,
        "start_hit_rate": rate(start_hit),
        "wake_hit_rate": rate(wake_hit),
        "both_hit_rate": rate(both_hit),
        "wake_late_over_60min_count": wake_late_over_60,
    }


def run_guard(
    *,
    baseline_model_path: Path | str,
    candidate_model_path: Path | str,
    eval_days: int = 2,
    recent_eval_days: int = 7,
    buffer_min: int = 45,
    edge_buffer_min: int = 30,
    start_early_min: int = -30,
    start_late_min: int = 15,
    wake_early_min: int = -15,
    wake_late_min: int = 30,
    max_wake_late_over_60min_count: int = 0,
    max_awake_core_sleep_rate: float = 0.0,
    max_awake_core_sleep_rate_delta: float = 0.0,
    max_awake_far_sleep_rate: float = 0.08,
    max_awake_far_sleep_rate_delta: float = 0.02,
    max_sleep_core_uncertain_rate: float = 0.18,
    max_sleep_core_uncertain_rate_delta: float = 0.02,
    min_core_after_first_sleeping_rate: float = 0.97,
    max_core_after_first_sleeping_rate_drop: float = 0.01,
    min_start_hit_rate: float = 0.0,
    min_wake_hit_rate: float = 0.0,
    min_both_hit_rate: float = 0.0,
    max_sleep_core_awake_rate: float = 0.10,
    max_sleep_core_awake_rate_delta: float = 0.005,
    require_awake_le_uncertain_in_sleep_core: bool = True,
) -> dict:
    labels = sleep_model.load_labels()
    windows = _parse_windows(labels)
    if not windows:
        return {
            "accept": True,
            "reason": "skip:no_valid_windows",
        }

    end_d: date = max(e.date() for _, e in windows)
    # 1. eval_days <= 0 时，直接使用全部已标注睡眠数据做门禁。
    # 2. 否则仍保留按最近 N 天回放的兼容行为。
    if int(eval_days) <= 0:
        start_d = min(s.date() for s, _ in windows)
    else:
        days = max(1, int(eval_days))
        start_d = end_d - timedelta(days=days - 1)
    entries = eval_replay.load_entries(start_d, end_d)
    if not entries:
        return {
            "accept": True,
            "reason": "skip:no_eval_entries",
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
        }

    baseline = joblib.load(Path(baseline_model_path))
    candidate = joblib.load(Path(candidate_model_path))
    m_old = _eval_one_model(
        baseline,
        entries=entries,
        labels=labels,
        windows=windows,
        buffer_min=buffer_min,
        edge_buffer_min=edge_buffer_min,
    )
    m_new = _eval_one_model(
        candidate,
        entries=entries,
        labels=labels,
        windows=windows,
        buffer_min=buffer_min,
        edge_buffer_min=edge_buffer_min,
    )

    old_starts, old_ends = _edge_events(m_old["pred_points"])
    new_starts, new_ends = _edge_events(m_new["pred_points"])
    evt_old = _event_window_metrics(
        windows,
        old_starts,
        old_ends,
        start_early_min=start_early_min,
        start_late_min=start_late_min,
        wake_early_min=wake_early_min,
        wake_late_min=wake_late_min,
    )
    evt_new = _event_window_metrics(
        windows,
        new_starts,
        new_ends,
        start_early_min=start_early_min,
        start_late_min=start_late_min,
        wake_early_min=wake_early_min,
        wake_late_min=wake_late_min,
    )

    old_rate = float(m_old["awake_core_to_sleep_rate"])
    new_rate = float(m_new["awake_core_to_sleep_rate"])
    delta = new_rate - old_rate

    reasons: list[str] = []
    if new_rate > float(max_awake_core_sleep_rate):
        reasons.append(
            "new_awake_core_to_sleep_rate_exceeds_abs_limit"
            f"({new_rate:.4f}>{float(max_awake_core_sleep_rate):.4f})"
        )
    if delta > float(max_awake_core_sleep_rate_delta):
        reasons.append(
            "new_awake_core_to_sleep_rate_regressed"
            f"(delta={delta:.4f}>{float(max_awake_core_sleep_rate_delta):.4f})"
        )
    new_awake_far_sleep_rate = float(m_new["awake_far_to_sleep_rate"])
    old_awake_far_sleep_rate = float(m_old["awake_far_to_sleep_rate"])
    awake_far_sleep_delta = new_awake_far_sleep_rate - old_awake_far_sleep_rate
    if new_awake_far_sleep_rate > float(max_awake_far_sleep_rate):
        reasons.append(
            "new_awake_far_to_sleep_rate_exceeds_abs_limit"
            f"({new_awake_far_sleep_rate:.4f}>{float(max_awake_far_sleep_rate):.4f})"
        )
    if awake_far_sleep_delta > float(max_awake_far_sleep_rate_delta):
        reasons.append(
            "new_awake_far_to_sleep_rate_regressed"
            f"(delta={awake_far_sleep_delta:.4f}>{float(max_awake_far_sleep_rate_delta):.4f})"
        )
    new_sleep_core_uncertain_rate = float(m_new["sleep_core_to_uncertain_rate"])
    old_sleep_core_uncertain_rate = float(m_old["sleep_core_to_uncertain_rate"])
    sleep_core_uncertain_delta = (
        new_sleep_core_uncertain_rate - old_sleep_core_uncertain_rate
    )
    if new_sleep_core_uncertain_rate > float(max_sleep_core_uncertain_rate):
        reasons.append(
            "new_sleep_core_to_uncertain_rate_exceeds_abs_limit"
            f"({new_sleep_core_uncertain_rate:.4f}>{float(max_sleep_core_uncertain_rate):.4f})"
        )
    if sleep_core_uncertain_delta > float(max_sleep_core_uncertain_rate_delta):
        reasons.append(
            "new_sleep_core_to_uncertain_rate_regressed"
            f"(delta={sleep_core_uncertain_delta:.4f}>{float(max_sleep_core_uncertain_rate_delta):.4f})"
        )
    new_core_after_first_sleeping_rate = float(m_new["core_after_first_sleeping_rate"])
    old_core_after_first_sleeping_rate = float(m_old["core_after_first_sleeping_rate"])
    core_after_first_drop = old_core_after_first_sleeping_rate - new_core_after_first_sleeping_rate
    if new_core_after_first_sleeping_rate < float(min_core_after_first_sleeping_rate):
        reasons.append(
            "new_core_after_first_sleeping_rate_below_abs_floor"
            f"({new_core_after_first_sleeping_rate:.4f}<{float(min_core_after_first_sleeping_rate):.4f})"
        )
    if core_after_first_drop > float(max_core_after_first_sleeping_rate_drop):
        reasons.append(
            "new_core_after_first_sleeping_rate_regressed"
            f"(drop={core_after_first_drop:.4f}>{float(max_core_after_first_sleeping_rate_drop):.4f})"
        )

    new_sleep_core_awake_rate = float(m_new["sleep_core_to_awake_rate"])
    old_sleep_core_awake_rate = float(m_old["sleep_core_to_awake_rate"])
    sleep_core_awake_delta = new_sleep_core_awake_rate - old_sleep_core_awake_rate
    if new_sleep_core_awake_rate > float(max_sleep_core_awake_rate):
        reasons.append(
            "new_sleep_core_to_awake_rate_exceeds_abs_limit"
            f"({new_sleep_core_awake_rate:.4f}>{float(max_sleep_core_awake_rate):.4f})"
        )
    if sleep_core_awake_delta > float(max_sleep_core_awake_rate_delta):
        reasons.append(
            "new_sleep_core_to_awake_rate_regressed"
            f"(delta={sleep_core_awake_delta:.4f}>{float(max_sleep_core_awake_rate_delta):.4f})"
        )
    if int(evt_new["wake_late_over_60min_count"]) > int(max_wake_late_over_60min_count):
        reasons.append(
            "new_wake_late_over_60min_count_exceeds_limit"
            f"({int(evt_new['wake_late_over_60min_count'])}>{int(max_wake_late_over_60min_count)})"
        )
    if float(evt_new["start_hit_rate"]) < max(
        float(min_start_hit_rate), float(evt_old["start_hit_rate"])
    ):
        reasons.append(
            "new_start_hit_rate_below_floor_or_baseline"
            f"({float(evt_new['start_hit_rate']):.4f}<max({float(min_start_hit_rate):.4f},{float(evt_old['start_hit_rate']):.4f}))"
        )
    if float(evt_new["wake_hit_rate"]) < max(
        float(min_wake_hit_rate), float(evt_old["wake_hit_rate"]) - 0.03
    ):
        reasons.append(
            "new_wake_hit_rate_below_floor_or_too_much_regression"
            f"({float(evt_new['wake_hit_rate']):.4f}<max({float(min_wake_hit_rate):.4f},{float(evt_old['wake_hit_rate']) - 0.03:.4f}))"
        )
    if float(evt_new["both_hit_rate"]) < max(
        float(min_both_hit_rate), float(evt_old["both_hit_rate"])
    ):
        reasons.append(
            "new_both_hit_rate_below_floor_or_baseline"
            f"({float(evt_new['both_hit_rate']):.4f}<max({float(min_both_hit_rate):.4f},{float(evt_old['both_hit_rate']):.4f}))"
        )
    if require_awake_le_uncertain_in_sleep_core and (
        new_sleep_core_awake_rate > new_sleep_core_uncertain_rate
    ):
        reasons.append(
            "sleep_core_awake_rate_should_not_exceed_uncertain_rate"
            f"({new_sleep_core_awake_rate:.4f}>{new_sleep_core_uncertain_rate:.4f})"
        )

    # 近期窗口二次验收，避免“整体通过但最近严重退化”
    recent_days = max(1, int(recent_eval_days))
    recent_start_d = end_d - timedelta(days=recent_days - 1)
    recent_entries = eval_replay.load_entries(recent_start_d, end_d)
    if recent_entries:
        m_old_recent = _eval_one_model(
            baseline,
            entries=recent_entries,
            labels=labels,
            windows=windows,
            buffer_min=buffer_min,
            edge_buffer_min=edge_buffer_min,
        )
        m_new_recent = _eval_one_model(
            candidate,
            entries=recent_entries,
            labels=labels,
            windows=windows,
            buffer_min=buffer_min,
            edge_buffer_min=edge_buffer_min,
        )
        old_recent_starts, old_recent_ends = _edge_events(m_old_recent["pred_points"])
        new_recent_starts, new_recent_ends = _edge_events(m_new_recent["pred_points"])
        evt_old_recent = _event_window_metrics(
            windows,
            old_recent_starts,
            old_recent_ends,
            start_early_min=start_early_min,
            start_late_min=start_late_min,
            wake_early_min=wake_early_min,
            wake_late_min=wake_late_min,
        )
        evt_new_recent = _event_window_metrics(
            windows,
            new_recent_starts,
            new_recent_ends,
            start_early_min=start_early_min,
            start_late_min=start_late_min,
            wake_early_min=wake_early_min,
            wake_late_min=wake_late_min,
        )
        if float(evt_new_recent["both_hit_rate"]) < float(evt_old_recent["both_hit_rate"]):
            reasons.append(
                "recent_both_hit_rate_regressed"
                f"({float(evt_new_recent['both_hit_rate']):.4f}<{float(evt_old_recent['both_hit_rate']):.4f})"
            )
        if int(evt_new_recent["wake_late_over_60min_count"]) > int(max_wake_late_over_60min_count):
            reasons.append(
                "recent_wake_late_over_60min_count_exceeds_limit"
                f"({int(evt_new_recent['wake_late_over_60min_count'])}>{int(max_wake_late_over_60min_count)})"
            )
    else:
        m_old_recent = None
        m_new_recent = None
        evt_old_recent = None
        evt_new_recent = None

    # 删除大对象，避免日志过大
    m_old.pop("pred_points", None)
    m_new.pop("pred_points", None)
    if m_old_recent is not None:
        m_old_recent.pop("pred_points", None)
    if m_new_recent is not None:
        m_new_recent.pop("pred_points", None)

    return {
        "accept": len(reasons) == 0,
        "start_date": start_d.isoformat(),
        "end_date": end_d.isoformat(),
        "buffer_min": int(buffer_min),
        "edge_buffer_min": int(edge_buffer_min),
        "baseline": m_old,
        "candidate": m_new,
        "constraints": {
            "recent_eval_days": recent_days,
            "start_window": [int(start_early_min), int(start_late_min)],
            "wake_window": [int(wake_early_min), int(wake_late_min)],
            "max_wake_late_over_60min_count": int(max_wake_late_over_60min_count),
            "min_start_hit_rate": float(min_start_hit_rate),
            "min_wake_hit_rate": float(min_wake_hit_rate),
            "min_both_hit_rate": float(min_both_hit_rate),
            "max_sleep_core_awake_rate": float(max_sleep_core_awake_rate),
            "max_sleep_core_awake_rate_delta": float(max_sleep_core_awake_rate_delta),
            "require_awake_le_uncertain_in_sleep_core": bool(
                require_awake_le_uncertain_in_sleep_core
            ),
            "max_awake_core_sleep_rate": float(max_awake_core_sleep_rate),
            "max_awake_core_sleep_rate_delta": float(max_awake_core_sleep_rate_delta),
            "max_awake_far_sleep_rate": float(max_awake_far_sleep_rate),
            "max_awake_far_sleep_rate_delta": float(
                max_awake_far_sleep_rate_delta
            ),
            "max_sleep_core_uncertain_rate": float(max_sleep_core_uncertain_rate),
            "max_sleep_core_uncertain_rate_delta": float(
                max_sleep_core_uncertain_rate_delta
            ),
            "min_core_after_first_sleeping_rate": float(
                min_core_after_first_sleeping_rate
            ),
            "max_core_after_first_sleeping_rate_drop": float(
                max_core_after_first_sleeping_rate_drop
            ),
        },
        "delta": {
            "awake_core_to_sleep_rate": delta,
            "awake_far_to_sleep_rate": awake_far_sleep_delta,
            "sleep_to_awake_rate": float(m_new["sleep_to_awake_rate"])
            - float(m_old["sleep_to_awake_rate"]),
            "sleep_core_to_awake_rate": sleep_core_awake_delta,
            "sleep_core_to_uncertain_rate": sleep_core_uncertain_delta,
            "sleep_to_uncertain_rate": float(m_new["sleep_to_uncertain_rate"])
            - float(m_old["sleep_to_uncertain_rate"]),
            "core_after_first_sleeping_rate": (
                new_core_after_first_sleeping_rate - old_core_after_first_sleeping_rate
            ),
        },
        "event_metrics": {"baseline": evt_old, "candidate": evt_new},
        "recent": {
            "start_date": recent_start_d.isoformat() if recent_entries else None,
            "end_date": end_d.isoformat() if recent_entries else None,
            "baseline": m_old_recent,
            "candidate": m_new_recent,
            "event_metrics": {
                "baseline": evt_old_recent,
                "candidate": evt_new_recent,
            },
        },
        "reasons": reasons,
    }


def dumps_compact(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
