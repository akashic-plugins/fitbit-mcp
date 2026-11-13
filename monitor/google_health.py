"""Google Health API 访问层：OAuth 与数据读取，输出保持 monitor 既有的旧 Fitbit 数据形状。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
import time
from typing import TypedDict
import urllib.parse

import requests


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4/users/me"
SCOPES = " ".join(
    f"https://www.googleapis.com/auth/googlehealth.{name}.readonly"
    for name in ("activity_and_fitness", "health_metrics_and_measurements", "sleep")
)

_STAGE_KEYS = {"DEEP": "deep", "LIGHT": "light", "REM": "rem", "AWAKE": "wake"}


class MinutePoint(TypedDict):
    time: str
    value: float


class HealthApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Google Health API HTTP {status}: {body[:220]}")
        self.status = status


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    redirect_uri: str


# 生成授权地址：offline + consent 才会下发 refresh_token
def authorization_url(client: OAuthClient, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(client: OAuthClient, code: str, code_verifier: str) -> dict:
    return _token_request(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client.redirect_uri,
            "code_verifier": code_verifier,
        },
    )


# Google 刷新时通常不返回新的 refresh_token，需要沿用旧值
def refresh(client: OAuthClient, tokens: Mapping[str, object]) -> dict:
    fresh = _token_request(
        client,
        {"grant_type": "refresh_token", "refresh_token": str(tokens["refresh_token"])},
    )
    fresh.setdefault("refresh_token", tokens["refresh_token"])
    return fresh


def _token_request(client: OAuthClient, form: dict[str, str]) -> dict:
    r = requests.post(
        TOKEN_URL,
        data={**form, "client_id": client.client_id, "client_secret": client.client_secret},
        timeout=15,
    )
    if not r.ok:
        raise HealthApiError(r.status_code, r.text)
    tokens = dict(r.json())
    tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 3600))
    return tokens


class HealthClient:
    def __init__(self, access_token: str) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}

    # 当天按分钟聚合的心率，对齐旧 activities-heart-intraday 1min 数据集
    def minute_heart_rate(self, day: date) -> list[MinutePoint]:
        points: list[MinutePoint] = []
        for row in self._roll_up_day("heart-rate", day):
            avg = (row.get("heartRate") or {}).get("beatsPerMinuteAvg")
            if avg is not None:
                points.append({"time": _local_clock(row["startTime"]), "value": round(float(avg))})
        # rollUp 按时间倒序返回，下游以 [-1] 作为最新值
        return sorted(points, key=lambda p: p["time"])

    # 当天按分钟的步数；旧接口逐分钟补零，零步分钟是睡眠特征的一部分，这里补到 until 为止
    def minute_steps(self, day: date, until: str | None) -> list[MinutePoint]:
        counts: dict[str, float] = {}
        for row in self._roll_up_day("steps", day):
            total = (row.get("steps") or {}).get("countSum")
            if total is not None:
                counts[_local_clock(row["startTime"])] = float(total)
        clocks: list[str] = [*counts, *([until] if until else [])]
        if not clocks:
            return []
        last = max(clocks)
        end_minute = int(last[:2]) * 60 + int(last[3:5])
        return [
            {"time": clock, "value": counts.get(clock, 0.0)}
            for clock in (f"{m // 60:02d}:{m % 60:02d}:00" for m in range(end_minute + 1))
        ]

    def spo2_samples(self, day: date) -> list[MinutePoint]:
        field = "oxygen_saturation.sample_time.civil_time"
        points: list[MinutePoint] = [
            {
                "time": _civil_clock(point["oxygenSaturation"]["sampleTime"]["civilTime"]),
                "value": round(float(point["oxygenSaturation"]["percentage"]), 1),
            }
            for point in self._list("oxygen-saturation", _civil_range(field, day, day))
        ]
        return sorted(points, key=lambda p: p["time"])

    # 睡眠会话转换成旧 /1.2/sleep 形状；dateOfSleep 取醒来的本地日期
    def sleep_sessions(self, start: date, end: date) -> list[dict]:
        sessions = [
            _legacy_sleep(point["sleep"])
            for point in self._list("sleep", _civil_range("sleep.interval.civil_end_time", start, end))
        ]
        return sorted(sessions, key=lambda s: s["startTime"])

    def daily_hrv(self, start: date, end: date) -> dict[str, float]:
        out: dict[str, float] = {}
        for point in self._list(
            "daily-heart-rate-variability", _date_range("daily_heart_rate_variability", start, end)
        ):
            body = point["dailyHeartRateVariability"]
            value = body.get("averageHeartRateVariabilityMilliseconds") or body.get(
                "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds"
            )
            if value is not None:
                out[_civil_date(body["date"])] = round(float(value), 1)
        return out

    # 夜间皮温相对基线的偏移，对齐旧 temp/skin 的 nightlyRelative
    def skin_temperature(self, start: date, end: date) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for point in self._list(
            "daily-sleep-temperature-derivations",
            _date_range("daily_sleep_temperature_derivations", start, end),
        ):
            body = point["dailySleepTemperatureDerivations"]
            baseline = body.get("baselineTemperatureCelsius")
            if baseline is not None:
                delta = float(body["nightlyTemperatureCelsius"]) - float(baseline)
                out.append((_civil_date(body["date"]), round(delta, 3)))
        return sorted(out)

    # 核心体温按本地日期取均值，对齐旧 temp/core 的每日记录
    def core_temperature(self, start: date, end: date) -> list[tuple[str, float]]:
        by_day: dict[str, list[float]] = {}
        field = "core_body_temperature.sample_time.civil_time"
        for point in self._list("core-body-temperature", _civil_range(field, start, end)):
            body = point["coreBodyTemperature"]
            day = _civil_date(body["sampleTime"]["civilTime"]["date"])
            by_day.setdefault(day, []).append(float(body["temperatureCelsius"]))
        return sorted((d, round(sum(v) / len(v), 3)) for d, v in by_day.items())

    # 运动记录：返回本地墙钟时间、运动类型、主动/自动识别及汇总指标，按开始时间倒序。
    def workouts(self, start: date, end: date) -> list[dict]:
        sessions = [
            _parse_workout(point)
            for point in self._list(
                "exercise",
                _civil_range("exercise.interval.civil_start_time", start, end),
            )
            if isinstance(point.get("exercise"), Mapping)
        ]
        return sorted(sessions, key=lambda item: item["start_time"], reverse=True)

    # 按天 86400s 聚合活动指标（steps/distance/active-zone-minutes/active-energy-burned/total-calories）。
    def daily_activity_rollup(self, data_type: str, start: date, end: date) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in self._roll_up_range(data_type, start, end, window_size="86400s"):
            day = _parse_utc(str(row["startTime"])).astimezone().date().isoformat()
            out[day] = dict(row)
        return out

    # 久坐时段明细，按开始时间升序。
    def sedentary_periods(self, start: date, end: date) -> list[dict]:
        field = "sedentary_period.interval.civil_start_time"
        periods: list[dict] = []
        for point in self._list("sedentary-period", _civil_range(field, start, end)):
            body = point.get("sedentaryPeriod")
            if not isinstance(body, Mapping):
                continue
            interval = body.get("interval")
            if not isinstance(interval, Mapping):
                continue
            start_local = _interval_civil(interval, "start")[:19]
            end_local = _interval_civil(interval, "end")[:19]
            duration_sec = max(
                0,
                int((_parse_utc(str(interval["endTime"])) - _parse_utc(str(interval["startTime"]))).total_seconds()),
            )
            periods.append(
                {
                    "date": start_local[:10],
                    "start_time": start_local,
                    "end_time": end_local,
                    "duration_min": round(duration_sec / 60, 1),
                }
            )
        return sorted(periods, key=lambda item: item["start_time"])

    def daily_resting_heart_rate(self, start: date, end: date) -> dict[str, int]:
        out: dict[str, int] = {}
        for ds, body in self._daily_records("daily-resting-heart-rate", "dailyRestingHeartRate", start, end):
            bpm = body.get("beatsPerMinute")
            if isinstance(bpm, (int, float, str)):
                out[ds] = int(bpm)
        return out

    def daily_respiratory_rate(self, start: date, end: date) -> dict[str, float]:
        out: dict[str, float] = {}
        for ds, body in self._daily_records("daily-respiratory-rate", "dailyRespiratoryRate", start, end):
            rate = body.get("breathsPerMinute")
            if isinstance(rate, (int, float, str)):
                out[ds] = round(float(rate), 1)
        return out

    def daily_oxygen_saturation(self, start: date, end: date) -> dict[str, dict[str, float | None]]:
        out: dict[str, dict[str, float | None]] = {}
        for ds, body in self._daily_records("daily-oxygen-saturation", "dailyOxygenSaturation", start, end):
            avg = body.get("averagePercentage")
            if not isinstance(avg, (int, float, str)):
                continue
            low = body.get("lowerBoundPercentage")
            high = body.get("upperBoundPercentage")
            out[ds] = {
                "avg": round(float(avg), 1),
                "min": round(float(low), 1) if isinstance(low, (int, float, str)) else None,
                "max": round(float(high), 1) if isinstance(high, (int, float, str)) else None,
            }
        return out

    def daily_vo2_max(self, start: date, end: date) -> dict[str, float]:
        out: dict[str, float] = {}
        for ds, body in self._daily_records("daily-vo2-max", "dailyVo2Max", start, end):
            val = body.get("vo2Max") or body.get("cardioFitnessScore")
            if isinstance(val, (int, float, str)):
                out[ds] = round(float(val), 1)
        return out

    def _daily_records(
        self, data_type: str, member: str, start: date, end: date
    ) -> list[tuple[str, Mapping[str, object]]]:
        start_s, end_s = start.isoformat(), end.isoformat()
        limit = max(30, min((end - start).days + 10, 500))
        rows: list[tuple[str, Mapping[str, object]]] = []
        for point in self._list(data_type, filter_text=None, page_size=limit):
            body = point.get(member)
            if not isinstance(body, Mapping):
                continue
            raw_date = body.get("date")
            if not isinstance(raw_date, Mapping):
                continue
            ds = _civil_date(cast_int_map(raw_date))
            if ds < start_s:
                break
            if start_s <= ds <= end_s:
                rows.append((ds, body))
        return rows

    def _roll_up_range(
        self,
        data_type: str,
        start_day: date,
        end_day: date,
        *,
        window_size: str,
    ) -> Iterator[dict]:
        start = datetime.combine(start_day, dt_time()).astimezone()
        end_cap = datetime.combine(end_day + timedelta(days=1), dt_time()).astimezone()
        end = min(end_cap, datetime.now().astimezone().replace(microsecond=0))
        if end <= start:
            return
        body: dict[str, object] = {
            "range": {"startTime": start.isoformat(), "endTime": end.isoformat()},
            "windowSize": window_size,
            "pageSize": 1440,
        }
        while True:
            payload = self._request("POST", f"{data_type}/dataPoints:rollUp", json=body)
            yield from payload.get("rollupDataPoints", [])
            token = payload.get("nextPageToken")
            if not token:
                return
            body["pageToken"] = token

    def _roll_up_day(self, data_type: str, day: date) -> Iterator[dict]:
        return self._roll_up_range(data_type, day, day, window_size="60s")

    def _list(
        self,
        data_type: str,
        filter_text: str | None = None,
        *,
        page_size: int | None = None,
    ) -> Iterator[dict]:
        default_size = 25 if data_type in {"sleep", "exercise"} else 10000
        params: dict[str, str] = {"pageSize": str(page_size or default_size)}
        if filter_text:
            params["filter"] = filter_text
        while True:
            payload = self._request("GET", f"{data_type}/dataPoints", params=params)
            yield from payload.get("dataPoints", [])
            token = payload.get("nextPageToken")
            if not token:
                return
            if page_size is not None and not filter_text:
                return
            params["pageToken"] = token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict:
        r = requests.request(
            method,
            f"{API_BASE}/dataTypes/{path}",
            headers=self._headers,
            params=params,
            json=json,
            timeout=15,
        )
        if not r.ok:
            raise HealthApiError(r.status_code, r.text)
        return dict(r.json())


def _legacy_sleep(sleep: dict) -> dict:
    interval = sleep["interval"]
    start = _interval_civil(interval, "start")
    end = _interval_civil(interval, "end")
    summary = sleep.get("summary") or {}
    asleep = int(summary.get("minutesAsleep", 0))
    in_period = int(summary.get("minutesInSleepPeriod", 0))
    levels: dict[str, dict[str, int]] = {}
    for stage in summary.get("stagesSummary") or []:
        key = _STAGE_KEYS.get(stage.get("type", ""))
        if key:
            levels[key] = {"minutes": int(stage.get("minutes", 0))}
    duration_ms = int(
        (_parse_utc(interval["endTime"]) - _parse_utc(interval["startTime"])).total_seconds() * 1000
    )
    return {
        "dateOfSleep": end[:10],
        "startTime": start,
        "endTime": end,
        "duration": duration_ms,
        "efficiency": round(asleep * 100 / in_period) if in_period else None,
        "minutesAsleep": asleep,
        "minutesAwake": int(summary.get("minutesAwake", 0)),
        "isMainSleep": bool((sleep.get("metadata") or {}).get("mainSleep")),
        "levels": {"summary": levels},
    }


def _civil_range(field: str, start: date, end: date) -> str:
    return f'{field} >= "{start.isoformat()}" AND {field} < "{(end + timedelta(days=1)).isoformat()}"'


def _date_range(data_type: str, start: date, end: date) -> str:
    return _civil_range(f"{data_type}.date", start, end)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local_clock(value: str) -> str:
    return _parse_utc(value).astimezone().strftime("%H:%M:%S")


def _civil_date(value: Mapping[str, int]) -> str:
    return f"{value['year']:04d}-{value['month']:02d}-{value['day']:02d}"


def _civil_clock(value: Mapping[str, Mapping[str, int]]) -> str:
    clock = value.get("time") or {}
    return f"{clock.get('hours', 0):02d}:{clock.get('minutes', 0):02d}:{clock.get('seconds', 0):02d}"


def _civil_iso(value: Mapping[str, Mapping[str, int]]) -> str:
    return f"{_civil_date(value['date'])}T{_civil_clock(value)}.000"


# 睡眠会话的 interval 只保证给 UTC 时刻与偏移；civil* 字段并非所有响应都带，
# 缺了就按 startUtcOffset/endUtcOffset 换算成本地墙钟时间。
def _interval_civil(interval: Mapping[str, object], edge: str) -> str:
    civil = interval.get(f"civil{edge.capitalize()}Time")
    if isinstance(civil, Mapping):
        return _civil_iso(civil)  # type: ignore[arg-type]
    return _offset_iso(str(interval[f"{edge}Time"]), str(interval.get(f"{edge}UtcOffset") or "0s"))


def _offset_iso(value: str, offset: str) -> str:
    return (_parse_utc(value) + _parse_offset(offset)).strftime("%Y-%m-%dT%H:%M:%S.000")


def _parse_offset(value: str) -> timedelta:
    return timedelta(seconds=int(value.rstrip("s") or 0))


def _parse_duration_sec(value: object) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if not isinstance(value, str):
        return 0
    raw = value.strip().rstrip("s")
    if not raw:
        return 0
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return 0


def cast_int_map(raw: Mapping[str, object]) -> dict[str, int]:
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float, str))}


def _parse_workout(point: Mapping[str, object]) -> dict:
    ex = point.get("exercise")
    if not isinstance(ex, Mapping):
        raise ValueError("exercise 数据点缺少 exercise 字段")
    interval = ex.get("interval")
    if not isinstance(interval, Mapping):
        raise ValueError("exercise 数据点缺少 interval 字段")
    start_local = _interval_civil(interval, "start")[:19]
    end_local = _interval_civil(interval, "end")[:19]
    exercise_type = str(ex.get("exerciseType") or "UNKNOWN")
    display_name = str(ex.get("displayName") or exercise_type)
    raw_name = str(point.get("name") or "")
    workout_id = raw_name.rsplit("/", 1)[-1] if "/" in raw_name else f"{start_local}_{exercise_type}"

    active_sec = _parse_duration_sec(ex.get("activeDuration"))
    if active_sec == 0 and interval.get("startTime") and interval.get("endTime"):
        active_sec = max(
            0,
            int((_parse_utc(str(interval["endTime"])) - _parse_utc(str(interval["startTime"]))).total_seconds()),
        )

    raw_ms = ex.get("metricsSummary")
    ms: Mapping[str, object] = raw_ms if isinstance(raw_ms, Mapping) else {}
    raw_zones = ms.get("heartRateZoneDurations")
    zones_raw: Mapping[str, object] = raw_zones if isinstance(raw_zones, Mapping) else {}
    hr_zones_min = {
        str(k).removesuffix("Time"): round(_parse_duration_sec(v) / 60, 1)
        for k, v in zones_raw.items()
        if _parse_duration_sec(v) > 0
    }

    raw_source = point.get("dataSource")
    data_source: Mapping[str, object] = raw_source if isinstance(raw_source, Mapping) else {}
    raw_device = data_source.get("device")
    device_map: Mapping[str, object] = raw_device if isinstance(raw_device, Mapping) else {}

    distance_mm = ms.get("distanceMillimeters")
    pace_spm = ms.get("averagePaceSecondsPerMeter")
    calories = ms.get("caloriesKcal")
    steps = ms.get("steps")
    avg_hr = ms.get("averageHeartRateBeatsPerMinute")
    azm = ms.get("activeZoneMinutes")

    return {
        "id": workout_id,
        "exercise_type": exercise_type,
        "display_name": display_name,
        "recording_method": str(data_source.get("recordingMethod") or ""),
        "device": str(device_map.get("displayName") or ""),
        "date": start_local[:10],
        "start_time": start_local,
        "end_time": end_local,
        "active_duration_sec": active_sec,
        "active_duration_min": round(active_sec / 60, 1),
        "calories_kcal": round(float(calories), 1) if isinstance(calories, (int, float, str)) else None,
        "distance_km": round(float(distance_mm) / 1_000_000, 2) if isinstance(distance_mm, (int, float, str)) else None,
        "steps": int(steps) if isinstance(steps, (int, float, str)) else None,
        "avg_heart_rate_bpm": int(avg_hr) if isinstance(avg_hr, (int, float, str)) else None,
        "active_zone_minutes": int(azm) if isinstance(azm, (int, float, str)) else None,
        "avg_pace_min_per_km": round(float(pace_spm) * 1000 / 60, 2) if isinstance(pace_spm, (int, float, str)) else None,
        "heart_rate_zones_min": hr_zones_min,
    }
