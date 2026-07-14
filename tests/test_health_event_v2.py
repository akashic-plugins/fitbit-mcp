from __future__ import annotations

from datetime import datetime, timedelta
import json

from monitor.health_event_v2 import HealthEventV2Engine, HealthEventV2Runtime


def _sleep_rows(start: datetime, days: int, polls_per_day: int, spo2: float) -> list[dict]:
    rows = []
    for day_offset in range(days):
        day_start = start + timedelta(days=day_offset)
        for poll in range(polls_per_day):
            observed_at = day_start + timedelta(minutes=5 * poll)
            rows.append(
                {
                    "poll_time": observed_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "state": "sleeping",
                    "spo2": spo2,
                    "spo2_time": observed_at.strftime("%H:%M:%S"),
                    "spo2_lag_min": 0,
                    "signals": {},
                }
            )
    return rows


def test_recovery_debt_is_not_generated() -> None:
    start = datetime(2026, 7, 1)
    rows = _sleep_rows(start, 7, 78, 90.0)
    rows += _sleep_rows(start + timedelta(days=7), 3, 40, 88.0)

    events = HealthEventV2Engine().process(rows)

    assert all(event.type != "recovery_debt" for event in events)


def test_removed_recovery_debt_is_dropped_from_state(tmp_path) -> None:
    state_path = tmp_path / "stat_events_v2.json"
    expires_at = (datetime.now() + timedelta(hours=1)).timestamp()
    state_path.write_text(
        json.dumps(
            {
                "pending": [
                    {
                        "id": "removed",
                        "type": "recovery_debt",
                        "expires_at_ts": expires_at,
                    },
                    {
                        "id": "kept",
                        "type": "acute_cardio_stress",
                        "expires_at_ts": expires_at,
                    },
                ],
                "acked_ids": [],
            }
        ),
        encoding="utf-8",
    )

    events = HealthEventV2Runtime(state_path).get_pending_events()

    assert [event["id"] for event in events] == ["kept"]


def _heart_row(observed_at: datetime, hr: float, evidence_id: str) -> dict:
    return {
        "poll_time": observed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "data_time": observed_at.strftime("%H:%M:%S"),
        "data_lag_min": 5,
        "state": "awake",
        "heart_rate": hr,
        "signals": {
            "hr_avg": hr,
            "zero_steps_count": 20,
            "sustained_zero_min": 30,
            "evidence_id": evidence_id,
        },
    }


def _heart_baseline(target: datetime) -> list[dict]:
    return [
        _heart_row(
            target - timedelta(days=day),
            80.0,
            f"baseline-{day}",
        )
        for day in range(2, 29)
    ]


def test_acute_cardio_stress_requires_unique_sustained_windows() -> None:
    target = datetime(2026, 7, 1, 10, 0)
    rows = _heart_baseline(target)
    for offset in (0, 15, 30):
        observed_at = target + timedelta(minutes=offset)
        evidence_id = f"high-{offset}"
        rows.append(_heart_row(observed_at, 105.0, evidence_id))
        rows.append(_heart_row(observed_at + timedelta(minutes=5), 105.0, evidence_id))

    events = [
        event
        for event in HealthEventV2Engine().process(rows)
        if event.type == "acute_cardio_stress"
    ]

    assert len(events) == 1
    assert events[0].metrics["unique_windows"] == 3
    assert events[0].metrics["window_span_min"] == 30.0
    assert events[0].metrics["hr_delta"] == 25.0


def test_repeated_fitbit_evidence_does_not_trigger_heart_alert() -> None:
    target = datetime(2026, 7, 1, 10, 0)
    rows = _heart_baseline(target)
    rows += [
        _heart_row(target + timedelta(minutes=offset), 105.0, "same-reading")
        for offset in (0, 5, 10, 15, 20, 25, 30)
    ]

    events = HealthEventV2Engine().process(rows)

    assert all(event.type != "acute_cardio_stress" for event in events)
