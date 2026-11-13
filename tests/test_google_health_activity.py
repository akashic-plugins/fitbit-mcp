from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

MONITOR_DIR = Path(__file__).resolve().parents[1] / "monitor"
if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

import server  # type: ignore[import-not-found]
from google_health import HealthClient, _parse_workout  # type: ignore[import-not-found]


WORKOUT_POINT = {
    "name": "users/2676595788398938530/dataTypes/exercise/dataPoints/4533240029710414800",
    "dataSource": {
        "recordingMethod": "PASSIVELY_MEASURED",
        "device": {"formFactor": "FITNESS_BAND", "displayName": "Inspire 3"},
        "platform": "FITBIT",
    },
    "exercise": {
        "interval": {
            "startTime": "2026-11-13T10:34:53Z",
            "startUtcOffset": "28800s",
            "endTime": "2026-11-13T10:54:31Z",
            "endUtcOffset": "28800s",
        },
        "activeDuration": "1178s",
        "exerciseType": "WALKING",
        "displayName": "步行",
        "metricsSummary": {
            "caloriesKcal": 98,
            "distanceMillimeters": 1073300,
            "steps": "1504",
            "averagePaceSecondsPerMeter": 1.0975,
            "averageHeartRateBeatsPerMinute": "108",
            "activeZoneMinutes": "9",
            "heartRateZoneDurations": {
                "lightTime": "1200s",
                "moderateTime": "0s",
            },
        },
    },
}


def test_parse_workout_extracts_real_google_health_fields() -> None:
    parsed = _parse_workout(WORKOUT_POINT)
    assert parsed["id"] == "4533240029710414800"
    assert parsed["exercise_type"] == "WALKING"
    assert parsed["display_name"] == "步行"
    assert parsed["start_time"] == "2026-11-13T18:34:53"
    assert parsed["end_time"] == "2026-11-13T18:54:31"
    assert parsed["active_duration_sec"] == 1178
    assert parsed["active_duration_min"] == 19.6
    assert parsed["calories_kcal"] == 98.0
    assert parsed["distance_km"] == 1.07
    assert parsed["steps"] == 1504
    assert parsed["avg_heart_rate_bpm"] == 108
    assert parsed["active_zone_minutes"] == 9
    assert parsed["heart_rate_zones_min"] == {"light": 20.0}


def test_is_notable_workout_filters_daily_walks_but_keeps_real_workouts() -> None:
    assert not server._is_notable_workout({"exercise_type": "WALKING", "active_duration_sec": 1900})
    assert server._is_notable_workout({"exercise_type": "WALKING", "active_duration_sec": 3600})
    assert server._is_notable_workout({"exercise_type": "BIKING", "active_duration_sec": 988})
    assert not server._is_notable_workout({"exercise_type": "BIKING", "active_duration_sec": 120})


def test_google_client_sedentary_and_daily_vitals(monkeypatch) -> None:
    client = HealthClient("token")

    def fake_list(data_type: str, filter_text: str | None = None, *, page_size: int | None = None):
        if data_type == "sedentary-period":
            yield {
                "sedentaryPeriod": {
                    "interval": {
                        "startTime": "2026-11-13T10:55:00Z",
                        "startUtcOffset": "28800s",
                        "endTime": "2026-11-13T11:16:00Z",
                        "endUtcOffset": "28800s",
                    }
                }
            }
        elif data_type == "daily-resting-heart-rate":
            yield {"dailyRestingHeartRate": {"date": {"year": 2026, "month": 11, "day": 13}, "beatsPerMinute": "74"}}
        elif data_type == "daily-respiratory-rate":
            yield {"dailyRespiratoryRate": {"date": {"year": 2026, "month": 11, "day": 13}, "breathsPerMinute": 17.8}}
        elif data_type == "daily-oxygen-saturation":
            yield {
                "dailyOxygenSaturation": {
                    "date": {"year": 2026, "month": 11, "day": 13},
                    "averagePercentage": 96.1,
                    "lowerBoundPercentage": 94.3,
                    "upperBoundPercentage": 97.5,
                }
            }
        elif data_type == "daily-vo2-max":
            yield {"dailyVo2Max": {"date": {"year": 2026, "month": 11, "day": 13}, "vo2Max": 42.4}}

    monkeypatch.setattr(client, "_list", fake_list)

    d = date(2026, 11, 13)
    sed = client.sedentary_periods(d, d)
    assert sed == [
        {
            "date": "2026-11-13",
            "start_time": "2026-11-13T18:55:00",
            "end_time": "2026-11-13T19:16:00",
            "duration_min": 21.0,
        }
    ]
    assert client.daily_resting_heart_rate(d, d) == {"2026-11-13": 74}
    assert client.daily_respiratory_rate(d, d) == {"2026-11-13": 17.8}
    assert client.daily_oxygen_saturation(d, d) == {
        "2026-11-13": {"avg": 96.1, "min": 94.3, "max": 97.5}
    }
    assert client.daily_vo2_max(d, d) == {"2026-11-13": 42.4}


