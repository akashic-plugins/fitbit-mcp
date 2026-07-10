#!/usr/bin/env python3
"""
Manual binary retrain entry:
- Features: minute-level polling signals from sleep_log.jsonl
- Labels: Fitbit sleep windows from sleep_labels.json
- Targets: sleeping(1) / awake(0)
"""

from __future__ import annotations

import argparse

import sleep_model


def main() -> int:
    ap = argparse.ArgumentParser(description="Binary retrain for Fitbit sleep model.")
    ap.add_argument("--max-lag-min", type=int, default=8)
    ap.add_argument("--pos-edge-exclude-min", type=int, default=20)
    ap.add_argument("--neg-edge-margin-min", type=int, default=45)
    ap.add_argument("--awake-sample-weight", type=float, default=2.0)
    ap.add_argument("--min-samples", type=int, default=30)
    args = ap.parse_args()

    model = sleep_model.train_binary_from_labels(
        max_lag_min=args.max_lag_min,
        pos_edge_exclude_min=args.pos_edge_exclude_min,
        neg_edge_margin_min=args.neg_edge_margin_min,
        awake_sample_weight=args.awake_sample_weight,
        min_samples=args.min_samples,
    )
    if model is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
