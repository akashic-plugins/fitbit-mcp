#!/usr/bin/env python3
"""
Fitbit 实时健康数据仪表板
访问 http://<host>:<port>
"""

import json, os, time, base64, secrets, hashlib, urllib.parse, asyncio, copy, sys, atexit, shutil
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from threading import Thread, Lock, Event
from pathlib import Path
import tomllib
import requests as req
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import uvicorn
import sleep_model
import retrain_guard
import build_sleep_diff_report
from stat_engine import StatEngine
from health_event_v2 import HealthEventV2Runtime
from paths import CODE_DIR, DATA_DIR, data_path

# ── 配置 ────────────────────────────────────────────────────────────────────
BASE_DIR = CODE_DIR
RUNTIME_LOG_FILE = DATA_DIR / "monitor.runtime.log"


class _TeeTextIO:
    """镜像输出到原始流和日志文件。"""

    def __init__(self, original, mirror):
        self._original = original
        self._mirror = mirror
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "replace")

    def write(self, s):
        n = self._original.write(s)
        self._mirror.write(s)
        return n

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            self._mirror.flush()
        except Exception:
            pass

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return self._original.isatty()

    def writable(self):
        return True


def _stream_points_to(path: Path, stream) -> bool:
    try:
        stream_fd = stream.fileno()
        stream_stat = os.fstat(stream_fd)
        path_stat = path.stat()
    except Exception:
        return False
    return (stream_stat.st_dev, stream_stat.st_ino) == (
        path_stat.st_dev,
        path_stat.st_ino,
    )


def _install_runtime_log_mirror() -> None:
    """
    保证无论通过何种方式启动，stdout/stderr 都会写入 monitor.runtime.log。
    若上层已重定向到同一个文件，则不重复包裹，避免双写。
    """
    if isinstance(sys.stdout, _TeeTextIO) or isinstance(sys.stderr, _TeeTextIO):
        return
    try:
        RUNTIME_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_f = RUNTIME_LOG_FILE.open("a", encoding="utf-8", buffering=1)
    except Exception:
        return
    log_f.write(
        f"\n===== fitbit-monitor start {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"pid={os.getpid()} =====\n"
    )
    if not _stream_points_to(RUNTIME_LOG_FILE, sys.stdout):
        sys.stdout = _TeeTextIO(sys.stdout, log_f)
    if not _stream_points_to(RUNTIME_LOG_FILE, sys.stderr):
        sys.stderr = _TeeTextIO(sys.stderr, log_f)
    atexit.register(log_f.close)


_install_runtime_log_mirror()
CONFIG_CANDIDATES = [
    DATA_DIR / "monitor.config.toml",
    DATA_DIR / "monitor.config.local.toml",
]

DEFAULT_CONFIG: dict = {
    "server": {
        "host": "127.0.0.1",
        "port": 18765,
        "log_level": "warning",
        "poll_interval_sec": 300,
    },
    "fitbit": {
        "client_id": "23V3JZ",
        "client_secret": "",
        "redirect_uri": "http://127.0.0.1:18765/oauth/callback",
        "auth_url": "https://www.fitbit.com/oauth2/authorize",
        "token_url": "https://api.fitbit.com/oauth2/token",
        "api_base": "https://api.fitbit.com",
        "scopes": "heartrate activity oxygen_saturation sleep temperature",
    },
    "files": {
        "token_file": "tokens.json",
        "log_file": "sleep_log.jsonl",
        "static_dir": "static",
        "sleep_diff_report_file": "static/sleep_diff_report.html",
        "sleep_diff_default_start_date": "2026-03-02",
        "sleep_diff_min_rebuild_interval_sec": 1800,
    },
    "sleep_detection": {
        "sleep_enter_threshold": 0.75,
        "sleep_exit_threshold": 0.35,
        "sleep_enter_hr_gate_max": 72.0,
        "sleep_enter_override_prob": 0.93,
        "sleep_enter_confirm_polls": 2,
        "sleep_exit_confirm_polls": 2,
        "sleep_enter_min_confirm_minutes": 10,
        "sleep_stale_lag_guard_min": 8,
        "sleep_wake_confirm_polls_strict": 3,
        "sleep_wake_hr_min": 88.0,
        "sleep_wake_trend_min": 0.30,
        "sleep_wake_zero_steps_max": 10,
        "sleep_wake_sustained_zero_max": 3,
        "sleep_mid_band_sticky_sleep": True,
        "sleep_mid_band_sticky_min_prob": 0.65,
        "sleep_aggressive_bias_enabled": True,
        "sleep_aggressive_min_prob": 0.50,
        "sleep_aggressive_zero_steps_min": 18,
        "sleep_aggressive_sustained_zero_min": 40,
        "sleep_aggressive_hr_max": 84.0,
        "sleep_aggressive_max_lag_min": 8,
        "uncertain_delay_min": 15,
    },
    "health": {
        "baseline_days": 30,
        "baseline_min_points": 5,
        "window_polls": 3,
        "lookback_polls": 12,
        "history_max": 6000,
        # v1: 原 stat_engine；v2: 新策略；both: 同时产出（用于灰度）
        "event_engine": "v1",
        # v2 事件最大时效（小时），超时事件不再给 agent
        "max_event_age_hours": 12,
        "alert_cooldown_min": {
            "high_hr": 60,
            "low_spo2": 60,
            "hr_recovery": 6 * 60,
            "spo2_recovery": 6 * 60,
            "sleep_recovery": 12 * 60,
            "temperature": 12 * 60,
        },
    },
    "cache": {
        "sleep_recovery_seconds": 3600,
        "temperature_seconds": 3600,
    },
    "retrain": {
        "sleep_poll_interval_sec": 6 * 3600,
        "sleep_lookback_days": 7,
        "report_retrain_enabled": False,
        "report_retrain_cooldown_sec": 30 * 60,
        "wake_retrain_awake_streak": 2,
        "accept_guard_enabled": False,
        "guard_eval_days": 0,
        "guard_recent_eval_days": 7,
        "guard_buffer_min": 45,
        "guard_edge_buffer_min": 30,
        "guard_start_early_min": -30,
        "guard_start_late_min": 15,
        "guard_wake_early_min": -15,
        "guard_wake_late_min": 30,
        "guard_max_wake_late_over_60min_count": 0,
        "guard_min_start_hit_rate": 0.10,
        "guard_min_wake_hit_rate": 0.45,
        "guard_min_both_hit_rate": 0.06,
        "guard_max_sleep_core_awake_rate": 0.10,
        "guard_max_sleep_core_awake_rate_delta": 0.005,
        "guard_require_awake_le_uncertain_in_sleep_core": True,
        "guard_max_awake_core_sleep_rate": 0.15,
        "guard_max_awake_core_sleep_rate_delta": 0.05,
        "guard_max_awake_far_sleep_rate": 0.08,
        "guard_max_awake_far_sleep_rate_delta": 0.02,
        "guard_max_sleep_core_uncertain_rate": 0.18,
        "guard_max_sleep_core_uncertain_rate_delta": 0.02,
        "guard_min_core_after_first_sleeping_rate": 0.97,
        "guard_max_core_after_first_sleeping_rate_drop": 0.01,
    },
}


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _load_config_file(path: Path) -> dict:
    raw = path.read_bytes()
    if path.suffix.lower() != ".toml":
        raise ValueError(f"不支持的配置格式: {path.suffix}")
    cfg = tomllib.loads(raw.decode("utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("配置文件根节点必须是对象")
    return cfg


def _get_cfg(cfg: dict, keys: tuple[str, ...], default):
    node = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return default


def _resolve_local_path(raw: str, default_name: str) -> Path:
    return data_path(raw or default_name)


def _load_runtime_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    loaded: list[str] = []
    for p in CONFIG_CANDIDATES:
        if not p.exists():
            continue
        try:
            user_cfg = _load_config_file(p)
        except Exception as e:
            print(f"[配置] 读取失败 {p.name}: {e}")
            continue
        _deep_merge(cfg, user_cfg)
        loaded.append(p.name)
    if loaded:
        print(f"[配置] 已加载: {', '.join(loaded)}")
    else:
        print("[配置] 未找到 monitor.config.toml / monitor.config.local.toml，使用内置默认配置")
    return cfg


CONFIG = _load_runtime_config()

CLIENT_ID = str(
    _get_cfg(CONFIG, ("fitbit", "client_id"), DEFAULT_CONFIG["fitbit"]["client_id"])
)
CLIENT_SECRET = str(
    _get_cfg(
        CONFIG, ("fitbit", "client_secret"), DEFAULT_CONFIG["fitbit"]["client_secret"]
    )
)
REDIRECT_URI = str(
    _get_cfg(
        CONFIG, ("fitbit", "redirect_uri"), DEFAULT_CONFIG["fitbit"]["redirect_uri"]
    )
)
TOKEN_FILE = _resolve_local_path(
    str(
        _get_cfg(CONFIG, ("files", "token_file"), DEFAULT_CONFIG["files"]["token_file"])
    ),
    DEFAULT_CONFIG["files"]["token_file"],
)
STATIC_DIR = _resolve_local_path(
    str(
        _get_cfg(CONFIG, ("files", "static_dir"), DEFAULT_CONFIG["files"]["static_dir"])
    ),
    DEFAULT_CONFIG["files"]["static_dir"],
)
if str(_get_cfg(CONFIG, ("files", "static_dir"), "static")) == "static":
    STATIC_DIR = BASE_DIR / "static"

AUTH_URL = str(
    _get_cfg(CONFIG, ("fitbit", "auth_url"), DEFAULT_CONFIG["fitbit"]["auth_url"])
)
TOKEN_URL = str(
    _get_cfg(CONFIG, ("fitbit", "token_url"), DEFAULT_CONFIG["fitbit"]["token_url"])
)
API_BASE = str(
    _get_cfg(CONFIG, ("fitbit", "api_base"), DEFAULT_CONFIG["fitbit"]["api_base"])
)

scope_cfg = _get_cfg(CONFIG, ("fitbit", "scopes"), DEFAULT_CONFIG["fitbit"]["scopes"])
if isinstance(scope_cfg, list):
    SCOPES = " ".join(str(x) for x in scope_cfg)
else:
    SCOPES = str(scope_cfg)

POLL_INTERVAL = _as_int(
    _get_cfg(
        CONFIG,
        ("server", "poll_interval_sec"),
        DEFAULT_CONFIG["server"]["poll_interval_sec"],
    ),
    DEFAULT_CONFIG["server"]["poll_interval_sec"],
)
LOG_FILE = _resolve_local_path(
    str(_get_cfg(CONFIG, ("files", "log_file"), DEFAULT_CONFIG["files"]["log_file"])),
    DEFAULT_CONFIG["files"]["log_file"],
)
SLEEP_DIFF_REPORT_FILE = _resolve_local_path(
    str(
        _get_cfg(
            CONFIG,
            ("files", "sleep_diff_report_file"),
            DEFAULT_CONFIG["files"]["sleep_diff_report_file"],
        )
    ),
    DEFAULT_CONFIG["files"]["sleep_diff_report_file"],
)
SLEEP_DIFF_DEFAULT_START_DATE = str(
    _get_cfg(
        CONFIG,
        ("files", "sleep_diff_default_start_date"),
        DEFAULT_CONFIG["files"]["sleep_diff_default_start_date"],
    )
)
SLEEP_DIFF_MIN_REBUILD_INTERVAL_SEC = _as_int(
    _get_cfg(
        CONFIG,
        ("files", "sleep_diff_min_rebuild_interval_sec"),
        DEFAULT_CONFIG["files"]["sleep_diff_min_rebuild_interval_sec"],
    ),
    DEFAULT_CONFIG["files"]["sleep_diff_min_rebuild_interval_sec"],
)
SLEEP_ENTER_THRESHOLD = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_enter_threshold"), 0.75), 0.75
)
SLEEP_EXIT_THRESHOLD = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_exit_threshold"), 0.35), 0.35
)
SLEEP_ENTER_HR_GATE_MAX = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_enter_hr_gate_max"), 72.0), 72.0
)
SLEEP_ENTER_OVERRIDE_PROB = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_enter_override_prob"), 0.93), 0.93
)
SLEEP_ENTER_CONFIRM_POLLS = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_enter_confirm_polls"), 2), 2
)
SLEEP_EXIT_CONFIRM_POLLS = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_exit_confirm_polls"), 2), 2
)
SLEEP_ENTER_MIN_CONFIRM_MINUTES = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_enter_min_confirm_minutes"), 10), 10
)
SLEEP_STALE_LAG_GUARD_MIN = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_stale_lag_guard_min"), 8), 8
)
SLEEP_WAKE_CONFIRM_POLLS_STRICT = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_wake_confirm_polls_strict"), 3), 3
)
SLEEP_WAKE_HR_MIN = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_wake_hr_min"), 88.0), 88.0
)
SLEEP_WAKE_TREND_MIN = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_wake_trend_min"), 0.30), 0.30
)
SLEEP_WAKE_ZERO_STEPS_MAX = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_wake_zero_steps_max"), 10), 10
)
SLEEP_WAKE_SUSTAINED_ZERO_MAX = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_wake_sustained_zero_max"), 3), 3
)
SLEEP_MID_BAND_STICKY_SLEEP = _as_bool(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_mid_band_sticky_sleep"), True), True
)
SLEEP_MID_BAND_STICKY_MIN_PROB = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_mid_band_sticky_min_prob"), 0.65), 0.65
)
SLEEP_AGGRESSIVE_BIAS_ENABLED = _as_bool(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_aggressive_bias_enabled"), True), True
)
SLEEP_AGGRESSIVE_MIN_PROB = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_aggressive_min_prob"), 0.50), 0.50
)
SLEEP_AGGRESSIVE_ZERO_STEPS_MIN = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_aggressive_zero_steps_min"), 18), 18
)
SLEEP_AGGRESSIVE_SUSTAINED_ZERO_MIN = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_aggressive_sustained_zero_min"), 40), 40
)
SLEEP_AGGRESSIVE_HR_MAX = _as_float(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_aggressive_hr_max"), 84.0), 84.0
)
SLEEP_AGGRESSIVE_MAX_LAG_MIN = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "sleep_aggressive_max_lag_min"), 8), 8
)
UNCERTAIN_DELAY_MIN = _as_int(
    _get_cfg(CONFIG, ("sleep_detection", "uncertain_delay_min"), 15), 15
)
HEALTH_BASELINE_DAYS = _as_int(_get_cfg(CONFIG, ("health", "baseline_days"), 30), 30)
HEALTH_BASELINE_MIN_POINTS = _as_int(
    _get_cfg(CONFIG, ("health", "baseline_min_points"), 5), 5
)
HEALTH_WINDOW_POLLS = _as_int(_get_cfg(CONFIG, ("health", "window_polls"), 3), 3)
HEALTH_LOOKBACK_POLLS = _as_int(_get_cfg(CONFIG, ("health", "lookback_polls"), 12), 12)
health_cooldown = _get_cfg(
    CONFIG,
    ("health", "alert_cooldown_min"),
    DEFAULT_CONFIG["health"]["alert_cooldown_min"],
)
HEALTH_ALERT_COOLDOWN_MIN = dict(DEFAULT_CONFIG["health"]["alert_cooldown_min"])
if isinstance(health_cooldown, dict):
    for k, v in health_cooldown.items():
        HEALTH_ALERT_COOLDOWN_MIN[str(k)] = _as_int(
            v, HEALTH_ALERT_COOLDOWN_MIN.get(str(k), 0)
        )
