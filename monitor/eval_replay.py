#!/usr/bin/env python3
"""
Replay-evaluate sleep states on historical polling logs with current model.

Outputs:
1) Timeline CSV (per poll)
2) Hourly summary CSV
3) Text summary report
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

import sleep_model
from paths import DATA_DIR

LOG_FILE = DATA_DIR / "sleep_log.jsonl"
LABELS_FILE = DATA_DIR / "sleep_labels.json"

SLEEP_ENTER_THRESHOLD = 0.75
SLEEP_EXIT_THRESHOLD = 0.35
SLEEP_ENTER_HR_GATE_MAX = 85.0
SLEEP_ENTER_OVERRIDE_PROB = 0.88
SLEEP_ENTER_CONFIRM_POLLS = 2
SLEEP_EXIT_CONFIRM_POLLS = 2
SLEEP_ENTER_MIN_CONFIRM_MINUTES = 10
SLEEP_STALE_LAG_GUARD_MIN = 8
SLEEP_WAKE_CONFIRM_POLLS_STRICT = 3
SLEEP_WAKE_HR_MIN = 82.0
SLEEP_WAKE_TREND_MIN = 0.30
SLEEP_WAKE_ZERO_STEPS_MAX = 10
SLEEP_WAKE_SUSTAINED_ZERO_MAX = 3
SLEEP_MID_BAND_STICKY_SLEEP = True
SLEEP_MID_BAND_STICKY_MIN_PROB = 0.65
SLEEP_AGGRESSIVE_BIAS_ENABLED = True
SLEEP_AGGRESSIVE_MIN_PROB = 0.50
SLEEP_AGGRESSIVE_ZERO_STEPS_MIN = 18
SLEEP_AGGRESSIVE_SUSTAINED_ZERO_MIN = 40
SLEEP_AGGRESSIVE_HR_MAX = 88.0
SLEEP_AGGRESSIVE_MAX_LAG_MIN = 8
DEFAULT_START_EARLY_MIN = -30
DEFAULT_START_LATE_MIN = 15
DEFAULT_WAKE_EARLY_MIN = -15
DEFAULT_WAKE_LATE_MIN = 30


@dataclass
class StateMachine:
    prev_state: str = "unknown"
    viterbi: sleep_model.OnlineViterbiState | None = None

    def next_state(
        self, prob: float, raw_reason: str, signals: dict
    ) -> tuple[str, str]:
        if self.viterbi is None:
            self.viterbi = sleep_model.OnlineViterbiState()
        state, reason = self.viterbi.step(float(prob), signals.get("evidence_id"))
        self.prev_state = state
        return state, reason


def parse_poll_dt(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def raw_state_from_prob(prob: float, signals: dict) -> tuple[str, str]:
    hr_avg = signals.get("hr_avg")
    if prob >= SLEEP_ENTER_THRESHOLD:
        return (
            "sleeping",
            f"高概率睡眠（{prob:.0%}，心率 {hr_avg} bpm，静止 {signals.get('zero_steps_count', 0)}/20）",
        )
    if prob <= SLEEP_EXIT_THRESHOLD:
        if hr_avg is not None:
            return "awake", f"高概率清醒（{prob:.0%}，心率 {hr_avg} bpm）"
        return "awake", f"高概率清醒（{prob:.0%}）"
    return "uncertain", f"状态不确定（{prob:.0%}）"


def load_entries(start_d: date, end_d: date) -> list[dict]:
    all_rows: list[dict] = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        dt = parse_poll_dt(str(obj.get("poll_time", "")))
        if dt is None:
            continue
        obj["_poll_dt"] = dt
        all_rows.append(obj)
    all_rows.sort(key=lambda x: x["_poll_dt"])

    # 截取目标日期范围
    rows = [obj for obj in all_rows if start_d <= obj["_poll_dt"].date() <= end_d]
    return rows


def to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _rate(n: int, d: int) -> float:
    if d <= 0:
        return 0.0
    return n / d


def load_label_windows(labels: list[dict]) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    for w in labels:
        start = parse_poll_dt(str(w.get("start", "")))
        end = parse_poll_dt(str(w.get("end", "")))
        if start is None or end is None or end <= start:
            continue
        windows.append((start, end))
    windows.sort(key=lambda x: x[0])
    return windows


def build_state_edges(
    timeline_rows: list[dict],
) -> tuple[list[datetime], list[datetime], list[tuple[datetime, datetime]]]:
    sleep_starts: list[datetime] = []
    wake_starts: list[datetime] = []
    sleep_windows: list[tuple[datetime, datetime]] = []
    in_sleep = False
    curr_start: datetime | None = None

    for row in timeline_rows:
        dt = parse_poll_dt(str(row.get("poll_time", "")))
        if dt is None:
            continue
        state = str(row.get("new_state", "unknown"))
        if state == "sleeping" and not in_sleep:
            in_sleep = True
            curr_start = dt
            sleep_starts.append(dt)
        elif state != "sleeping" and in_sleep:
            in_sleep = False
            wake_starts.append(dt)
            if curr_start is not None and dt > curr_start:
                sleep_windows.append((curr_start, dt))
            curr_start = None

    if in_sleep and curr_start is not None:
        end_dt = parse_poll_dt(str(timeline_rows[-1].get("poll_time", "")))
        if end_dt is not None and end_dt > curr_start:
            sleep_windows.append((curr_start, end_dt))
    return sleep_starts, wake_starts, sleep_windows


def _nearest_in_window(
    points: list[datetime], target: datetime, left_min: int, right_min: int
) -> datetime | None:
    left = target + timedelta(minutes=left_min)
    right = target + timedelta(minutes=right_min)
    cands = [t for t in points if left <= t <= right]
    if not cands:
        return None
    return min(cands, key=lambda x: abs((x - target).total_seconds()))


def compute_event_window_metrics(
    label_windows: list[tuple[datetime, datetime]],
    sleep_starts: list[datetime],
    wake_starts: list[datetime],
    start_early_min: int,
    start_late_min: int,
    wake_early_min: int,
    wake_late_min: int,
) -> dict[str, float | int]:
    start_hits = 0
    wake_hits = 0
    both_hits = 0
    start_errs: list[float] = []
    wake_errs: list[float] = []

    for start, end in label_windows:
        pred_start = _nearest_in_window(sleep_starts, start, -180, 360)
        pred_wake = _nearest_in_window(wake_starts, end, -360, 360)
        if pred_start is not None:
            err = (pred_start - start).total_seconds() / 60.0
            start_errs.append(err)
            if start_early_min <= err <= start_late_min:
                start_hits += 1
        if pred_wake is not None:
            err = (pred_wake - end).total_seconds() / 60.0
            wake_errs.append(err)
            if wake_early_min <= err <= wake_late_min:
                wake_hits += 1
        if pred_start is not None and pred_wake is not None:
            s_ok = start_early_min <= (pred_start - start).total_seconds() / 60.0 <= start_late_min
            w_ok = wake_early_min <= (pred_wake - end).total_seconds() / 60.0 <= wake_late_min
            if s_ok and w_ok:
                both_hits += 1

    total = len(label_windows)
    return {
        "event_total": total,
        "start_hit": start_hits,
        "wake_hit": wake_hits,
        "both_hit": both_hits,
        "start_hit_rate": _rate(start_hits, total),
        "wake_hit_rate": _rate(wake_hits, total),
        "both_hit_rate": _rate(both_hits, total),
        "start_err_median_min": (median(start_errs) if start_errs else 0.0),
        "wake_err_median_min": (median(wake_errs) if wake_errs else 0.0),
    }


def compute_gate_metrics(timeline_rows: list[dict]) -> dict[str, float | int]:
    hi_false_send = 0
    hi_false_block = 0
    normal_false_send = 0
    normal_false_block = 0
    total = 0
    truth_sleep_total = 0
    truth_awake_total = 0

    for row in timeline_rows:
        truth = str(row.get("truth_state", "awake"))
        pred = str(row.get("new_state", "unknown"))
        if truth not in ("sleeping", "awake"):
            continue
        total += 1
        if truth == "sleeping":
            truth_sleep_total += 1
        else:
            truth_awake_total += 1

        # 1. 高优先消息：awake/uncertain 允许发送，sleeping 阻断。
        hi_send = pred != "sleeping"
        # 2. 普通消息：仅 awake 允许发送，uncertain/sleeping 均阻断。
        normal_send = pred == "awake"

        if truth == "sleeping":
            hi_false_send += int(hi_send)
            normal_false_send += int(normal_send)
        else:
            hi_false_block += int(not hi_send)
            normal_false_block += int(not normal_send)

    return {
        "gate_samples": total,
        "truth_sleep_samples": truth_sleep_total,
        "truth_awake_samples": truth_awake_total,
        "hi_false_send": hi_false_send,
        "hi_false_block": hi_false_block,
        "hi_false_send_rate_on_sleep": _rate(hi_false_send, truth_sleep_total),
        "hi_false_block_rate_on_awake": _rate(hi_false_block, truth_awake_total),
        "normal_false_send": normal_false_send,
        "normal_false_block": normal_false_block,
        "normal_false_send_rate_on_sleep": _rate(normal_false_send, truth_sleep_total),
        "normal_false_block_rate_on_awake": _rate(normal_false_block, truth_awake_total),
    }


def main() -> int:
    global SLEEP_ENTER_HR_GATE_MAX
    global SLEEP_ENTER_OVERRIDE_PROB
    global SLEEP_ENTER_MIN_CONFIRM_MINUTES
    global SLEEP_AGGRESSIVE_MIN_PROB
    global SLEEP_AGGRESSIVE_SUSTAINED_ZERO_MIN
    global SLEEP_AGGRESSIVE_HR_MAX

    p = argparse.ArgumentParser(description="Replay evaluate Fitbit sleep states.")
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD, default=yesterday")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD, default=today")
    p.add_argument("--start-early-min", type=int, default=DEFAULT_START_EARLY_MIN)
    p.add_argument("--start-late-min", type=int, default=DEFAULT_START_LATE_MIN)
    p.add_argument("--wake-early-min", type=int, default=DEFAULT_WAKE_EARLY_MIN)
    p.add_argument("--wake-late-min", type=int, default=DEFAULT_WAKE_LATE_MIN)
    p.add_argument(
        "--sleep-enter-hr-gate-max",
        type=float,
        default=SLEEP_ENTER_HR_GATE_MAX,
    )
    p.add_argument(
        "--sleep-enter-override-prob",
        type=float,
        default=SLEEP_ENTER_OVERRIDE_PROB,
    )
    p.add_argument(
        "--sleep-enter-min-confirm-minutes",
        type=int,
        default=SLEEP_ENTER_MIN_CONFIRM_MINUTES,
    )
    p.add_argument(
        "--sleep-aggressive-min-prob",
        type=float,
        default=SLEEP_AGGRESSIVE_MIN_PROB,
    )
    p.add_argument(
        "--sleep-aggressive-sustained-zero-min",
        type=int,
        default=SLEEP_AGGRESSIVE_SUSTAINED_ZERO_MIN,
    )
    p.add_argument(
        "--sleep-aggressive-hr-max",
        type=float,
        default=SLEEP_AGGRESSIVE_HR_MAX,
    )
    p.add_argument(
        "--out-dir", default="logs/fitbit-proactive", help="Output directory"
    )
    args = p.parse_args()

    today = date.today()
    start_d = (
        date.fromisoformat(args.start_date)
        if args.start_date
        else (today - timedelta(days=1))
    )
    end_d = date.fromisoformat(args.end_date) if args.end_date else today
    if start_d > end_d:
        raise ValueError("start-date cannot be after end-date")

    SLEEP_ENTER_HR_GATE_MAX = float(args.sleep_enter_hr_gate_max)
    SLEEP_ENTER_OVERRIDE_PROB = float(args.sleep_enter_override_prob)
    SLEEP_ENTER_MIN_CONFIRM_MINUTES = int(args.sleep_enter_min_confirm_minutes)
    SLEEP_AGGRESSIVE_MIN_PROB = float(args.sleep_aggressive_min_prob)
    SLEEP_AGGRESSIVE_SUSTAINED_ZERO_MIN = int(args.sleep_aggressive_sustained_zero_min)
    SLEEP_AGGRESSIVE_HR_MAX = float(args.sleep_aggressive_hr_max)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    model = sleep_model.load_model()
    if model is None:
        raise RuntimeError("sleep_model.pkl unavailable or failed to load")

    labels = []
    if LABELS_FILE.exists():
        labels = json.loads(LABELS_FILE.read_text(encoding="utf-8"))

    entries = load_entries(start_d, end_d)
    if not entries:
        raise RuntimeError("No entries in requested date range")

    state_machine = StateMachine()

    timeline_rows: list[dict] = []
    pred_counts = defaultdict(int)
    truth_counts = defaultdict(int)
    strict_ok = 0
    old_strict_ok = 0
    covered = 0
    covered_ok = 0
    changed = 0

    # pred x truth where truth in {sleeping, awake}
    conf_pred_truth = {
        ("sleeping", "sleeping"): 0,
        ("sleeping", "awake"): 0,
        ("awake", "sleeping"): 0,
        ("awake", "awake"): 0,
        ("uncertain", "sleeping"): 0,
        ("uncertain", "awake"): 0,
    }

    for e in entries:
        poll_time = str(e.get("poll_time", ""))
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
        raw_state, raw_reason = raw_state_from_prob(prob, signals)
        new_state, new_reason = state_machine.next_state(prob, raw_reason, signals)

        truth_sleep = sleep_model.is_sleeping(poll_time, labels)
        truth_state = "sleeping" if truth_sleep else "awake"
        old_state = str(e.get("state", "unknown"))
        old_binary_state = (
            old_state if old_state in ("sleeping", "awake") else "uncertain"
        )

        pred_counts[new_state] += 1
        truth_counts[truth_state] += 1
        conf_pred_truth[(new_state, truth_state)] += 1

        is_strict_ok = int(new_state == truth_state)
        strict_ok += is_strict_ok

        is_old_strict_ok = int(old_binary_state == truth_state)
        old_strict_ok += is_old_strict_ok

        if new_state in ("sleeping", "awake"):
            covered += 1
            covered_ok += int(new_state == truth_state)

        is_changed = int(old_state != new_state)
        changed += is_changed

        timeline_rows.append(
            {
                "poll_time": poll_time,
                "hour_bucket": e["_poll_dt"].strftime("%Y-%m-%d %H:00"),
                "truth_state": truth_state,
                "old_state": old_state,
                "new_state": new_state,
                "sleep_prob_new": round(prob, 4),
                "data_lag_min": lag if lag is not None else "",
                "hr_avg": to_float(signals.get("hr_avg")),
                "hr_range": to_float(signals.get("hr_range")),
                "zero_steps_count": signals.get("zero_steps_count"),
                "sustained_zero_min": signals.get("sustained_zero_min"),
                "strict_correct": is_strict_ok,
                "covered_binary": int(new_state in ("sleeping", "awake")),
                "covered_binary_correct": int(
                    new_state in ("sleeping", "awake") and new_state == truth_state
                ),
                "state_changed_vs_old": is_changed,
                "new_reason": new_reason,
            }
        )

    total = len(timeline_rows)
    strict_acc = strict_ok / total if total else 0.0
    old_strict_acc = old_strict_ok / total if total else 0.0
    coverage = covered / total if total else 0.0
    covered_acc = covered_ok / covered if covered else 0.0
    changed_ratio = changed / total if total else 0.0
    label_windows = load_label_windows(labels)
    sleep_starts, wake_starts, _ = build_state_edges(timeline_rows)
    event_metrics = compute_event_window_metrics(
        label_windows=label_windows,
        sleep_starts=sleep_starts,
        wake_starts=wake_starts,
        start_early_min=args.start_early_min,
        start_late_min=args.start_late_min,
        wake_early_min=args.wake_early_min,
        wake_late_min=args.wake_late_min,
    )
    gate_metrics = compute_gate_metrics(timeline_rows)

    stem = f"reeval_{start_d.isoformat()}_{end_d.isoformat()}"
    timeline_csv = out_dir / f"{stem}.timeline.csv"
    hourly_csv = out_dir / f"{stem}.hourly.csv"
    summary_txt = out_dir / f"{stem}.summary.txt"

    # timeline
    fieldnames = list(timeline_rows[0].keys())
    with timeline_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(timeline_rows)

    # hourly
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in timeline_rows:
        buckets[r["hour_bucket"]].append(r)
    hourly_rows = []
    for k in sorted(buckets.keys()):
        rows = buckets[k]
        n = len(rows)
        n_sleep = sum(1 for r in rows if r["new_state"] == "sleeping")
        n_awake = sum(1 for r in rows if r["new_state"] == "awake")
        n_uncertain = sum(1 for r in rows if r["new_state"] == "uncertain")
        n_ok = sum(int(r["strict_correct"]) for r in rows)
        n_cov = sum(int(r["covered_binary"]) for r in rows)
        n_cov_ok = sum(int(r["covered_binary_correct"]) for r in rows)
        hourly_rows.append(
            {
                "hour_bucket": k,
                "samples": n,
                "pred_sleeping": n_sleep,
                "pred_awake": n_awake,
                "pred_uncertain": n_uncertain,
                "strict_acc": round(n_ok / n, 4) if n else 0.0,
                "covered_ratio": round(n_cov / n, 4) if n else 0.0,
                "covered_acc": round(n_cov_ok / n_cov, 4) if n_cov else 0.0,
            }
        )
    with hourly_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(hourly_rows[0].keys()))
        w.writeheader()
        w.writerows(hourly_rows)

    # summary
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("Fitbit Sleep Replay Evaluation\n")
        f.write(f"generated_at: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"date_range: {start_d} ~ {end_d}\n")
        f.write(f"samples: {total}\n")
        f.write(
            f"truth_counts: sleeping={truth_counts['sleeping']} awake={truth_counts['awake']}\n"
        )
        f.write(
            "pred_counts: "
            f"sleeping={pred_counts['sleeping']} "
            f"awake={pred_counts['awake']} "
            f"uncertain={pred_counts['uncertain']}\n"
        )
        f.write("\nAccuracy Metrics\n")
        f.write(f"strict_3state_acc: {strict_acc:.4f}\n")
        f.write(f"covered_ratio(pred!=uncertain): {coverage:.4f}\n")
        f.write(f"covered_binary_acc: {covered_acc:.4f}\n")
        f.write(f"old_state_strict_acc: {old_strict_acc:.4f}\n")
        f.write(f"delta_vs_old: {(strict_acc - old_strict_acc):+.4f}\n")
        f.write(f"changed_vs_old: {changed} ({changed_ratio:.2%})\n")
        f.write("\nEvent Window Metrics\n")
        f.write(
            f"start_window: [{args.start_early_min}, {args.start_late_min}] min\n"
        )
        f.write(f"wake_window: [{args.wake_early_min}, {args.wake_late_min}] min\n")
        f.write(f"event_total: {event_metrics['event_total']}\n")
        f.write(
            f"start_hit: {event_metrics['start_hit']} ({event_metrics['start_hit_rate']:.4f})\n"
        )
        f.write(
            f"wake_hit: {event_metrics['wake_hit']} ({event_metrics['wake_hit_rate']:.4f})\n"
        )
        f.write(
            f"both_hit: {event_metrics['both_hit']} ({event_metrics['both_hit_rate']:.4f})\n"
        )
        f.write(
            f"start_err_median_min: {float(event_metrics['start_err_median_min']):.2f}\n"
        )
        f.write(
            f"wake_err_median_min: {float(event_metrics['wake_err_median_min']):.2f}\n"
        )
        f.write("\nMessage Gate Metrics\n")
        f.write(f"gate_samples: {gate_metrics['gate_samples']}\n")
        f.write(f"truth_sleep_samples: {gate_metrics['truth_sleep_samples']}\n")
        f.write(f"truth_awake_samples: {gate_metrics['truth_awake_samples']}\n")
        f.write(
            f"hi_false_send: {gate_metrics['hi_false_send']} ({gate_metrics['hi_false_send_rate_on_sleep']:.4f})\n"
        )
        f.write(
            f"hi_false_block: {gate_metrics['hi_false_block']} ({gate_metrics['hi_false_block_rate_on_awake']:.4f})\n"
        )
        f.write(
            f"normal_false_send: {gate_metrics['normal_false_send']} ({gate_metrics['normal_false_send_rate_on_sleep']:.4f})\n"
        )
        f.write(
            f"normal_false_block: {gate_metrics['normal_false_block']} ({gate_metrics['normal_false_block_rate_on_awake']:.4f})\n"
        )
        f.write("\nPred x Truth\n")
        for key in (
            ("sleeping", "sleeping"),
            ("sleeping", "awake"),
            ("awake", "sleeping"),
            ("awake", "awake"),
            ("uncertain", "sleeping"),
            ("uncertain", "awake"),
        ):
            f.write(f"{key[0]}|{key[1]}: {conf_pred_truth[key]}\n")
        f.write("\nOutputs\n")
        f.write(f"timeline_csv: {timeline_csv}\n")
        f.write(f"hourly_csv: {hourly_csv}\n")

    print(f"[ok] summary: {summary_txt}")
    print(f"[ok] timeline: {timeline_csv}")
    print(f"[ok] hourly: {hourly_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
