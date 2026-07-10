#!/usr/bin/env python3
"""
使用 Fitbit 官方 Sleep API 回填睡眠标注。

默认行为：
- 按日期区间逐天调用 /1.2/user/-/sleep/date/{date}.json
- 将返回的 startTime/endTime 写入 sleep_labels.json（自动去重）
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import sleep_model
from paths import DATA_DIR

TOKENS_FILE = DATA_DIR / "tokens.json"


def _iter_dates(start_d: date, end_d: date):
    d = start_d
    while d <= end_d:
        yield d
        d += timedelta(days=1)


def _load_tokens(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"token 文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill Fitbit sleep labels by date range.")
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--tokens-file", default=str(TOKENS_FILE), help="tokens.json path")
    args = ap.parse_args()

    start_d = date.fromisoformat(args.start_date)
    end_d = date.fromisoformat(args.end_date)
    if start_d > end_d:
        raise ValueError("start-date 不能晚于 end-date")

    tokens = _load_tokens(Path(args.tokens_file))
    added_days = 0
    checked_days = 0

    for d in _iter_dates(start_d, end_d):
        checked_days += 1
        # 1. 按天调用 Fitbit 官方睡眠接口，确保真实睡眠时间来源一致。
        # 2. 复用既有去重逻辑写入 sleep_labels.json，避免重复窗口。
        # 3. 记录增量结果，方便后续评估覆盖范围。
        added = sleep_model.fetch_and_label(tokens, d.isoformat())
        if added:
            added_days += 1

    labels = sleep_model.load_labels()
    print(
        f"[ok] checked_days={checked_days} added_days={added_days} total_windows={len(labels)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