SLEEP_RECOVERY_CACHE_SECONDS = _as_int(
    _get_cfg(CONFIG, ("cache", "sleep_recovery_seconds"), 3600), 3600
)
TEMPERATURE_CACHE_SECONDS = _as_int(
    _get_cfg(CONFIG, ("cache", "temperature_seconds"), 3600), 3600
)
HEALTH_HISTORY_MAX = _as_int(_get_cfg(CONFIG, ("health", "history_max"), 6000), 6000)
HEALTH_EVENT_ENGINE = str(
    _get_cfg(CONFIG, ("health", "event_engine"), "v1")
).strip().lower()
HEALTH_EVENT_MAX_AGE_HOURS = _as_int(
    _get_cfg(CONFIG, ("health", "max_event_age_hours"), 12),
    12,
)
if HEALTH_EVENT_ENGINE not in {"v1", "v2", "both"}:
    print(f"[配置] health.event_engine={HEALTH_EVENT_ENGINE} 非法，已回退为 v1")
    HEALTH_EVENT_ENGINE = "v1"
print(f"[配置] health.event_engine={HEALTH_EVENT_ENGINE}")
SERVER_HOST = str(
    _get_cfg(CONFIG, ("server", "host"), DEFAULT_CONFIG["server"]["host"])
)
SERVER_PORT = _as_int(
    _get_cfg(CONFIG, ("server", "port"), DEFAULT_CONFIG["server"]["port"]), 18765
)
SERVER_LOG_LEVEL = str(
    _get_cfg(CONFIG, ("server", "log_level"), DEFAULT_CONFIG["server"]["log_level"])
)

# ── 全局状态 ─────────────────────────────────────────────────────────────────


def _auto_update_model(tokens: dict | None = None, target_date: str | None = None):
    """拉取单日睡眠标注 → 重训练模型。"""
    global _sleep_model
    if tokens is None:
        tokens = valid_tokens()
    if not tokens:
        return
    print(f"[模型] 拉取睡眠标注（{target_date or '昨天'}）...")
    got_new = sleep_model.fetch_and_label(tokens, target_date)
    if got_new or not sleep_model.MODEL_FILE.exists():
        print("[模型] 开始训练...")
        model = _train_sleep_model()
        if model:
            with _model_lock:
                _sleep_model = model


SLEEP_POLL_INTERVAL = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "sleep_poll_interval_sec"),
        DEFAULT_CONFIG["retrain"]["sleep_poll_interval_sec"],
    ),
    DEFAULT_CONFIG["retrain"]["sleep_poll_interval_sec"],
)
SLEEP_LOOKBACK_DAYS = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "sleep_lookback_days"),
        DEFAULT_CONFIG["retrain"]["sleep_lookback_days"],
    ),
    DEFAULT_CONFIG["retrain"]["sleep_lookback_days"],
)
REPORT_RETRAIN_ENABLED = _as_bool(
    _get_cfg(
        CONFIG,
        ("retrain", "report_retrain_enabled"),
        DEFAULT_CONFIG["retrain"]["report_retrain_enabled"],
    ),
    DEFAULT_CONFIG["retrain"]["report_retrain_enabled"],
)
REPORT_RETRAIN_COOLDOWN = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "report_retrain_cooldown_sec"),
        DEFAULT_CONFIG["retrain"]["report_retrain_cooldown_sec"],
    ),
    DEFAULT_CONFIG["retrain"]["report_retrain_cooldown_sec"],
)
WAKE_RETRAIN_AWAKE_STREAK = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "wake_retrain_awake_streak"),
        DEFAULT_CONFIG["retrain"]["wake_retrain_awake_streak"],
    ),
    DEFAULT_CONFIG["retrain"]["wake_retrain_awake_streak"],
)
RETRAIN_ACCEPT_GUARD_ENABLED = _as_bool(
    _get_cfg(
        CONFIG,
        ("retrain", "accept_guard_enabled"),
        DEFAULT_CONFIG["retrain"]["accept_guard_enabled"],
    ),
    DEFAULT_CONFIG["retrain"]["accept_guard_enabled"],
)
RETRAIN_GUARD_EVAL_DAYS = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_eval_days"),
        DEFAULT_CONFIG["retrain"]["guard_eval_days"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_eval_days"],
)
RETRAIN_GUARD_RECENT_EVAL_DAYS = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_recent_eval_days"),
        DEFAULT_CONFIG["retrain"]["guard_recent_eval_days"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_recent_eval_days"],
)
RETRAIN_GUARD_BUFFER_MIN = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_buffer_min"),
        DEFAULT_CONFIG["retrain"]["guard_buffer_min"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_buffer_min"],
)
RETRAIN_GUARD_EDGE_BUFFER_MIN = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_edge_buffer_min"),
        DEFAULT_CONFIG["retrain"]["guard_edge_buffer_min"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_edge_buffer_min"],
)
RETRAIN_GUARD_START_EARLY_MIN = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_start_early_min"),
        DEFAULT_CONFIG["retrain"]["guard_start_early_min"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_start_early_min"],
)
RETRAIN_GUARD_START_LATE_MIN = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_start_late_min"),
        DEFAULT_CONFIG["retrain"]["guard_start_late_min"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_start_late_min"],
)
RETRAIN_GUARD_WAKE_EARLY_MIN = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_wake_early_min"),
        DEFAULT_CONFIG["retrain"]["guard_wake_early_min"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_wake_early_min"],
)
RETRAIN_GUARD_WAKE_LATE_MIN = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_wake_late_min"),
        DEFAULT_CONFIG["retrain"]["guard_wake_late_min"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_wake_late_min"],
)
RETRAIN_GUARD_MAX_WAKE_LATE_OVER_60MIN_COUNT = _as_int(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_wake_late_over_60min_count"),
        DEFAULT_CONFIG["retrain"]["guard_max_wake_late_over_60min_count"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_wake_late_over_60min_count"],
)
RETRAIN_GUARD_MIN_START_HIT_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_min_start_hit_rate"),
        DEFAULT_CONFIG["retrain"]["guard_min_start_hit_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_min_start_hit_rate"],
)
RETRAIN_GUARD_MIN_WAKE_HIT_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_min_wake_hit_rate"),
        DEFAULT_CONFIG["retrain"]["guard_min_wake_hit_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_min_wake_hit_rate"],
)
RETRAIN_GUARD_MIN_BOTH_HIT_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_min_both_hit_rate"),
        DEFAULT_CONFIG["retrain"]["guard_min_both_hit_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_min_both_hit_rate"],
)
RETRAIN_GUARD_MAX_SLEEP_CORE_AWAKE_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_sleep_core_awake_rate"),
        DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_awake_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_awake_rate"],
)
RETRAIN_GUARD_MAX_SLEEP_CORE_AWAKE_RATE_DELTA = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_sleep_core_awake_rate_delta"),
        DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_awake_rate_delta"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_awake_rate_delta"],
)
RETRAIN_GUARD_REQUIRE_AWAKE_LE_UNCERTAIN_IN_SLEEP_CORE = _as_bool(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_require_awake_le_uncertain_in_sleep_core"),
        DEFAULT_CONFIG["retrain"]["guard_require_awake_le_uncertain_in_sleep_core"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_require_awake_le_uncertain_in_sleep_core"],
)
RETRAIN_GUARD_MAX_AWAKE_CORE_SLEEP_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_awake_core_sleep_rate"),
        DEFAULT_CONFIG["retrain"]["guard_max_awake_core_sleep_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_awake_core_sleep_rate"],
)
RETRAIN_GUARD_MAX_AWAKE_CORE_SLEEP_RATE_DELTA = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_awake_core_sleep_rate_delta"),
        DEFAULT_CONFIG["retrain"]["guard_max_awake_core_sleep_rate_delta"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_awake_core_sleep_rate_delta"],
)
RETRAIN_GUARD_MAX_AWAKE_FAR_SLEEP_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_awake_far_sleep_rate"),
        DEFAULT_CONFIG["retrain"]["guard_max_awake_far_sleep_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_awake_far_sleep_rate"],
)
RETRAIN_GUARD_MAX_AWAKE_FAR_SLEEP_RATE_DELTA = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_awake_far_sleep_rate_delta"),
        DEFAULT_CONFIG["retrain"]["guard_max_awake_far_sleep_rate_delta"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_awake_far_sleep_rate_delta"],
)
RETRAIN_GUARD_MAX_SLEEP_CORE_UNCERTAIN_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_sleep_core_uncertain_rate"),
        DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_uncertain_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_uncertain_rate"],
)
RETRAIN_GUARD_MAX_SLEEP_CORE_UNCERTAIN_RATE_DELTA = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_sleep_core_uncertain_rate_delta"),
        DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_uncertain_rate_delta"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_sleep_core_uncertain_rate_delta"],
)
RETRAIN_GUARD_MIN_CORE_AFTER_FIRST_SLEEPING_RATE = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_min_core_after_first_sleeping_rate"),
        DEFAULT_CONFIG["retrain"]["guard_min_core_after_first_sleeping_rate"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_min_core_after_first_sleeping_rate"],
)
RETRAIN_GUARD_MAX_CORE_AFTER_FIRST_SLEEPING_RATE_DROP = _as_float(
    _get_cfg(
        CONFIG,
        ("retrain", "guard_max_core_after_first_sleeping_rate_drop"),
        DEFAULT_CONFIG["retrain"]["guard_max_core_after_first_sleeping_rate_drop"],
    ),
    DEFAULT_CONFIG["retrain"]["guard_max_core_after_first_sleeping_rate_drop"],
)
_retrain_lock = Lock()
_last_retrain_at: float = 0.0


