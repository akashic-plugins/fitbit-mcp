#!/usr/bin/env python3
"""
回放 sleep_log.jsonl，生成 health events v2（旁路，不影响线上）。
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path

from health_event_v2 import HealthEventV2Engine, load_log_rows
from paths import DATA_DIR

DEFAULT_LOG = DATA_DIR / "sleep_log.jsonl"
DEFAULT_OUT_DIR = DATA_DIR / "logs"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "event_id",
                    "type",
                    "severity",
                    "confidence",
                    "created_at",
                    "message",
                ]
            )
        return
    keys = ["event_id", "type", "severity", "confidence", "created_at", "message"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in keys})


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay v2 health events from sleep log.")
    ap.add_argument("--log", default=str(DEFAULT_LOG), help="sleep_log.jsonl 路径")
    ap.add_argument(
        "--start-date",
        default="2026-03-02",
        help="开始日期（YYYY-MM-DD）",
    )
    ap.add_argument("--end-date", default=None, help="结束日期（YYYY-MM-DD）")
    ap.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="输出目录",
    )
    ap.add_argument(
        "--prefix",
        default="health_events_v2_replay",
        help="输出文件名前缀",
    )
    args = ap.parse_args()

    rows = load_log_rows(
        log_path=args.log,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    engine = HealthEventV2Engine()
    events = [e.to_dict() for e in engine.process(rows)]
    type_counter = Counter(e["type"] for e in events)
    severity_counter = Counter(e["severity"] for e in events)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = (
        f"{args.start_date}_to_{args.end_date}"
        if args.end_date
        else f"{args.start_date}_to_latest"
    )
    json_path = out_dir / f"{args.prefix}.{suffix}.json"
    csv_path = out_dir / f"{args.prefix}.{suffix}.csv"
    summary_path = out_dir / f"{args.prefix}.{suffix}.summary.txt"

    _write_json(json_path, events)
    _write_csv(csv_path, events)

    lines: list[str] = []
    lines.append("Health Events V2 Replay Summary")
    lines.append(f"log={args.log}")
    lines.append(f"start_date={args.start_date}")
    lines.append(f"end_date={args.end_date or 'latest'}")
    lines.append(f"rows={len(rows)}")
    lines.append(f"events_total={len(events)}")
    lines.append("")
    lines.append("By Type:")
    for t, c in sorted(type_counter.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"- {t}: {c}")
    lines.append("")
    lines.append("By Severity:")
    for s, c in sorted(severity_counter.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"- {s}: {c}")
    lines.append("")
    lines.append("Top Samples:")
    for e in events[:10]:
        lines.append(
            f"- [{e['created_at']}] {e['type']}({e['severity']}, conf={e['confidence']}) {e['message']}"
        )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"rows={len(rows)}")
    print(f"events_total={len(events)}")
    print("by_type=" + json.dumps(type_counter, ensure_ascii=False))
    print(f"json={json_path}")
    print(f"csv={csv_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
