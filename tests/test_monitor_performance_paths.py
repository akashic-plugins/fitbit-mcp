from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def test_monitor_uses_reverse_tail_and_disk_free_dashboard_snapshot(tmp_path: Path) -> None:
    monitor_dir = Path(__file__).parents[1] / "monitor"
    script = r'''
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import server

log_path = Path(sys.argv[2]) / "history.jsonl"
rows = [
    {
        "poll_time": (datetime(2026, 8, 1, 12, 0) + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
        "state": "sleeping" if i < 3 else "awake",
        "reason": f"decision-{i}",
        "changed": i in {0, 3},
        "sleep_prob": 0.8 if i < 3 else 0.2,
        "signals": {"sleep_prob": 0.8 if i < 3 else 0.2, "prob_source": "ml"},
    }
    for i in range(6)
]
log_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

assert server._tail_jsonl(log_path, limit=3) == rows[-3:]
data = {
    "summary": {"heart_rate": 72, "steps": 10, "spo2": 97.0},
    "sleep": {"state": "awake", "reason": "decision-5", "since": None},
    "signals": {"sleep_prob": 0.2, "prob_source": "ml"},
    "data_meta": {"poll_time": rows[-1]["poll_time"]},
    "heart_rate": [{"time": "12:05:00", "value": 72}],
    "steps": [{"time": "12:05:00", "value": 0}],
    "last_updated": "12:05:00",
}
history_rows = [
    {
        **rows[0],
        "poll_time": "2026-07-31 12:05:00",
        "reason": "outside-window",
    },
    *rows,
]
snapshot = server._build_dashboard_snapshot(
    data, history_rows, now=datetime(2026, 8, 1, 12, 6)
)
assert [event["poll_time"] for event in snapshot["prediction_events"]] == [
    row["poll_time"] for row in reversed(rows)
]
server._dashboard_snapshot = snapshot
server.LOG_FILE = Path(sys.argv[2]) / "must-not-be-opened.jsonl"
response = server.api_dashboard_snapshot()
assert json.loads(response.body) == snapshot
'''
    environment = os.environ.copy()
    environment["AKA_PLUGIN_DATA_DIR"] = str(tmp_path)
    subprocess.run(
        [sys.executable, "-c", script, str(monitor_dir), str(tmp_path)],
        check=True,
        env=environment,
    )