def _train_sleep_model() -> object | None:
    """统一训练入口：优先二分类/方向化，训练后按门禁决定是否接纳新模型。"""

    def _backup_model_for_guard() -> Path | None:
        if not sleep_model.MODEL_FILE.exists():
            return None
        backup_dir = DATA_DIR / "backups" / "model"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = (
            backup_dir
            / f"sleep_model.pre-retrain-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pkl"
        )
        shutil.copy2(sleep_model.MODEL_FILE, backup_path)
        return backup_path

    baseline_backup: Path | None = None
    if RETRAIN_ACCEPT_GUARD_ENABLED:
        try:
            baseline_backup = _backup_model_for_guard()
            if baseline_backup is not None:
                print(f"[模型] 已备份旧模型用于门禁比对: {baseline_backup}", flush=True)
        except Exception as e:
            print(f"[模型] 备份旧模型失败，跳过门禁比对: {e}", flush=True)
            baseline_backup = None

    model: object | None = sleep_model.train()
    if model is None:
        return None

    if not RETRAIN_ACCEPT_GUARD_ENABLED:
        return model
    if baseline_backup is None:
        print("[模型] 无旧模型可比对，跳过门禁，接纳新模型", flush=True)
        return model

    try:
        guard = retrain_guard.run_guard(
            baseline_model_path=baseline_backup,
            candidate_model_path=sleep_model.MODEL_FILE,
            eval_days=RETRAIN_GUARD_EVAL_DAYS,
            recent_eval_days=RETRAIN_GUARD_RECENT_EVAL_DAYS,
            buffer_min=max(0, RETRAIN_GUARD_BUFFER_MIN),
            edge_buffer_min=max(0, RETRAIN_GUARD_EDGE_BUFFER_MIN),
            start_early_min=RETRAIN_GUARD_START_EARLY_MIN,
            start_late_min=RETRAIN_GUARD_START_LATE_MIN,
            wake_early_min=RETRAIN_GUARD_WAKE_EARLY_MIN,
            wake_late_min=RETRAIN_GUARD_WAKE_LATE_MIN,
            max_wake_late_over_60min_count=max(
                0, RETRAIN_GUARD_MAX_WAKE_LATE_OVER_60MIN_COUNT
            ),
            max_awake_core_sleep_rate=max(0.0, RETRAIN_GUARD_MAX_AWAKE_CORE_SLEEP_RATE),
            max_awake_core_sleep_rate_delta=max(
                0.0, RETRAIN_GUARD_MAX_AWAKE_CORE_SLEEP_RATE_DELTA
            ),
            max_awake_far_sleep_rate=max(0.0, RETRAIN_GUARD_MAX_AWAKE_FAR_SLEEP_RATE),
            max_awake_far_sleep_rate_delta=max(
                0.0, RETRAIN_GUARD_MAX_AWAKE_FAR_SLEEP_RATE_DELTA
            ),
            max_sleep_core_uncertain_rate=max(
                0.0, RETRAIN_GUARD_MAX_SLEEP_CORE_UNCERTAIN_RATE
            ),
            max_sleep_core_uncertain_rate_delta=max(
                0.0, RETRAIN_GUARD_MAX_SLEEP_CORE_UNCERTAIN_RATE_DELTA
            ),
            min_core_after_first_sleeping_rate=max(
                0.0, RETRAIN_GUARD_MIN_CORE_AFTER_FIRST_SLEEPING_RATE
            ),
            max_core_after_first_sleeping_rate_drop=max(
                0.0, RETRAIN_GUARD_MAX_CORE_AFTER_FIRST_SLEEPING_RATE_DROP
            ),
            min_start_hit_rate=max(0.0, RETRAIN_GUARD_MIN_START_HIT_RATE),
            min_wake_hit_rate=max(0.0, RETRAIN_GUARD_MIN_WAKE_HIT_RATE),
            min_both_hit_rate=max(0.0, RETRAIN_GUARD_MIN_BOTH_HIT_RATE),
            max_sleep_core_awake_rate=max(0.0, RETRAIN_GUARD_MAX_SLEEP_CORE_AWAKE_RATE),
            max_sleep_core_awake_rate_delta=max(
                0.0, RETRAIN_GUARD_MAX_SLEEP_CORE_AWAKE_RATE_DELTA
            ),
            require_awake_le_uncertain_in_sleep_core=(
                RETRAIN_GUARD_REQUIRE_AWAKE_LE_UNCERTAIN_IN_SLEEP_CORE
            ),
        )
        print(f"[模型] 重训门禁评估: {retrain_guard.dumps_compact(guard)}", flush=True)
        if not guard.get("accept", False):
            shutil.copy2(baseline_backup, sleep_model.MODEL_FILE)
            print("[模型] 门禁拒绝新模型，已回滚到旧模型", flush=True)
            return None
        print("[模型] 门禁通过，接纳新模型", flush=True)
        return model
    except Exception as e:
        try:
            shutil.copy2(baseline_backup, sleep_model.MODEL_FILE)
        except Exception:
            pass
        print(f"[模型] 门禁评估异常，已回滚旧模型: {e}", flush=True)
        return None


def _retrain_from_report(reason: str, target_dates: list[str] | None = None):
    """
    拉取睡眠报告并尝试重训（后台任务）。
    默认拉取 today + yesterday，兼容 Fitbit 报告落地延迟。
    """
    global _sleep_model, _last_retrain_at
    if not REPORT_RETRAIN_ENABLED:
        print(f"[模型] 报告触发重训已关闭（触发原因: {reason}）", flush=True)
        return
    try:
        with _retrain_lock:
            now_ts = time.time()
            if now_ts - _last_retrain_at < REPORT_RETRAIN_COOLDOWN:
                print(f"[模型] 跳过重训（冷却中，触发原因: {reason}）")
                return

            if target_dates is None:
                today = date.today()
                target_dates = [
                    today.isoformat(),
                    (today - timedelta(days=1)).isoformat(),
                ]

            tokens = valid_tokens()
            if not tokens:
                print(f"[模型] 无有效 token，无法执行报告重训（触发原因: {reason}）")
                return

            got_any_new = False
            for d in dict.fromkeys(target_dates):  # 去重且保序
                if sleep_model.fetch_and_label(tokens, d):
                    got_any_new = True

            if got_any_new or not sleep_model.MODEL_FILE.exists():
                print(f"[模型] 触发重训（原因: {reason}）...", flush=True)
                try:
                    candidate_model = _train_sleep_model()
                except Exception as e:
                    print(
                        f"[模型] 训练异常，保留旧模型（触发原因: {reason}）: {e}",
                        flush=True,
                    )
                    return
                if candidate_model:
                    with _model_lock:
                        _sleep_model = candidate_model
                    _last_retrain_at = time.time()
                    print("[模型] 报告触发重训完成，模型已热更新", flush=True)
                else:
                    print(
                        f"[模型] 训练未产出有效模型，保留旧模型（触发原因: {reason}）",
                        flush=True,
                    )
            else:
                print(
                    f"[模型] 未发现新睡眠标注，跳过重训（触发原因: {reason}）",
                    flush=True,
                )
                _last_retrain_at = time.time()
    except Exception as e:
        print(f"[模型] 报告触发重训失败: {e}")


def trigger_retrain_async(reason: str, target_dates: list[str] | None = None) -> bool:
    if not REPORT_RETRAIN_ENABLED:
        print(f"[模型] 报告触发重训已关闭（触发原因: {reason}）", flush=True)
        return False
    Thread(
        target=_retrain_from_report, args=(reason, target_dates), daemon=True
    ).start()
    return True