def test_build_workouts_and_activity_report_payloads(monkeypatch) -> None:
    server._workouts_cache.clear()
    server._activity_report_cache.clear()

    class FakeClient:
        def __init__(self, _token: str) -> None:
            pass

        def workouts(self, start: date, end: date) -> list[dict]:
            return [
                _parse_workout(WORKOUT_POINT),
                {
                    "id": "w2",
                    "exercise_type": "BIKING",
                    "display_name": "自行车",
                    "date": end.isoformat(),
                    "start_time": f"{end.isoformat()}T14:43:00",
                    "end_time": f"{end.isoformat()}T15:00:00",
                    "active_duration_sec": 1020,
                    "active_duration_min": 17.0,
                    "calories_kcal": 117.0,
                    "distance_km": 3.5,
                    "steps": None,
                    "avg_heart_rate_bpm": 103,
                    "active_zone_minutes": 10,
                    "avg_pace_min_per_km": None,
                    "heart_rate_zones_min": {},
                },
            ]

        def daily_activity_rollup(self, data_type: str, start: date, end: date) -> dict[str, dict]:
            ds = end.isoformat()
            mapping = {
                "steps": {ds: {"steps": {"countSum": "8703"}}},
                "distance": {ds: {"distance": {"millimetersSum": "6350900"}}},
                "active-zone-minutes": {
                    ds: {
                        "activeZoneMinutes": {
                            "sumInFatBurnHeartZone": "10",
                            "sumInCardioHeartZone": "0",
                            "sumInPeakHeartZone": "0",
                        }
                    }
                },
                "active-energy-burned": {ds: {"activeEnergyBurned": {"kcalSum": 378.34}}},
                "total-calories": {ds: {"totalCalories": {"kcalSum": 1856.96}}},
            }
            return mapping.get(data_type, {})

        def sedentary_periods(self, start: date, end: date) -> list[dict]:
            ds = end.isoformat()
            return [
                {"date": ds, "start_time": f"{ds}T10:00:00", "end_time": f"{ds}T11:30:00", "duration_min": 90.0},
                {"date": ds, "start_time": f"{ds}T14:00:00", "end_time": f"{ds}T14:30:00", "duration_min": 30.0},
            ]

        def daily_resting_heart_rate(self, start: date, end: date) -> dict[str, int]:
            return {end.isoformat(): 74}

        def daily_respiratory_rate(self, start: date, end: date) -> dict[str, float]:
            return {end.isoformat(): 17.8}

        def daily_oxygen_saturation(self, start: date, end: date) -> dict[str, dict[str, float | None]]:
            return {end.isoformat(): {"avg": 96.1, "min": 94.3, "max": 97.5}}

        def daily_vo2_max(self, start: date, end: date) -> dict[str, float]:
            return {}

    monkeypatch.setattr(server.google_health, "HealthClient", FakeClient)

    status, w_payload = server._build_workouts_payload(
        days=7, exercise_type="biking", tokens={"access_token": "x"}
    )
    assert status == 200
    assert w_payload["count"] == 1
    assert w_payload["summary"]["total_duration_min"] == 17.0

    status2, a_payload = server._build_activity_report_payload(
        days=2, tokens={"access_token": "x"}
    )
    assert status2 == 200
    assert a_payload["summary"]["avg_steps"] == 8703
    today_row = a_payload["days"][-1]
    assert today_row["distance_km"] == 6.35
    assert today_row["active_zone_minutes"]["total"] == 10
    assert today_row["sedentary"] == {
        "count": 2,
        "total_minutes": 120.0,
        "longest_minutes": 90.0,
        "longest_window": "10:00-11:30",
    }
    assert today_row["resting_hr_bpm"] == 74
    assert today_row["respiratory_rate_bpm"] == 17.8
