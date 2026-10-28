from __future__ import annotations

from datetime import date

from monitor.google_health import HealthClient, _legacy_sleep

# 2026-10-28 从 Google Health v4 实际取回的睡眠点：interval 只有 UTC 时刻与偏移，
# 没有 civilStartTime / civilEndTime。旧实现直接下标取 civil 字段，导致 KeyError。
GOOGLE_SLEEP_POINT = {
    "dataSource": {
        "recordingMethod": "PASSIVELY_MEASURED",
        "device": {"formFactor": "FITNESS_BAND", "displayName": "Inspire 3"},
        "platform": "FITBIT",
    },
    "sleep": {
        "interval": {
            "startTime": "2026-10-27T18:25:00Z",
            "startUtcOffset": "28800s",
            "endTime": "2026-10-28T01:41:00Z",
            "endUtcOffset": "28800s",
        },
        "summary": {
            "minutesAsleep": 428,
            "minutesInSleepPeriod": 436,
            "minutesAwake": 8,
            "stagesSummary": [
                {"type": "AWAKE", "minutes": 8},
                {"type": "LIGHT", "minutes": 258},
                {"type": "DEEP", "minutes": 93},
                {"type": "REM", "minutes": 77},
            ],
        },
    },
}


def test_sleep_point_without_civil_fields_is_converted() -> None:
    session = _legacy_sleep(GOOGLE_SLEEP_POINT["sleep"])

    assert session["startTime"] == "2026-10-28T02:25:00.000"
    assert session["endTime"] == "2026-10-28T09:41:00.000"
    assert session["dateOfSleep"] == "2026-10-28"
    assert session["duration"] == 436 * 60 * 1000
    assert session["minutesAsleep"] == 428
    assert session["minutesAwake"] == 8
    assert session["efficiency"] == 98
    assert session["levels"]["summary"] == {
        "deep": {"minutes": 93},
        "light": {"minutes": 258},
        "rem": {"minutes": 77},
        "wake": {"minutes": 8},
    }


def test_sleep_point_with_civil_fields_prefers_them() -> None:
    sleep = {
        "interval": {
            "startTime": "2026-10-27T18:25:00Z",
            "startUtcOffset": "0s",
            "endTime": "2026-10-28T01:41:00Z",
            "endUtcOffset": "0s",
            "civilStartTime": {"date": {"year": 2026, "month": 10, "day": 28}, "time": {"hours": 3, "minutes": 1, "seconds": 5}},
            "civilEndTime": {"date": {"year": 2026, "month": 10, "day": 28}, "time": {"hours": 10, "minutes": 2, "seconds": 6}},
        },
        "summary": {"minutesAsleep": 60, "minutesInSleepPeriod": 60},
    }

    session = _legacy_sleep(sleep)

    assert session["startTime"] == "2026-10-28T03:01:05.000"
    assert session["endTime"] == "2026-10-28T10:02:06.000"


def test_sleep_point_without_offset_falls_back_to_utc() -> None:
    sleep = {
        "interval": {"startTime": "2026-10-27T18:25:00Z", "endTime": "2026-10-28T01:41:00Z"},
        "summary": {"minutesAsleep": 428, "minutesInSleepPeriod": 436},
    }

    session = _legacy_sleep(sleep)

    assert session["startTime"] == "2026-10-27T18:25:00.000"
    assert session["endTime"] == "2026-10-28T01:41:00.000"


def test_sleep_sessions_reads_real_google_shape(monkeypatch) -> None:
    client = HealthClient("token")
    filters: list[str] = []

    def fake_list(data_type: str, filter_text: str):
        filters.append(filter_text)
        assert data_type == "sleep"
        yield GOOGLE_SLEEP_POINT

    monkeypatch.setattr(client, "_list", fake_list)

    sessions = client.sleep_sessions(date(2026, 10, 21), date(2026, 10, 28))

    assert filters == [
        'sleep.interval.civil_end_time >= "2026-10-21" AND sleep.interval.civil_end_time < "2026-10-29"'
    ]
    assert [s["startTime"] for s in sessions] == ["2026-10-28T02:25:00.000"]