def sleep_label_loop():
    """
    独立的睡眠标注轮询线程，每 6 小时执行一次：
    1. 拉取最近 7 天的 Fitbit 睡眠报告
    2. 有新标注则自动重训练模型
    """
    global _sleep_model, _last_retrain_at
    if not REPORT_RETRAIN_ENABLED:
        print("[模型] 报告自动补拉重训已关闭", flush=True)
        return
    while True:
        try:
            tokens = valid_tokens()
            if tokens:
                with _retrain_lock:
                    got_any_new = False
                    for i in range(SLEEP_LOOKBACK_DAYS):
                        d = (date.today() - timedelta(days=i)).isoformat()
                        if sleep_model.fetch_and_label(tokens, d):
                            got_any_new = True
                    if got_any_new or not sleep_model.MODEL_FILE.exists():
                        print("[模型] 有新睡眠数据，重新训练...", flush=True)
                        model = _train_sleep_model()
                        if model:
                            with _model_lock:
                                _sleep_model = model
                            _last_retrain_at = time.time()
                            print("[模型] 训练完成，已更新", flush=True)
                    else:
                        print(
                            f"[模型] 无新睡眠数据（已检查最近 {SLEEP_LOOKBACK_DAYS} 天）",
                            flush=True,
                        )
        except Exception as e:
            print(f"[模型] 睡眠标注轮询出错: {e}")
        time.sleep(SLEEP_POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop, _sleep_model
    main_loop = asyncio.get_event_loop()

    # 启动时：先尝试加载已有模型，再尝试自动更新
    with _model_lock:
        _sleep_model = sleep_model.load_model()
    _init_health_history()

    # 启动睡眠标注轮询线程（立即执行一次，之后每 6 小时）
    if REPORT_RETRAIN_ENABLED:
        Thread(target=sleep_label_loop, daemon=True).start()
    else:
        print("[模型] 报告自动补拉重训已关闭", flush=True)
    Thread(target=polling_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
data_lock = Lock()
poll_event = Event()  # 触发立即轮询
main_loop: asyncio.AbstractEventLoop | None = None
ws_clients: set[WebSocket] = set()

latest_data: dict = {
    "heart_rate": [],
    "steps": [],
    "spo2": [],
    "summary": {"heart_rate": None, "steps": 0, "spo2": None},
    "sleep": {"state": "unknown", "reason": "", "since": None},
    "agent_policy": {
        "mode": "cautious",
        "action": "delay_low_priority",
        "delay_min": UNCERTAIN_DELAY_MIN,
    },
    "health_context": {
        "alerts": [],
        "notify_alerts": [],
        "baseline": {},
        "coverage": {},
    },
    "last_updated": None,
}
_last_sleep_state: str = "unknown"
_sleep_model = None  # 已训练的 sklearn 模型
_model_lock = Lock()
_high_prob_confirm_count: int = 0
_low_prob_confirm_count: int = 0
_last_confirm_evidence_id: str | None = None
_sleep_enter_candidate_at: datetime | None = None
_sleep_session_active: bool = False
_sleep_viterbi_state = sleep_model.OnlineViterbiState()
_last_fetch_day: str | None = None
_crossday_hr_ref_tail: list[dict] = []
_crossday_steps_ref_tail: list[dict] = []
_health_history: deque[dict] = deque(maxlen=HEALTH_HISTORY_MAX)
_health_alert_last_notify: dict[str, float] = {}
_sleep_recovery_cache: dict[str, object] = {"fetched_at": 0.0, "payload": None}
_temperature_cache: dict[str, object] = {"fetched_at": 0.0, "payload": None}
_stat_engine = StatEngine(DATA_DIR / "stat_events.json")
_stat_engine_v2 = HealthEventV2Runtime(
    DATA_DIR / "stat_events_v2.json",
    max_event_age_hours=HEALTH_EVENT_MAX_AGE_HOURS,
)
_sleep_diff_lock = Lock()

_code_verifier: str | None = None

# ── Auth 工具 ────────────────────────────────────────────────────────────────


def _update_health_event_engines(current_entry: dict, history: list[dict]) -> None:
    if HEALTH_EVENT_ENGINE in {"v1", "both"}:
        _stat_engine.update(current_entry, history)
    if HEALTH_EVENT_ENGINE in {"v2", "both"}:
        _stat_engine_v2.update(current_entry, history)


def _current_health_events() -> list[dict]:
    if HEALTH_EVENT_ENGINE == "v1":
        return _stat_engine.get_pending_events()
    if HEALTH_EVENT_ENGINE == "v2":
        return _stat_engine_v2.get_pending_events()
    # both：v2 优先，其次补 v1 中未出现的 id
    v2 = _stat_engine_v2.get_pending_events()
    v1 = _stat_engine.get_pending_events()
    seen = {str(e.get("id", "")).strip() for e in v2}
    merged = list(v2)
    for e in v1:
        eid = str(e.get("id", "")).strip()
        if eid and eid in seen:
            continue
        merged.append(e)
    return merged


def _ack_health_event(event_id: str) -> bool:
    if HEALTH_EVENT_ENGINE == "v1":
        return _stat_engine.acknowledge(event_id)
    if HEALTH_EVENT_ENGINE == "v2":
        return _stat_engine_v2.acknowledge(event_id)
    ok1 = _stat_engine.acknowledge(event_id)
    ok2 = _stat_engine_v2.acknowledge(event_id)
    return bool(ok1 or ok2)


def make_pkce() -> str:
    global _code_verifier
    _code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(_code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def _resolve_sleep_diff_start_date() -> date:
    try:
        return date.fromisoformat(SLEEP_DIFF_DEFAULT_START_DATE)
    except Exception:
        return date(2026, 3, 2)


def _ensure_sleep_model_for_report() -> tuple[bool, str]:
    global _sleep_model
    model_file = sleep_model.MODEL_FILE
    labels_file = sleep_model.LABELS_FILE
    if model_file.exists():
        with _model_lock:
            if _sleep_model is None:
                _sleep_model = sleep_model.load_model()
        if _safe_mtime(model_file) >= _safe_mtime(labels_file):
            return True, "reuse_existing"
        # 看板优先保证可打开；模型刷新交给后台轮询/手动重训，不在这里阻塞。
        return True, "reuse_existing_stale"
    model = _train_sleep_model()
    if model is None and not model_file.exists():
        return False, "model_missing_and_train_failed"
    with _model_lock:
        if model is not None:
            _sleep_model = model
        elif _sleep_model is None:
            _sleep_model = sleep_model.load_model()
    return True, "trained_or_reloaded"


def _sleep_diff_should_rebuild(force: bool) -> bool:
    if force or not SLEEP_DIFF_REPORT_FILE.exists():
        return True
    report_mtime = _safe_mtime(SLEEP_DIFF_REPORT_FILE)
    deps = [
        sleep_model.MODEL_FILE,
        sleep_model.LABELS_FILE,
        BASE_DIR / "build_sleep_diff_report.py",
    ]
    latest_hard_dep = max(_safe_mtime(p) for p in deps)
    if report_mtime < latest_hard_dep:
        return True

    # 日志持续增长会导致每次都重建；改为“间隔到期后才因日志变更重建”。
    log_mtime = _safe_mtime(LOG_FILE)
    if report_mtime >= log_mtime:
        return False
    age_sec = max(0, int(time.time() - report_mtime))
    return age_sec >= max(60, SLEEP_DIFF_MIN_REBUILD_INTERVAL_SEC)


def _build_sleep_diff_if_needed(force: bool = False) -> dict:
    with _sleep_diff_lock:
        # 1. 先确保模型二进制可用（缺失或过旧时自动训练）。
        ok, model_action = _ensure_sleep_model_for_report()
        if not ok:
            return {"ok": False, "error": model_action}

        # 2. 若报告过期或强制刷新，则重放日志并生成 HTML。
        should_build = _sleep_diff_should_rebuild(force)
        start_d = _resolve_sleep_diff_start_date()
        end_d = date.today()
        if should_build:
            rows = build_sleep_diff_report.build_rows(
                start_d, end_d, binary_mode="uncertain_as_sleeping"
            )
            html = build_sleep_diff_report.render_html(
                rows, start_d, end_d, binary_mode="uncertain_as_sleeping"
            )
            SLEEP_DIFF_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            SLEEP_DIFF_REPORT_FILE.write_text(html, encoding="utf-8")

        # 3. 返回统一元信息，供看板按钮和页面路由复用。
        return {
            "ok": SLEEP_DIFF_REPORT_FILE.exists(),
            "built": should_build,
            "model_action": model_action,
            "report_path": str(SLEEP_DIFF_REPORT_FILE),
            "url": "/sleep-diff",
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
        }


def basic_header() -> str:
    return "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()


# ── Token 管理 ───────────────────────────────────────────────────────────────


def save_tokens(t: dict):
    TOKEN_FILE.write_text(json.dumps(t, indent=2))


def load_tokens() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text())


def refresh_tokens(t: dict) -> dict | None:
    try:
        r = req.post(
            TOKEN_URL,
            headers={
                "Authorization": basic_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"]},
        )
        r.raise_for_status()
        new = r.json()
        new["expires_at"] = time.time() + new.get("expires_in", 28800)
        save_tokens(new)
        return new
    except Exception as e:
        print(f"Token 刷新失败: {e}")
        return None


def valid_tokens() -> dict | None:
    t = load_tokens()
    if not t:
        return None
    if time.time() > t.get("expires_at", 0) - 300:
        return refresh_tokens(t)
    return t


def _safe_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _parse_poll_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    m = n // 2
    if n % 2 == 1:
        return s[m]
    return (s[m - 1] + s[m]) / 2


def _std(values: list[float], mean_v: float | None = None) -> float:
    if not values or len(values) < 2:
        return 0.0
    mean_v = mean_v if mean_v is not None else (_avg(values) or 0.0)
    var = sum((x - mean_v) ** 2 for x in values) / (len(values) - 1)
    return var**0.5


def _tail_jsonl(path: Path, limit: int = HEALTH_HISTORY_MAX) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows: list[dict] = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except Exception:
            continue
    return rows


def _init_health_history() -> None:
    rows = _tail_jsonl(LOG_FILE, limit=HEALTH_HISTORY_MAX)
    _health_history.clear()
    _health_history.extend(rows)


def _history_last_days(days: int = HEALTH_BASELINE_DAYS) -> list[dict]:
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    rows: list[dict] = []
    for e in _health_history:
        dt = _parse_poll_dt(e.get("poll_time"))
        if dt is None:
            continue
        if dt >= cutoff:
            rows.append(e)
    return rows


def _collect_recent_consecutive(
    rows: list[dict],
    *,
    predicate,
    need: int = HEALTH_WINDOW_POLLS,
    lookback: int = HEALTH_LOOKBACK_POLLS,
) -> list[dict]:
    consec: list[dict] = []
    for e in reversed(rows[-lookback:]):
        if predicate(e):
            consec.append(e)
            if len(consec) >= need:
                return list(reversed(consec))
        elif consec:
            break
    return []


def _cooldown_remaining_sec(alert_type: str, now_ts: float) -> int:
    cooldown_s = int(HEALTH_ALERT_COOLDOWN_MIN.get(alert_type, 60) * 60)
    last = float(_health_alert_last_notify.get(alert_type, 0.0) or 0.0)
    remain = cooldown_s - int(now_ts - last)
    return max(0, remain)


# ── 睡眠检测 ─────────────────────────────────────────────────────────────────


def time_to_minutes(t: str) -> int:
    """'HH:MM:SS' 或 'HH:MM' → 分钟数"""
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def detect_sleep(
    heart_rate: list,
    steps: list,
    spo2: list,
    data_lag_min: int | None = None,
    evidence_id: str | None = None,
    poll_time: str | None = None,
) -> tuple[str, str, dict]:
    """
    返回 (state, reason, signals)
    state: 'sleeping' | 'awake' | 'uncertain' | 'unknown'
    signals: 所有中间信号的原始值，用于事后分析
    """
    now_min = datetime.now().hour * 60 + datetime.now().minute
    signals = {
        "spo2_minutes_ago": None,
        "zero_steps_count": 0,  # 最近多少分钟步数为零
        "steps_window": 20,  # 判断窗口（分钟）
        "sustained_zero_min": 0,  # 持续零步数时长（分钟）
        "hr_avg": None,
        "hr_range": None,
        "hr_min": None,
        "hr_max": None,
        "hr_window": 20,
        "triggered": [],  # 触发了哪些信号
        "sleep_prob": None,  # 最终睡眠概率（0-1）
        "prob_source": None,  # ml | heuristic
        "data_lag_min": data_lag_min,
        "evidence_id": evidence_id,
        "poll_time": poll_time,
        "hr_delta_baseline": None,  # v2：当前HR - 个人2h基线（负=低于基线）
        "hr_drop_30min": None,  # v2：当前HR - 30min前HR（负=下降趋势）
    }

    # 信号1：SpO2 30分钟内有新数据
    spo2_recent = False
    if spo2:
        last_min = time_to_minutes(spo2[-1]["time"])
        diff = (now_min - last_min) % (24 * 60)
        signals["spo2_minutes_ago"] = diff
        if diff <= 30:
            spo2_recent = True
            signals["triggered"].append("spo2")

    # 信号2：最近 20 分钟步数为零
    recent_steps = steps[-20:] if len(steps) >= 20 else steps
    zero_count = sum(1 for s in recent_steps if s["value"] == 0)
    signals["zero_steps_count"] = zero_count
    zero_steps = len(recent_steps) >= 20 and zero_count >= 18
    if zero_steps:
        signals["triggered"].append("zero_steps")

    # 信号4：持续零步数时长（从数据末尾往前数连续零步分钟数）
    sustained_zero_min = 0
    for s in reversed(steps):
        if s["value"] == 0:
            sustained_zero_min += 1
        else:
            break
    signals["sustained_zero_min"] = sustained_zero_min
    if sustained_zero_min >= 45:
        signals["triggered"].append("sustained_zero")

    # 信号3：最近 20 分钟心率低且稳定
    recent_hr = heart_rate[-20:] if len(heart_rate) >= 20 else heart_rate
    stable_low_hr = False
    hr_avg = None
    hr_range = None
    if len(recent_hr) >= 10:
        import numpy as np

        vals = [d["value"] for d in recent_hr]
        hr_avg = round(sum(vals) / len(vals), 1)
        hr_range = max(vals) - min(vals)
        hr_std = round(float(np.std(vals)), 2)
        hr_trend = round(float(np.polyfit(np.arange(len(vals)), vals, 1)[0]), 4)
        signals["hr_avg"] = hr_avg
        signals["hr_range"] = hr_range
        signals["hr_min"] = min(vals)
        signals["hr_max"] = max(vals)
        signals["hr_std"] = hr_std
        signals["hr_trend"] = hr_trend
        stable_low_hr = hr_avg < 78 and hr_range < 20
        if stable_low_hr:
            signals["triggered"].append("stable_low_hr")

    # v2 特征：从 _health_history 计算个人化时序基线
    # hr_delta_baseline：当前HR - 过去60~120分钟的均值（个人近期基线）
    # hr_drop_30min：当前HR - 25~35分钟前的均值（短期下降趋势）
    if hr_avg is not None and poll_time is not None:
        try:
            poll_dt = _parse_poll_dt(poll_time)
            if poll_dt is not None:
                baseline_hrs: list[float] = []
                drop_hrs: list[float] = []
                for hist_e in _health_history:
                    hist_dt = _parse_poll_dt(hist_e.get("poll_time"))
                    if hist_dt is None:
                        continue
                    diff_min = (poll_dt - hist_dt).total_seconds() / 60.0
                    hist_hr = hist_e.get("signals", {}).get("hr_avg")
                    if hist_hr is None:
                        continue
                    if 60.0 <= diff_min <= 120.0:
                        baseline_hrs.append(float(hist_hr))
                    if 25.0 <= diff_min <= 35.0:
                        drop_hrs.append(float(hist_hr))
                if baseline_hrs:
                    baseline_mean = sum(baseline_hrs) / len(baseline_hrs)
                    signals["hr_delta_baseline"] = round(hr_avg - baseline_mean, 2)
                if drop_hrs:
                    drop_mean = sum(drop_hrs) / len(drop_hrs)
                    signals["hr_drop_30min"] = round(hr_avg - drop_mean, 2)
        except Exception:
            pass

    if not heart_rate and not steps:
        return "unknown", "数据不足", signals

    # ── 睡眠概率：优先 ML，缺失时用规则估计 ───────────────────────────────────
    with _model_lock:
        model = _sleep_model
    if model is not None:
        prob = sleep_model.predict(model, signals, data_lag_min)
        signals["prob_source"] = "ml"
        signals["ml_prob"] = round(prob, 3)
    else:
        prob = 0.08
        if spo2_recent:
            prob += 0.32
        if zero_steps:
            prob += 0.28
        if stable_low_hr:
            prob += 0.22
        prob += min(0.15, max(0.0, sustained_zero_min / 90.0 * 0.15))
        if hr_avg is not None:
            if hr_avg >= 95:
                prob -= 0.18
            elif hr_avg >= 85:
                prob -= 0.10
            elif hr_avg <= 62:
                prob += 0.12
            elif hr_avg <= 72:
                prob += 0.06
        prob = max(0.0, min(1.0, prob))
        signals["prob_source"] = "heuristic"

    signals["sleep_prob"] = round(prob, 3)

    if prob >= SLEEP_ENTER_THRESHOLD:
        return (
            "sleeping",
            f"高概率睡眠（{prob:.0%}，心率 {hr_avg} bpm，静止 {signals['zero_steps_count']}/20）",
            signals,
        )

    if prob <= SLEEP_EXIT_THRESHOLD:
        reason = (
            f"高概率清醒（{prob:.0%}，心率 {hr_avg} bpm）"
            if hr_avg is not None
            else f"高概率清醒（{prob:.0%}）"
        )
        return "awake", reason, signals

    return "uncertain", f"状态不确定（{prob:.0%}）", signals


def resolve_sleep_state(
    prev_state: str, raw_state: str, raw_reason: str, signals: dict
) -> tuple[str, str]:
    global _high_prob_confirm_count, _low_prob_confirm_count
    global _sleep_enter_candidate_at, _sleep_session_active
    prob = signals.get("sleep_prob")
    if prob is None:
        _high_prob_confirm_count = 0
        _low_prob_confirm_count = 0
        _sleep_enter_candidate_at = None
        return "unknown", "数据不足，暂不决策"

    state, reason = _sleep_viterbi_state.step(float(prob), signals.get("evidence_id"))
    _sleep_session_active = state == "sleeping"
    _sleep_enter_candidate_at = None
    _high_prob_confirm_count = 0
    _low_prob_confirm_count = 0
    return state, reason


def build_agent_policy(state: str, signals: dict) -> dict:
    prob = signals.get("sleep_prob")
    source = signals.get("prob_source")

    if state == "sleeping":
        return {
            "mode": "sleep_protect",
            "action": "queue_non_urgent",
            "send_only": "urgent",
            "delay_min": 60,
            "sleep_prob": prob,
            "prob_source": source,
        }
    if state == "uncertain":
        return {
            "mode": "cautious",
            "action": "delay_low_priority",
            "send_only": "high_priority",
            "delay_min": UNCERTAIN_DELAY_MIN,
            "sleep_prob": prob,
            "prob_source": source,
        }
    if state == "awake":
        return {
            "mode": "normal",
            "action": "send_normal",
            "send_only": "all",
            "delay_min": 0,
            "sleep_prob": prob,
            "prob_source": source,
        }
    return {
        "mode": "cautious",
        "action": "delay_low_priority",
        "send_only": "high_priority",
        "delay_min": UNCERTAIN_DELAY_MIN,
        "sleep_prob": prob,
        "prob_source": source,
    }


def _build_sleep_report_payload(
    days: int = 7, tokens: dict | None = None
) -> tuple[int, dict]:
    """构建最近 N 天睡眠报告，供 API 输出和恢复度分析复用。"""
    days = max(1, min(days, 30))
    t = tokens or valid_tokens()
    if not t:
        return 401, {"error": "未授权，请先完成 Fitbit OAuth"}

    headers = {"Authorization": f"Bearer {t['access_token']}"}
    end_date = date.today() - timedelta(days=1)  # 今天往往不完整
    start_date = end_date - timedelta(days=days - 1)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    sleep_r = req.get(
        f"{API_BASE}/1.2/user/-/sleep/date/{start_str}/{end_str}.json",
        headers=headers,
        timeout=15,
    )
    sleep_by_date: dict[str, list] = {}
    if sleep_r.ok:
        for s in sleep_r.json().get("sleep", []):
            d = s.get("dateOfSleep", "")
            sleep_by_date.setdefault(d, []).append(s)

    hrv_r = req.get(
        f"{API_BASE}/1/user/-/hrv/date/{start_str}/{end_str}.json",
        headers=headers,
        timeout=15,
    )
    hrv_by_date: dict[str, float] = {}
    if hrv_r.ok:
        for h in hrv_r.json().get("hrv", []):
            d = h.get("dateTime", "")
            val = h.get("value", {})
            rmssd = val.get("dailyRmssd") or val.get("deepRmssd")
            f = _safe_float(rmssd)
            if d and f is not None:
                hrv_by_date[d] = round(f, 1)

    days_list: list[dict] = []
    cur = start_date
    while cur <= end_date:
        ds = cur.isoformat()
        sessions = sleep_by_date.get(ds, [])
        hrv_val = hrv_by_date.get(ds)

        if not sessions:
            days_list.append({"date": ds, "no_data": True, "hrv_ms": hrv_val})
            cur += timedelta(days=1)
            continue

        main = next((s for s in sessions if s.get("isMainSleep")), None)
        if main is None:
            main = max(sessions, key=lambda s: s.get("duration", 0))

        duration_min = main.get("duration", 0) // 60000
        levels = main.get("levels", {}).get("summary", {})

        def lvl_min(key: str) -> int:
            return levels.get(key, {}).get("minutes", 0)

        days_list.append(
            {
                "date": ds,
                "start_time": (
                    main.get("startTime", "")[-8:-3] if main.get("startTime") else None
                ),
                "end_time": (
                    main.get("endTime", "")[-8:-3] if main.get("endTime") else None
                ),
                "duration_min": duration_min,
                "efficiency": main.get("efficiency"),
                "minutes_asleep": main.get("minutesAsleep"),
                "minutes_awake": main.get("minutesAwake"),
                "deep_min": lvl_min("deep"),
                "light_min": lvl_min("light"),
                "rem_min": lvl_min("rem"),
                "wake_min": lvl_min("wake"),
                "hrv_ms": hrv_val,
                "no_data": False,
            }
        )
        cur += timedelta(days=1)

    valid = [d for d in days_list if not d.get("no_data")]

    def avg(key: str):
        vals = [_safe_float(d.get(key)) for d in valid]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)

    summary = {
        "days_requested": days,
        "days_with_data": len(valid),
        "avg_duration_min": avg("duration_min"),
        "avg_efficiency": avg("efficiency"),
        "avg_deep_min": avg("deep_min"),
        "avg_rem_min": avg("rem_min"),
        "avg_hrv_ms": avg("hrv_ms"),
    }
    return 200, {"summary": summary, "days": days_list}


def _extract_temp_series(payload: dict) -> list[tuple[str, float]]:
    if not isinstance(payload, dict):
        return []
    candidates: list[dict] = []
    for k, v in payload.items():
        if "temp" in k.lower() and isinstance(v, list):
            candidates.extend([x for x in v if isinstance(x, dict)])
    if not candidates and isinstance(payload.get("temperature"), list):
        candidates.extend(
            [x for x in payload.get("temperature", []) if isinstance(x, dict)]
        )

    out: list[tuple[str, float]] = []
    for item in candidates:
        ds = str(item.get("dateTime") or item.get("date") or "")
        value_block = item.get("value", item)
        val = None
        if isinstance(value_block, dict):
            for key in ("nightlyRelative", "temperature", "value", "temp"):
                val = _safe_float(value_block.get(key))
                if val is not None:
                    break
        else:
            val = _safe_float(value_block)
        if ds and val is not None:
            out.append((ds[:10], round(val, 3)))
    out.sort(key=lambda x: x[0])
    return out


def _get_temperature_signal(tokens: dict | None) -> dict:
    now_ts = time.time()
    if (
        _temperature_cache.get("payload") is not None
        and now_ts - float(_temperature_cache.get("fetched_at", 0.0) or 0.0)
        < TEMPERATURE_CACHE_SECONDS
    ):
        return dict(_temperature_cache["payload"])
    if not tokens:
        return {"available": False, "reason": "no_token"}

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=HEALTH_BASELINE_DAYS - 1)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()
    urls = {
        "skin": f"{API_BASE}/1/user/-/temp/skin/date/{start_str}/{end_str}.json",
        "core": f"{API_BASE}/1/user/-/temp/core/date/{start_str}/{end_str}.json",
    }

    result = {"available": True, "scope_ok": True, "series": {}, "alerts": []}
    for kind, url in urls.items():
        try:
            r = req.get(url, headers=headers, timeout=15)
        except Exception as e:
            result["available"] = False
            result["reason"] = f"request_error:{e}"
            continue
        if r.status_code == 403:
            result["scope_ok"] = False
            result["available"] = False
            result["reason"] = "permission_denied"
            continue
        if not r.ok:
            result["available"] = False
            result["reason"] = f"http_{r.status_code}"
            continue
        series = _extract_temp_series(r.json())
        result["series"][kind] = series
        if not series:
            continue

        latest_day, latest_val = series[-1]
        history_vals = [v for _, v in series[:-1]]
        base = _avg(history_vals)
        sd = _std(history_vals, base)
        # 皮肤温度通常是相对值，阈值偏紧；核心温度是绝对值，阈值偏保守。
        fallback_threshold = 0.8 if kind == "skin" else 0.5
        threshold = max(fallback_threshold, 2.0 * sd)
        if base is None:
            # 数据少时用更保守规则：皮温偏移 >=1.0 才提示；核心温度不做提示。
            abnormal = kind == "skin" and abs(latest_val) >= 1.0
            delta = latest_val if kind == "skin" else 0.0
        else:
            delta = latest_val - base
            abnormal = abs(delta) >= threshold
        if abnormal:
            severity = "high" if abs(delta) >= (threshold * 1.5) else "medium"
            result["alerts"].append(
                {
                    "type": "temperature",
                    "severity": severity,
                    "kind": kind,
                    "date": latest_day,
                    "value": round(latest_val, 2),
                    "baseline_30d": round(base, 2) if base is not None else None,
                    "delta": round(delta, 2),
                    "threshold": round(threshold, 2),
                    "message": f"{'皮肤' if kind == 'skin' else '核心'}温度偏离基线",
                }
            )

    _temperature_cache["fetched_at"] = now_ts
    _temperature_cache["payload"] = dict(result)
    return result


def _get_sleep_recovery_signal(tokens: dict | None) -> dict:
    now_ts = time.time()
    if (
        _sleep_recovery_cache.get("payload") is not None
        and now_ts - float(_sleep_recovery_cache.get("fetched_at", 0.0) or 0.0)
        < SLEEP_RECOVERY_CACHE_SECONDS
    ):
        return dict(_sleep_recovery_cache["payload"])

    status, payload = _build_sleep_report_payload(
        days=HEALTH_BASELINE_DAYS, tokens=tokens
    )
    if status != 200:
        out = {"available": False, "reason": payload.get("error", "unavailable")}
        _sleep_recovery_cache["fetched_at"] = now_ts
        _sleep_recovery_cache["payload"] = dict(out)
        return out

    valid = [d for d in payload.get("days", []) if not d.get("no_data")]
    if len(valid) < 4:
        out = {
            "available": False,
            "reason": "insufficient_days",
            "days_with_data": len(valid),
        }
        _sleep_recovery_cache["fetched_at"] = now_ts
        _sleep_recovery_cache["payload"] = dict(out)
        return out

    recent = valid[-3:]
    baseline = valid[:-3] if len(valid) >= 6 else valid[:-2]
    if len(baseline) < 3:
        out = {
            "available": False,
            "reason": "insufficient_baseline",
            "days_with_data": len(valid),
        }
        _sleep_recovery_cache["fetched_at"] = now_ts
        _sleep_recovery_cache["payload"] = dict(out)
        return out

    def metric_avg(rows: list[dict], key: str) -> float | None:
        vals = [_safe_float(r.get(key)) for r in rows]
        vals = [v for v in vals if v is not None]
        return _avg(vals) if vals else None

    base_dur = metric_avg(baseline, "duration_min")
    base_eff = metric_avg(baseline, "efficiency")
    base_hrv = metric_avg(baseline, "hrv_ms")
    rec_dur = metric_avg(recent, "duration_min")
    rec_eff = metric_avg(recent, "efficiency")
    rec_hrv = metric_avg(recent, "hrv_ms")

    dur_drop = (
        ((base_dur - rec_dur) / base_dur)
        if base_dur and rec_dur and base_dur > 0
        else None
    )
    eff_drop = (
        ((base_eff - rec_eff) / base_eff)
        if base_eff and rec_eff and base_eff > 0
        else None
    )
    hrv_drop = (
        ((base_hrv - rec_hrv) / base_hrv)
        if base_hrv and rec_hrv and base_hrv > 0
        else None
    )

    deteriorating = bool(
        (
            dur_drop is not None
            and eff_drop is not None
            and dur_drop >= 0.18
            and eff_drop >= 0.06
        )
        or (
            hrv_drop is not None
            and dur_drop is not None
            and hrv_drop >= 0.20
            and dur_drop >= 0.10
        )
    )
    severity = (
        "high"
        if deteriorating and ((dur_drop or 0) >= 0.28 or (hrv_drop or 0) >= 0.30)
        else "medium"
    )
    out = {
        "available": True,
        "deteriorating": deteriorating,
        "severity": severity if deteriorating else None,
        "baseline_days": len(baseline),
        "recent_days": len(recent),
        "days_with_data": len(valid),
        "base_duration_min": round(base_dur, 1) if base_dur is not None else None,
        "base_efficiency": round(base_eff, 1) if base_eff is not None else None,
        "base_hrv_ms": round(base_hrv, 1) if base_hrv is not None else None,
        "recent_duration_min": round(rec_dur, 1) if rec_dur is not None else None,
        "recent_efficiency": round(rec_eff, 1) if rec_eff is not None else None,
        "recent_hrv_ms": round(rec_hrv, 1) if rec_hrv is not None else None,
        "duration_drop_ratio": round(dur_drop, 3) if dur_drop is not None else None,
        "efficiency_drop_ratio": round(eff_drop, 3) if eff_drop is not None else None,
        "hrv_drop_ratio": round(hrv_drop, 3) if hrv_drop is not None else None,
    }
    _sleep_recovery_cache["fetched_at"] = now_ts
    _sleep_recovery_cache["payload"] = dict(out)
    return out


def _evaluate_health_context(current_entry: dict, tokens: dict | None) -> dict:
    now_ts = time.time()
    _health_history.append(current_entry)
    rows = _history_last_days(HEALTH_BASELINE_DAYS)
    if not rows:
        return {"alerts": [], "notify_alerts": [], "baseline": {}, "coverage": {}}

    days_covered = len(
        {str(r.get("poll_time", ""))[:10] for r in rows if r.get("poll_time")}
    )
    coverage = {
        "days_covered": days_covered,
        "target_days": HEALTH_BASELINE_DAYS,
        "coverage_ratio": round(days_covered / HEALTH_BASELINE_DAYS, 3),
    }

    hr_vals: list[float] = []
    spo2_vals: list[float] = []
    for r in rows:
        lag = _safe_float(r.get("data_lag_min"))
        if lag is not None and lag > 60:
            continue
        hr = _safe_float(r.get("heart_rate"))
        if hr is not None:
            hr_vals.append(hr)
        spo2 = _safe_float(r.get("spo2"))
        if spo2 is not None:
            spo2_vals.append(spo2)

    hr_base = _median(hr_vals)
    spo2_base = _median(spo2_vals)
    hr_threshold = max(110.0, (hr_base + 50.0) if hr_base is not None else 130.0)
    spo2_threshold = max(
        88.0, min(93.0, (spo2_base - 2.0) if spo2_base is not None else 92.0)
    )

    baseline = {
        "hr_30d_median": round(hr_base, 1) if hr_base is not None else None,
        "hr_threshold": round(hr_threshold, 1),
        "hr_samples": len(hr_vals),
        "spo2_30d_median": round(spo2_base, 1) if spo2_base is not None else None,
        "spo2_threshold": round(spo2_threshold, 1),
        "spo2_samples": len(spo2_vals),
    }
    recent_rows = rows[-HEALTH_LOOKBACK_POLLS:]
    recent_hr_vals: list[float] = []
    recent_spo2_vals: list[float] = []
    for r in recent_rows:
        lag = _safe_float(r.get("data_lag_min"))
        if lag is not None and lag > 60:
            continue
        h = _safe_float(r.get("heart_rate"))
        s = _safe_float(r.get("spo2"))
        if h is not None:
            recent_hr_vals.append(h)
        if s is not None:
            recent_spo2_vals.append(s)

    latest_hr = _safe_float(current_entry.get("heart_rate"))
    latest_spo2 = _safe_float(current_entry.get("spo2"))
    latest_lag = _safe_float(current_entry.get("data_lag_min"))
    latest_spo2_lag = _safe_float(current_entry.get("spo2_lag_min"))
    latest_zero_steps = int(
        (current_entry.get("signals") or {}).get("zero_steps_count", 0) or 0
    )

    summary_for_agent = {
        "latest": {
            "heart_rate": round(latest_hr, 1) if latest_hr is not None else None,
            "spo2": round(latest_spo2, 1) if latest_spo2 is not None else None,
            "data_lag_min": int(latest_lag) if latest_lag is not None else None,
            "spo2_lag_min": (
                int(latest_spo2_lag) if latest_spo2_lag is not None else None
            ),
            "zero_steps_count": latest_zero_steps,
        },
        "recent_window": {
            "polls": len(recent_rows),
            "hr_avg": round(_avg(recent_hr_vals), 1) if recent_hr_vals else None,
            "hr_max": round(max(recent_hr_vals), 1) if recent_hr_vals else None,
            "spo2_avg": round(_avg(recent_spo2_vals), 1) if recent_spo2_vals else None,
            "spo2_min": round(min(recent_spo2_vals), 1) if recent_spo2_vals else None,
        },
        "baseline_30d": baseline,
        "delta_vs_30d": {
            "heart_rate": (
                round(latest_hr - hr_base, 1)
                if latest_hr is not None and hr_base is not None
                else None
            ),
            "spo2": (
                round(latest_spo2 - spo2_base, 1)
                if latest_spo2 is not None and spo2_base is not None
                else None
            ),
        },
        "coverage": coverage,
    }

    alerts: list[dict] = []

    if len(hr_vals) >= HEALTH_BASELINE_MIN_POINTS:
        high_hr_window = _collect_recent_consecutive(
            rows,
            predicate=lambda e: (
                (_safe_float(e.get("heart_rate")) or 0.0) >= hr_threshold
                and ((_safe_float(e.get("data_lag_min")) or 0.0) <= 45.0)
                and int((e.get("signals") or {}).get("zero_steps_count", 0) or 0) >= 16
            ),
        )
        if high_hr_window:
            current_hr = _safe_float(high_hr_window[-1].get("heart_rate"))
            alerts.append(
                {
                    "type": "high_hr",
                    "severity": (
                        "high" if (current_hr or 0.0) >= hr_threshold + 10 else "medium"
                    ),
                    "message": "持续高心率（低活动状态）",
                    "window_polls": len(high_hr_window),
                    "current_hr": (
                        round(current_hr, 1) if current_hr is not None else None
                    ),
                    "baseline_hr": round(hr_base, 1) if hr_base is not None else None,
                    "threshold_hr": round(hr_threshold, 1),
                }
            )

    sleep_recovery = _get_sleep_recovery_signal(tokens)
    if sleep_recovery.get("available") and sleep_recovery.get("deteriorating"):
        alerts.append(
            {
                "type": "sleep_recovery",
                "severity": str(sleep_recovery.get("severity") or "medium"),
                "message": "睡眠恢复出现下降趋势",
                "baseline_days": sleep_recovery.get("baseline_days"),
                "recent_days": sleep_recovery.get("recent_days"),
                "duration_drop_ratio": sleep_recovery.get("duration_drop_ratio"),
                "efficiency_drop_ratio": sleep_recovery.get("efficiency_drop_ratio"),
                "hrv_drop_ratio": sleep_recovery.get("hrv_drop_ratio"),
            }
        )

    temperature = _get_temperature_signal(tokens)
    latest_temp: dict[str, dict | None] = {}
    for kind, series in (temperature.get("series") or {}).items():
        if series:
            day, val = series[-1]
            latest_temp[kind] = {"date": day, "value": val}
        else:
            latest_temp[kind] = None
    for t_alert in temperature.get("alerts", []):
        alerts.append(dict(t_alert))

    notify_alerts: list[dict] = []
    for a in alerts:
        a_type = str(a.get("type") or "")
        remain = _cooldown_remaining_sec(a_type, now_ts)
        a["cooldown_remaining_sec"] = remain
        a["notify"] = remain == 0
        if remain == 0:
            _health_alert_last_notify[a_type] = now_ts
            notify_alerts.append(a)

    return {
        "alerts": alerts,
        "notify_alerts": notify_alerts,
        "baseline": baseline,
        "coverage": coverage,
        "summary_for_agent": summary_for_agent,
        "sleep_recovery": sleep_recovery,
        "temperature": {
            "available": temperature.get("available"),
            "scope_ok": temperature.get("scope_ok", True),
            "reason": temperature.get("reason"),
            "latest": latest_temp,
        },
    }


def _build_log_entry(
    state: str, reason: str, data: dict, changed: bool = False
) -> dict:
    meta = data.get("data_meta", {})
    return {
        "poll_time": meta.get(
            "poll_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ),
        "data_time": meta.get("latest_hr_time"),  # 数据实际时间
        "data_lag_min": meta.get("data_lag_min"),  # 延迟分钟数
        "spo2_time": meta.get("latest_sleep_spo2_time"),
        "spo2_lag_min": meta.get("spo2_lag_min"),
        "state": state,
        "changed": changed,
        "reason": reason,
        # 摘要
        "heart_rate": data["summary"].get("heart_rate"),
        "steps_today": data["summary"].get("steps"),
        "spo2": data["summary"].get("spo2"),
        "policy": data.get("agent_policy", {}),
        "sleep_prob": (data.get("signals", {}) or {}).get("sleep_prob"),
        # 原始判断信号
        "signals": data.get("signals", {}),
        # 健康上下文（异常确认结果）
        "health_context": data.get("health_context", {}),
    }


def log_state_change(
    state: str, reason: str, data: dict, changed: bool = False
) -> dict:
    entry = _build_log_entry(state, reason, data, changed)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if changed:
        print(f"[状态变化] → {state}  原因: {reason}")
    return entry


# ── Fitbit 数据获取 ──────────────────────────────────────────────────────────


def fetch_data() -> dict | None:
    t = valid_tokens()
    if not t:
        return None

    headers = {"Authorization": f"Bearer {t['access_token']}"}
    global _last_fetch_day, _crossday_hr_ref_tail, _crossday_steps_ref_tail
    today = date.today().isoformat()
    result = {"heart_rate": [], "steps": [], "spo2": []}

    def get_with_retry(url, retries=3, delay=10):
        for i in range(retries):
            try:
                return req.get(url, headers=headers, timeout=15)
            except Exception as e:
                if i < retries - 1:
                    print(f"请求失败，{delay}秒后重试（{i + 1}/{retries - 1}）: {e}")
                    time.sleep(delay)
                else:
                    raise

    def _warn_non_ok(name: str, resp: req.Response) -> None:
        body = (resp.text or "").strip().replace("\n", " ")
        if len(body) > 220:
            body = body[:220] + "..."
        print(f"[fitbit] {name} 请求失败 status={resp.status_code} body={body}")

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # 心率 1min 粒度：今天数据为空时 fallback 昨天（跨凌晨同步延迟）
    r = get_with_retry(
        f"{API_BASE}/1/user/-/activities/heart/date/{today}/1d/1min.json"
    )
    if r.ok:
        result["heart_rate"] = (
            r.json().get("activities-heart-intraday", {}).get("dataset", [])
        )
    else:
        _warn_non_ok("heart", r)
    if not result["heart_rate"]:
        r2 = get_with_retry(
            f"{API_BASE}/1/user/-/activities/heart/date/{yesterday}/1d/1min.json"
        )
        if r2.ok:
            result["heart_rate"] = (
                r2.json().get("activities-heart-intraday", {}).get("dataset", [])
            )
            print(f"[fitbit] 今日心率数据为空，使用昨日数据 fallback（{yesterday}）")

    # 步数 1min 粒度：今天数据为空时 fallback 昨天
    r = get_with_retry(
        f"{API_BASE}/1/user/-/activities/steps/date/{today}/1d/1min.json"
    )
    if r.ok:
        result["steps"] = (
            r.json().get("activities-steps-intraday", {}).get("dataset", [])
        )
    else:
        _warn_non_ok("steps", r)
    if not result["steps"]:
        r2 = get_with_retry(
            f"{API_BASE}/1/user/-/activities/steps/date/{yesterday}/1d/1min.json"
        )
        if r2.ok:
            result["steps"] = (
                r2.json().get("activities-steps-intraday", {}).get("dataset", [])
            )
            print(f"[fitbit] 今日步数数据为空，使用昨日数据 fallback（{yesterday}）")

    # 跨天参考：若日期切换，拼接上一轮缓存尾部，避免 00:00 附近特征断层。
    hr_for_sleep = list(result["heart_rate"])
    steps_for_sleep = list(result["steps"])
    is_day_switched = _last_fetch_day is not None and _last_fetch_day != today
    if is_day_switched:
        if _crossday_hr_ref_tail:
            hr_for_sleep = list(_crossday_hr_ref_tail[-120:]) + hr_for_sleep
        if _crossday_steps_ref_tail:
            steps_for_sleep = list(_crossday_steps_ref_tail[-180:]) + steps_for_sleep

    # 血氧 intraday
    r = get_with_retry(f"{API_BASE}/1/user/-/spo2/date/{today}/all.json")
    if r.ok:
        minutes = r.json().get("minutes", [])
        result["spo2"] = [
            {"time": m["minute"].split("T")[-1][:8], "value": round(m["value"], 1)}
            for m in minutes
        ]
    else:
        _warn_non_ok("spo2", r)

    # 摘要
    if not result["heart_rate"] and not result["steps"] and not result["spo2"]:
        with data_lock:
            previous = copy.deepcopy(latest_data)
        has_previous = any(
            previous.get(key)
            for key in ("heart_rate", "steps", "spo2")
        )
        if has_previous:
            poll_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            previous["data_meta"] = dict(previous.get("data_meta") or {})
            previous["data_meta"]["poll_time"] = poll_time_str
            previous["data_meta"]["stale_reason"] = "fitbit_api_unavailable"
            previous["data_meta"]["evidence_id"] = poll_time_str
            previous["stale"] = True
            print("[fitbit] 本轮无新数据，保留上一条有效快照")
            return previous

    hr_val = result["heart_rate"][-1]["value"] if result["heart_rate"] else None
    hr_time = result["heart_rate"][-1]["time"] if result["heart_rate"] else None
    steps_sum = int(sum(s["value"] for s in result["steps"]))
    spo2_val = result["spo2"][-1]["value"] if result["spo2"] else None
    spo2_time = result["spo2"][-1]["time"] if result["spo2"] else None
    result["summary"] = {"heart_rate": hr_val, "steps": steps_sum, "spo2": spo2_val}

    # 数据延迟计算
    data_lag_min = None
    if hr_time:
        now_min = datetime.now().hour * 60 + datetime.now().minute
        data_min = time_to_minutes(hr_time)
        data_lag_min = (now_min - data_min) % (24 * 60)
    spo2_lag_min = None
    if spo2_time:
        now_min = datetime.now().hour * 60 + datetime.now().minute
        spo2_min = time_to_minutes(spo2_time)
        spo2_lag_min = (now_min - spo2_min) % (24 * 60)
    poll_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    evidence_id = f"{poll_time_str[:10]} {hr_time}" if hr_time else poll_time_str
    result["data_meta"] = {
        "latest_hr_time": hr_time,  # 最新心率数据的时间戳
        "data_lag_min": data_lag_min,  # 数据延迟（分钟）
        "latest_sleep_spo2_time": spo2_time,
        "spo2_lag_min": spo2_lag_min,
        "poll_time": poll_time_str,
        "evidence_id": evidence_id,
    }

    # 睡眠状态检测
    raw_state, raw_reason, signals = detect_sleep(
        hr_for_sleep,
        steps_for_sleep,
        result["spo2"],
        data_lag_min=data_lag_min,
        evidence_id=evidence_id,
        poll_time=poll_time_str,
    )
    result["sleep"] = {
        "state": raw_state,
        "reason": raw_reason,
        "raw_state": raw_state,
        "raw_reason": raw_reason,
        "since": None,
    }
    result["signals"] = signals

    # 更新跨天参考缓存（保存当前轮询尾部）。
    _last_fetch_day = today
    _crossday_hr_ref_tail = list(result["heart_rate"][-180:])
    _crossday_steps_ref_tail = list(result["steps"][-240:])

    return result


# ── WebSocket 广播 ───────────────────────────────────────────────────────────


async def broadcast(payload: dict):
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


def push(payload: dict):
    if main_loop:
        asyncio.run_coroutine_threadsafe(broadcast(payload), main_loop)


# ── 轮询线程 ─────────────────────────────────────────────────────────────────


def polling_loop():
    while True:
        try:
            data = fetch_data()
            if data:
                tokens = valid_tokens()
                global _last_sleep_state
                raw_state = data["sleep"]["state"]
                raw_reason = data["sleep"]["reason"]
                new_state, new_reason = resolve_sleep_state(
                    _last_sleep_state, raw_state, raw_reason, data.get("signals", {})
                )
                data["sleep"]["state"] = new_state
                data["sleep"]["reason"] = new_reason
                data["agent_policy"] = build_agent_policy(
                    new_state, data.get("signals", {})
                )
                _current_log_entry = _build_log_entry(
                    new_state,
                    new_reason,
                    data,
                    changed=(new_state != _last_sleep_state),
                )
                data["health_context"] = _evaluate_health_context(
                    _current_log_entry, tokens=tokens
                )
                _update_health_event_engines(_current_log_entry, list(_health_history))
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prev_state = _last_sleep_state
                payload = None

                # sleeping -> awake：可能刚醒，乐观拉一次标注尝试重训。
                # 有冷却保护（REPORT_RETRAIN_COOLDOWN），误判也无副作用。
                if prev_state == "sleeping" and new_state == "awake":
                    trigger_retrain_async("sleep_to_awake")

                with data_lock:
                    changed = new_state != _last_sleep_state
                    if changed:
                        data["sleep"]["since"] = now_str
                        _last_sleep_state = new_state
                    else:
                        data["sleep"]["since"] = latest_data["sleep"].get("since")

                    latest_data.update(data)
                    latest_data["last_updated"] = datetime.now().strftime("%H:%M:%S")
                    payload = dict(latest_data)

                # 每次轮询都记录（放到锁外，避免阻塞 /api/data）
                log_state_change(new_state, data["sleep"]["reason"], data, changed)

                s = data["summary"]
                sl = data["sleep"]
                last_updated = (payload or {}).get(
                    "last_updated", datetime.now().strftime("%H:%M:%S")
                )
                print(
                    f"[{last_updated}] 心率:{s['heart_rate']} bpm  步数:{s['steps']}  最近睡眠血氧:{s['spo2']}%  状态:{sl['state']}"
                )
                notify_alerts = (data.get("health_context", {}) or {}).get(
                    "notify_alerts", []
                )
                if notify_alerts:
                    brief = ", ".join(
                        f"{a.get('type')}({a.get('severity')})"
                        for a in notify_alerts[:3]
                    )
                    print(f"[健康提醒] {brief}")
                if payload is None:
                    with data_lock:
                        payload = dict(latest_data)
                push(payload)
        except Exception as e:
            print(f"轮询错误: {e}")
        # 等待下次触发或超时自动触发
        poll_event.wait(timeout=POLL_INTERVAL)
        poll_event.clear()


# ── FastAPI 启动 ─────────────────────────────────────────────────────────────

# ── 路由 ─────────────────────────────────────────────────────────────────────


@app.get("/auth/start")
def auth_start():
    challenge = make_pkce()
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.get("/oauth/callback")
def oauth_callback(code: str = None, error: str = None):
    if error or not code:
        return HTMLResponse("<h2>授权失败</h2>")
    r = req.post(
        TOKEN_URL,
        headers={
            "Authorization": basic_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": _code_verifier,
        },
    )
    r.raise_for_status()
    tokens = r.json()
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 28800)
    save_tokens(tokens)
    return RedirectResponse("/")


@app.get("/api/data")
def api_data():
    with data_lock:
        return JSONResponse(dict(latest_data))


def _build_sleep_24h_payload(now: datetime | None = None) -> dict:
    now_dt = now or datetime.now()
    start_dt = now_dt - timedelta(hours=24)
    points: list[tuple[datetime, str]] = []

    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            dt = _parse_poll_dt(str(obj.get("poll_time", "")))
            if dt is None or dt < start_dt:
                continue
            state = str(obj.get("state") or "unknown").strip().lower() or "unknown"
            points.append((dt, state))

    points.sort(key=lambda x: x[0])
    if not points:
        return {}

    segments: list[dict] = []
    for i, (seg_start_dt, state) in enumerate(points):
        seg_end_dt = points[i + 1][0] if i + 1 < len(points) else now_dt
        if seg_end_dt <= seg_start_dt:
            continue
        seg_start_dt = max(seg_start_dt, start_dt)
        if seg_end_dt <= seg_start_dt:
            continue
        if segments and segments[-1]["state"] == state and segments[-1]["end"] == seg_start_dt.strftime("%Y-%m-%d %H:%M:%S"):
            segments[-1]["end"] = seg_end_dt.strftime("%Y-%m-%d %H:%M:%S")
            continue
        segments.append(
            {
                "state": state,
                "start": seg_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "end": seg_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    state_map: dict[str, str] = {}
    for seg in segments:
        key = f"{seg['start'][11:16]}-{seg['end'][11:16]}"
        state_map[key] = seg["state"]

    return state_map


def _collapse_external_sleep_state(state: str | None) -> str:
    s = str(state or "unknown").strip().lower() or "unknown"
    if s == "uncertain":
        return "sleeping"
    return s


def _build_external_sleep_payload(
    sleep: dict, signals: dict, meta: dict
) -> dict[str, object]:
    raw_state = str(sleep.get("state", "unknown"))
    return {
        "state": _collapse_external_sleep_state(raw_state),
        "raw_state": raw_state,
        "prob": signals.get("sleep_prob"),
        "prob_source": signals.get("prob_source", "unavailable"),
        "data_lag_min": meta.get("data_lag_min"),
    }


def _build_external_sleep_24h_payload(now: datetime | None = None) -> dict:
    raw = _build_sleep_24h_payload(now=now)
    return {k: _collapse_external_sleep_state(v) for k, v in raw.items()}


@app.get("/api/agent")
def api_agent():
    """供 Akashic agent 专用：睡眠状态 + 健康事件队列，不含原始数字。"""
    with data_lock:
        sleep = latest_data.get("sleep", {}) or {}
        signals = latest_data.get("signals", {}) or {}
        meta = latest_data.get("data_meta", {}) or {}
        last_updated = latest_data.get("last_updated", "")
    health_events = _current_health_events()
    return JSONResponse(
        {
            "sleep": _build_external_sleep_payload(sleep, signals, meta),
            "health_events": health_events,
            "sleep_24h": _build_external_sleep_24h_payload(),
            "last_updated": last_updated,
        }
    )


@app.get("/api/tool/fitbit_health_snapshot")
def api_tool_fitbit_health_snapshot():
    """对齐 agent/tools/fitbit.py 的 fitbit_health_snapshot JSON 结构。"""
    with data_lock:
        summary = latest_data.get("summary", {}) or {}
        signals = latest_data.get("signals", {}) or {}
        meta = latest_data.get("data_meta", {}) or {}
        sleep = latest_data.get("sleep", {}) or {}
        last_updated = latest_data.get("last_updated", "")
    return JSONResponse(
        {
            "available": True,
            "data_lag_min": meta.get("data_lag_min"),
            "spo2_lag_min": meta.get("spo2_lag_min"),
            "last_updated": last_updated,
            "heart_rate": summary.get("heart_rate"),
            "spo2": summary.get("spo2"),
            "latest_sleep_spo2": summary.get("spo2"),
            "latest_sleep_spo2_time": meta.get("latest_sleep_spo2_time"),
            "steps": summary.get("steps"),
            "sleep_state": _collapse_external_sleep_state(sleep.get("state", "unknown")),
            "sleep_state_raw": sleep.get("state", "unknown"),
            "sleep_prob": signals.get("sleep_prob"),
            "sleep_24h": _build_external_sleep_24h_payload(),
        }
    )


@app.post("/api/agent/acknowledge/{event_id}")
def api_agent_acknowledge(event_id: str):
    """LLM 发送健康消息后调用，标记事件已处理。"""
    ok = _ack_health_event(event_id)
    return JSONResponse({"acknowledged": ok})


@app.get("/api/sleep_log")
def api_sleep_log(
    limit: int = 50, changed_only: bool = False, state: str | None = None
):
    if not LOG_FILE.exists():
        return JSONResponse([])
    safe_limit = max(1, min(limit, 5000))
    state_filter = (state or "").strip().lower()
    allowed_states = {"sleeping", "awake", "uncertain", "unknown"}
    if state_filter and state_filter not in allowed_states:
        state_filter = ""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return JSONResponse([])
    entries: list[dict] = []
    for line in reversed(lines):  # 最新在前
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if changed_only and not bool(row.get("changed")):
            continue
        if state_filter and str(row.get("state", "")).lower() != state_filter:
            continue
        entries.append(row)
        if len(entries) >= safe_limit:
            break
    return JSONResponse(entries)


@app.get("/api/sleep_report")
def api_sleep_report(days: int = 7):
    """
    返回最近 N 天的睡眠详情 + HRV 汇总，供 agent tool 调用。
    days 最大 30，受限于 Fitbit API 配额。
    """
    status, payload = _build_sleep_report_payload(days=days, tokens=None)
    return JSONResponse(payload, status_code=status)


@app.get("/api/refresh")
def api_refresh():
    poll_event.set()
    return {"status": "refreshing"}


@app.get("/api/retrain")
def api_retrain():
    if not trigger_retrain_async("manual_api"):
        return {"status": "disabled"}
    return {"status": "retraining"}


@app.get("/api/sleep_diff/build")
def api_sleep_diff_build(force: bool = False):
    result = _build_sleep_diff_if_needed(force=force)
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        with data_lock:
            await websocket.send_json(dict(latest_data))
        while True:
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        ws_clients.discard(websocket)


@app.get("/")
def index():
    if not TOKEN_FILE.exists():
        return HTMLResponse(AUTH_PAGE)
    html_file = STATIC_DIR / "index.html"
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/sleep-diff")
def sleep_diff_page(force: bool = False):
    result = _build_sleep_diff_if_needed(force=force)
    if not result.get("ok"):
        err = result.get("error", "unknown_error")
        return HTMLResponse(f"<h3>sleep diff 生成失败: {err}</h3>", status_code=500)
    return HTMLResponse(SLEEP_DIFF_REPORT_FILE.read_text(encoding="utf-8"))


AUTH_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Fitbit 仪表板 - 授权</title>
<style>
  body { margin:0; display:flex; align-items:center; justify-content:center; min-height:100vh;
         background:#0f172a; font-family:-apple-system,sans-serif; color:#f1f5f9; }
  .card { text-align:center; padding:48px; background:#1e293b; border-radius:16px; }
  h2 { font-size:1.8rem; margin-bottom:8px; }
  p  { color:#94a3b8; margin-bottom:32px; }
  a  { display:inline-block; padding:12px 32px; background:#0ea5e9;
       color:white; text-decoration:none; border-radius:8px; font-weight:600;
       transition:background .2s; }
  a:hover { background:#0284c7; }
</style>
</head>
<body>
<div class="card">
  <h2>Fitbit 健康仪表板</h2>
  <p>首次使用需要授权 Fitbit 账号</p>
  <a href="/auth/start">授权 Fitbit</a>
</div>
</body>
</html>"""

if __name__ == "__main__":
    print(f"服务器启动: http://{SERVER_HOST}:{SERVER_PORT}")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level=SERVER_LOG_LEVEL)
