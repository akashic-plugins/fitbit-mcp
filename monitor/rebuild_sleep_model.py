#!/usr/bin/env python3
"""
Rebuild sleep model from backup data with cleaning + sample weighting.

Default strategy:
- Keep only low-lag samples (data_lag_min <= 8)
- Drop uncertain/unknown state rows
- Positive samples: inside labeled sleep windows, excluding +/-20 min edges
- Negative samples: outside sleep windows, at least 45 min away from any window edge
- Old backup samples use lower weight (default 0.3), current samples use higher weight (default 1.0)
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np

try:
    import joblib
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
except Exception as e:
    raise SystemExit(f"sklearn/joblib not available: {e}")

import sleep_model


def parse_dt(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Sample:
    x: list[float]
    y: int
    w: float
    source: str
    ts: datetime
    lag: int | None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except json.JSONDecodeError:
            continue
    return out


def load_labels(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    except Exception:
        pass
    return []


def parse_windows(labels: list[dict]) -> list[tuple[datetime, datetime]]:
    wins: list[tuple[datetime, datetime]] = []
    for w in labels:
        s = parse_dt(str(w.get("start", "")))
        e = parse_dt(str(w.get("end", "")))
        if s is None or e is None or e <= s:
            continue
        wins.append((s, e))
    wins.sort(key=lambda x: x[0])
    return wins


def in_window(
    ts: datetime, windows: Iterable[tuple[datetime, datetime]]
) -> tuple[bool, int | None]:
    """
    Returns:
    - inside any window?
    - distance to nearest window edge in minutes (None if no windows)
    """
    nearest: float | None = None
    inside = False
    for s, e in windows:
        if s <= ts <= e:
            inside = True
            d = min((ts - s).total_seconds(), (e - ts).total_seconds()) / 60.0
            nearest = d if nearest is None else min(nearest, d)
        else:
            if ts < s:
                d = (s - ts).total_seconds() / 60.0
            else:
                d = (ts - e).total_seconds() / 60.0
            nearest = d if nearest is None else min(nearest, d)
    return inside, (int(nearest) if nearest is not None else None)


def build_samples(
    *,
    entries: list[dict],
    windows: list[tuple[datetime, datetime]],
    source: str,
    source_weight: float,
    max_lag_min: int,
    pos_edge_exclude_min: int,
    neg_edge_margin_min: int,
) -> tuple[list[Sample], dict[str, int]]:
    stats = {
        "rows": len(entries),
        "kept": 0,
        "drop_no_time": 0,
        "drop_no_feat": 0,
        "drop_lag": 0,
        "drop_uncertain": 0,
        "drop_pos_edge": 0,
        "drop_neg_near_edge": 0,
        "pos": 0,
        "neg": 0,
    }
    out: list[Sample] = []
    for e in entries:
        ts = parse_dt(str(e.get("poll_time", "")))
        if ts is None:
            stats["drop_no_time"] += 1
            continue

        lag_raw = e.get("data_lag_min")
        lag = int(lag_raw) if isinstance(lag_raw, (int, float)) else None
        if lag is not None and lag > max_lag_min:
            stats["drop_lag"] += 1
            continue

        state = str(e.get("state", "")).lower()
        if state in {"uncertain", "unknown"}:
            stats["drop_uncertain"] += 1
            continue

        feat = sleep_model.extract_features(e)
        if feat is None:
            stats["drop_no_feat"] += 1
            continue

        inside, near_min = in_window(ts, windows)
        if inside:
            if near_min is not None and near_min < pos_edge_exclude_min:
                stats["drop_pos_edge"] += 1
                continue
            y = 1
            stats["pos"] += 1
        else:
            if near_min is not None and near_min < neg_edge_margin_min:
                stats["drop_neg_near_edge"] += 1
                continue
            y = 0
            stats["neg"] += 1

        out.append(Sample(x=feat, y=y, w=source_weight, source=source, ts=ts, lag=lag))
        stats["kept"] += 1
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild sleep model with cleaned weighted data."
    )
    ap.add_argument(
        "--backup-dir",
        required=True,
        help="Path to backup dir with sleep_log.jsonl and sleep_labels.json",
    )
    ap.add_argument(
        "--current-dir",
        default="scripts/fitbit-monitor",
        help="Current fitbit-monitor dir",
    )
    ap.add_argument("--old-weight", type=float, default=0.3)
    ap.add_argument("--new-weight", type=float, default=1.0)
    ap.add_argument("--max-lag-min", type=int, default=8)
    ap.add_argument("--pos-edge-exclude-min", type=int, default=20)
    ap.add_argument("--neg-edge-margin-min", type=int, default=45)
    ap.add_argument("--out-model", default="scripts/fitbit-monitor/sleep_model.pkl")
    ap.add_argument("--report-out", default="", help="Optional report path")
    args = ap.parse_args()

    backup_dir = Path(args.backup_dir).resolve()
    current_dir = Path(args.current_dir).resolve()
    out_model = Path(args.out_model).resolve()

    backup_log = backup_dir / "sleep_log.jsonl"
    backup_labels = backup_dir / "sleep_labels.json"
    current_log = current_dir / "sleep_log.jsonl"
    current_labels = current_dir / "sleep_labels.json"

    b_entries = load_jsonl(backup_log)
    c_entries = load_jsonl(current_log)
    labels = load_labels(backup_labels) + load_labels(current_labels)
    windows = parse_windows(labels)
    if not windows:
        raise SystemExit("No valid sleep label windows found in backup/current labels.")

    old_samples, old_stats = build_samples(
        entries=b_entries,
        windows=windows,
        source="old",
        source_weight=float(args.old_weight),
        max_lag_min=int(args.max_lag_min),
        pos_edge_exclude_min=int(args.pos_edge_exclude_min),
        neg_edge_margin_min=int(args.neg_edge_margin_min),
    )
    new_samples, new_stats = build_samples(
        entries=c_entries,
        windows=windows,
        source="new",
        source_weight=float(args.new_weight),
        max_lag_min=int(args.max_lag_min),
        pos_edge_exclude_min=int(args.pos_edge_exclude_min),
        neg_edge_margin_min=int(args.neg_edge_margin_min),
    )

    samples = old_samples + new_samples
    if len(samples) < 30:
        raise SystemExit(f"Too few samples after cleaning: {len(samples)}")

    X = np.array([s.x for s in samples], dtype=float)
    y = np.array([s.y for s in samples], dtype=int)
    w = np.array([s.w for s in samples], dtype=float)

    pos = int(y.sum())
    neg = int(len(y) - pos)
    if pos < 10 or neg < 10:
        raise SystemExit(
            f"Class imbalance too high after cleaning: pos={pos} neg={neg}"
        )

    model = Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    class_weight="balanced", max_iter=1000, random_state=42
                ),
            ),
        ]
    )
    model.fit(X, y, clf__sample_weight=w)

    y_hat = model.predict(X)
    train_acc = float((y_hat == y).mean())
    weighted_acc = float((w * (y_hat == y)).sum() / w.sum())

    out_model.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out_model)

    report_lines = [
        "Sleep Model Rebuild Report",
        f"time: {datetime.now().isoformat(timespec='seconds')}",
        f"backup_dir: {backup_dir}",
        f"current_dir: {current_dir}",
        f"windows: {len(windows)}",
        f"samples_total: {len(samples)} (pos={pos} neg={neg})",
        f"old_samples: {len(old_samples)} weight={args.old_weight}",
        f"new_samples: {len(new_samples)} weight={args.new_weight}",
        f"train_acc: {train_acc:.4f}",
        f"weighted_acc: {weighted_acc:.4f}",
        "old_stats: " + json.dumps(old_stats, ensure_ascii=False),
        "new_stats: " + json.dumps(new_stats, ensure_ascii=False),
        f"saved_model: {out_model}",
    ]
    report = "\n".join(report_lines)
    print(report)

    report_out = Path(args.report_out).resolve() if args.report_out else None
    if report_out is not None:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(report + "\n", encoding="utf-8")
        print(f"saved_report: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
