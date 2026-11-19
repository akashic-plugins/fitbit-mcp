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


def test_legacy_sleep_preserves_stages_and_short_awakenings() -> None:
    sleep = {
        **GOOGLE_SLEEP_POINT["sleep"],
        "stages": [
            {
                "startTime": "2026-10-27T18:25:00Z",
                "startUtcOffset": "28800s",
                "endTime": "2026-10-27T18:40:00Z",
                "endUtcOffset": "28800s",
                "type": "LIGHT",
            },
            {
                "startTime": "2026-10-27T18:40:00Z",
                "startUtcOffset": "28800s",
                "endTime": "2026-10-27T19:00:00Z",
                "endUtcOffset": "28800s",
                "type": "DEEP",
            },
        ],
        "shortAwakenings": [
            {
                "startTime": "2026-10-27T18:35:00Z",
                "startUtcOffset": "28800s",
                "endTime": "2026-10-27T18:37:00Z",
                "endUtcOffset": "28800s",
            }
        ],
    }

    session = _legacy_sleep(sleep)

    assert session["stages"] == [
        {"start": "02:25", "end": "02:40", "m0": 0, "m1": 15, "type": "LIGHT"},
        {"start": "02:40", "end": "03:00", "m0": 15, "m1": 35, "type": "DEEP"},
    ]
    assert session["shortAwakenings"] == [
        {"start": "02:35", "end": "02:37", "m0": 10, "m1": 12}
    ]


def test_build_sleep_report_payload_includes_latest_detail_and_cross_midnight_hr(
    monkeypatch,
) -> None:
    from monitor import server

    class FakeClient:
        def __init__(self, _token: str) -> None:
            pass

        def sleep_sessions(self, start: date, end: date) -> list[dict]:
            return [
                {
                    "dateOfSleep": "2026-11-19",
                    "startTime": "2026-11-18T23:50:00.000",
                    "endTime": "2026-11-19T06:50:00.000",
                    "duration": 420 * 60 * 1000,
                    "efficiency": 97,
                    "minutesAsleep": 408,
                    "minutesAwake": 12,
                    "isMainSleep": True,
                    "levels": {"summary": {"deep": {"minutes": 80}, "light": {"minutes": 230}, "rem": {"minutes": 98}, "wake": {"minutes": 12}}},
                    "stages": [{"start": "23:50", "end": "00:10", "m0": 0, "m1": 20, "type": "LIGHT"}],
                    "shortAwakenings": [{"start": "23:55", "end": "23:57", "m0": 5, "m1": 7}],
                }
            ]

        def daily_hrv(self, start: date, end: date) -> dict[str, float]:
            return {"2026-11-19": 27.5}

        def daily_respiratory_rate(self, start: date, end: date) -> dict[str, float]:
            return {"2026-11-19": 18.1}

        def skin_temperature(self, start: date, end: date) -> list[tuple[str, float]]:
            return [("2026-11-19", 0.04)]

        def minute_heart_rate(self, day: date) -> list[dict]:
            if day == date(2026, 11, 18):
                return [{"time": "23:45:00", "value": 85}, {"time": "23:52:00", "value": 74}]
            if day == date(2026, 11, 19):
                return [{"time": "00:05:00", "value": 70}, {"time": "07:00:00", "value": 90}]
            return []

    class FakeDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 11, 19)

    monkeypatch.setattr(server.google_health, "HealthClient", FakeClient)
    monkeypatch.setattr(server, "date", FakeDate)

    status, payload = server._build_sleep_report_payload(days=2, tokens={"access_token": "tok"})

    assert status == 200
    latest = payload["latest_detail"]
    assert latest["date"] == "2026-11-19"
    assert latest["respiratory_rate"] == 18.1
    assert latest["skin_temp_delta_c"] == 0.04
    assert latest["short_awakenings_min"] == 2
    assert latest["night_hr_5min"] == [[0, 74], [15, 70]]
    assert latest["night_hr_stats"] == {"min": 70, "avg": 72.0, "max": 74}
