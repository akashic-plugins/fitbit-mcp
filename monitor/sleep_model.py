"""
睡眠检测 ML 模块
- 标注管理：从 Fitbit Sleep API 自动拉取，存入 sleep_labels.json
- 特征提取：hr_avg / hr_range / zero_steps_ratio / spo2_recent
- 训练：Logistic Regression（sklearn）
- 预测：返回睡眠概率，含数据延迟修正
"""

import json
import numpy as np
from datetime import datetime, date, timedelta
from paths import DATA_DIR

try:
    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

LOG_FILE = DATA_DIR / "sleep_log.jsonl"
LABELS_FILE = DATA_DIR / "sleep_labels.json"
MODEL_FILE = DATA_DIR / "sleep_model.pkl"
FEATURE_NAMES = [
    "hr_avg",
    "hr_range",
    "hr_std",
    "hr_trend",
    "zero_ratio",
    "sustained_norm",
    "lag_norm",
    "hr_delta",
    "hr_drop",
    "stillness",
    "hr_stillness",
]


# ── 标注管理 ──────────────────────────────────────────────────────────────────


def load_labels() -> list:
    if not LABELS_FILE.exists():
        return []
    return json.loads(LABELS_FILE.read_text(encoding="utf-8"))


def _save_labels(labels: list):
    LABELS_FILE.write_text(json.dumps(labels, indent=2, ensure_ascii=False))


def _parse_dt(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s}")


def add_sleep_window(start_iso: str, end_iso: str) -> bool:
    """添加一条睡眠窗口标注，自动去重，返回是否新增。"""
    labels = load_labels()
    for lb in labels:
        if lb["start"] == start_iso:
            return False
    labels.append({"start": start_iso, "end": end_iso})
    _save_labels(labels)
    print(f"  [标注] {start_iso} → {end_iso}", flush=True)
    return True


def is_sleeping(poll_time_str: str, windows: list) -> bool:
    try:
        poll_dt = _parse_dt(poll_time_str)
    except ValueError:
        return False
    for w in windows:
        try:
            if _parse_dt(w["start"]) <= poll_dt <= _parse_dt(w["end"]):
                return True
        except (ValueError, KeyError):
            continue
    return False


# ── 特征提取 ──────────────────────────────────────────────────────────────────


def _as_float(v, default: float = 0.0) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return default


def extract_features(entry: dict) -> list | None:
    sig = entry.get("signals", {})
    hr_avg = sig.get("hr_avg")
    hr_range = sig.get("hr_range")
    if hr_avg is None or hr_range is None:
        return None

    steps_win = _as_float(sig.get("steps_window"), 20.0) or 20.0
    zero_ratio = _as_float(sig.get("zero_steps_count")) / steps_win
    sustained = _as_float(sig.get("sustained_zero_min"))
    lag = sig.get("data_lag_min")
    lag_norm = min(1.0, float(lag) / 20.0) if isinstance(lag, (int, float)) else 0.0
    stillness = zero_ratio * min(1.0, sustained / 60.0)
    hr_avg_f = float(hr_avg)

    return [
        hr_avg_f,
        float(hr_range),
        _as_float(sig.get("hr_std")),
        _as_float(sig.get("hr_trend")),
        zero_ratio,
        min(2.0, sustained / 60.0),
        lag_norm,
        _as_float(sig.get("hr_delta_baseline")),
        _as_float(sig.get("hr_drop_30min")),
        stillness,
        hr_avg_f * stillness,
    ]


# ── 训练 ─────────────────────────────────────────────────────────────────────


def train() -> object | None:
    return train_binary_from_labels()


def _iter_entries() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _augment_temporal_features(entries: list[dict]) -> list[dict]:
    rows: list[tuple[datetime, dict]] = []
    for e in entries:
        try:
            ts = _parse_dt(str(e.get("poll_time", "")))
        except ValueError:
            continue
        sig = e.get("signals", {})
        if sig.get("hr_avg") is None:
            continue
        rows.append((ts, e))
    rows.sort(key=lambda x: x[0])

    hist: list[tuple[datetime, float]] = []
    for ts, e in rows:
        sig = e.setdefault("signals", {})
        hr_avg = float(sig["hr_avg"])
        hist = [(old_ts, hr) for old_ts, hr in hist if (ts - old_ts).total_seconds() <= 180 * 60]
        baseline_vals = [
            hr for old_ts, hr in hist if 60 <= (ts - old_ts).total_seconds() / 60 <= 180
        ]
        drop_vals = [
            hr for old_ts, hr in hist if 25 <= (ts - old_ts).total_seconds() / 60 <= 40
        ]
        if sig.get("hr_delta_baseline") is None and baseline_vals:
            sig["hr_delta_baseline"] = round(hr_avg - float(np.median(baseline_vals)), 2)
        if sig.get("hr_drop_30min") is None and drop_vals:
            sig["hr_drop_30min"] = round(hr_avg - float(np.median(drop_vals)), 2)
        hist.append((ts, hr_avg))
    return [e for _, e in rows]


def _load_windows() -> list[tuple[datetime, datetime]]:
    windows = []
    for w in load_labels():
        try:
            s = _parse_dt(str(w["start"]))
            e = _parse_dt(str(w["end"]))
        except Exception:
            continue
        if e > s:
            windows.append((s, e))
    windows.sort(key=lambda x: x[0])
    return windows


def _distance_to_window_edge(ts: datetime, windows: list[tuple[datetime, datetime]]) -> float:
    nearest = None
    for s, e in windows:
        if s <= ts <= e:
            d = min((ts - s).total_seconds(), (e - ts).total_seconds()) / 60.0
        elif ts < s:
            d = (s - ts).total_seconds() / 60.0
        else:
            d = (ts - e).total_seconds() / 60.0
        nearest = d if nearest is None else min(nearest, d)
    return float(nearest) if nearest is not None else 1e9


def train_binary_from_labels(
    max_lag_min: int = 8,
    pos_edge_exclude_min: int = 20,
    neg_edge_margin_min: int = 45,
    sleep_sample_weight: float = 1.15,
    min_samples: int = 30,
) -> object | None:
    if not HAS_SKLEARN:
        print("sklearn 未安装：pip install scikit-learn joblib")
        return None
    windows = _load_windows()
    if not windows:
        print("暂无睡眠标注，跳过训练")
        return None

    X, y, sample_w = [], [], []
    for e in _augment_temporal_features(_iter_entries()):
        poll_time = str(e.get("poll_time", ""))
        try:
            ts = _parse_dt(poll_time)
        except ValueError:
            continue
        lag = e.get("data_lag_min")
        if isinstance(lag, (int, float)) and lag > max_lag_min:
            continue
        feat = extract_features(e)
        if feat is None:
            continue
        dist = _distance_to_window_edge(ts, windows)
        in_sleep = any(s <= ts <= e2 for s, e2 in windows)
        if in_sleep and dist < pos_edge_exclude_min:
            continue
        if (not in_sleep) and dist < neg_edge_margin_min:
            continue
        X.append(feat)
        y.append(1 if in_sleep else 0)
        sample_w.append(max(1.0, float(sleep_sample_weight)) if in_sleep else 1.0)

    if len(X) < max(min_samples, 10):
        print(f"数据量不足（{len(X)} 条），跳过训练")
        return None
    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=int)
    w_arr = np.array(sample_w, dtype=float)
    pos_n = int(y_arr.sum())
    neg_n = int(len(y_arr) - pos_n)
    if pos_n < 10 or neg_n < 10:
        print(f"正负样本不足（睡眠 {pos_n} / 清醒 {neg_n}），跳过训练")
        return None

    model = HistGradientBoostingClassifier(
        max_iter=80,
        learning_rate=0.06,
        max_leaf_nodes=15,
        l2_regularization=0.08,
        random_state=42,
    )
    model.fit(X_arr, y_arr, sample_weight=w_arr)
    pred = model.predict(X_arr)
    acc = float((pred == y_arr).mean())
    print(
        f"训练数据：{len(X_arr)} 条（睡眠 {pos_n} / 清醒 {neg_n}）"
        f"  max_lag={max_lag_min} edge=+/-{pos_edge_exclude_min}/{neg_edge_margin_min}"
    )
    print(f"训练集准确率：{acc:.1%}")
    joblib.dump(model, MODEL_FILE)
    print(f"模型已保存 → {MODEL_FILE}")
    return model


# ── 加载与预测 ────────────────────────────────────────────────────────────────


def load_model() -> object | None:
    if not HAS_SKLEARN or not MODEL_FILE.exists():
        return None
    try:
        m = joblib.load(MODEL_FILE)
        print("已加载睡眠模型")
        return m
    except Exception as e:
        print(f"模型加载失败：{e}")
        return None


def predict(model, signals: dict, data_lag_min: int | None = None) -> float:
    """
    返回睡眠概率 0-1。
    data_lag_min > 20 时对概率施加轻微惩罚（数据较陈旧）。
    """
    feat = extract_features({"signals": signals})
    if feat is None:
        return 0.0

    prob = float(model.predict_proba([feat])[0][1])

    # 数据延迟修正：超过 10 min 的陈旧数据，概率向 0.5 压缩
    # steps 数据每分钟追加零值会造成假膨胀，HR 数据滞后时应降低置信度
    if data_lag_min is not None and data_lag_min > 10:
        freshness = max(0.0, min(1.0, 10 / data_lag_min))  # 1.0=新鲜, 趋近0=极旧
        prob = prob * (0.85 + 0.15 * freshness)

    return prob


class OnlineViterbiState:
    def __init__(
        self,
        sleep_bias: float = 0.16,
        switch_penalty: float = 1.6,
        wake_to_sleep_penalty: float = 0.15,
    ):
        self.sleep_bias = float(sleep_bias)
        self.switch_penalty = float(switch_penalty)
        self.wake_to_sleep_penalty = float(wake_to_sleep_penalty)
        self.awake_score = 0.0
        self.sleep_score = 0.0
        self.state = "awake"
        self.last_evidence_id: str | None = None

    def step(self, prob: float, evidence_id: str | None = None) -> tuple[str, str]:
        if evidence_id is not None and evidence_id == self.last_evidence_id:
            return self.state, self._reason(prob)
        if evidence_id is not None:
            self.last_evidence_id = evidence_id

        eps = 1e-6
        p_sleep = min(1.0 - eps, max(eps, float(prob) + self.sleep_bias))
        p_wake = min(1.0 - eps, max(eps, 1.0 - float(prob)))
        emit_awake = float(np.log(p_wake))
        emit_sleep = float(np.log(p_sleep))

        next_awake = max(
            self.awake_score,
            self.sleep_score - self.switch_penalty,
        ) + emit_awake
        next_sleep = max(
            self.sleep_score,
            self.awake_score - self.switch_penalty - self.wake_to_sleep_penalty,
        ) + emit_sleep
        self.awake_score = next_awake
        self.sleep_score = next_sleep
        self.state = "sleeping" if self.sleep_score > self.awake_score else "awake"
        return self.state, self._reason(prob)

    def _reason(self, prob: float) -> str:
        gap = abs(self.sleep_score - self.awake_score)
        label = "睡眠" if self.state == "sleeping" else "清醒"
        return f"Viterbi 判定{label}（睡眠概率 {prob:.0%}，分差 {gap:.2f}）"


# ── Fitbit 自动标注 ───────────────────────────────────────────────────────────


def fetch_and_label(tokens: dict, target_date: str | None = None) -> bool:
    """
    从 Fitbit Sleep API 拉取指定日期的睡眠记录并写入标注文件。
    target_date: 'YYYY-MM-DD'，默认昨天。
    返回是否新增了标注。
    """
    import requests

    if target_date is None:
        target_date = (date.today() - timedelta(days=1)).isoformat()

    r = requests.get(
        f"https://api.fitbit.com/1.2/user/-/sleep/date/{target_date}.json",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=15,
    )
    if not r.ok:
        print(f"Fitbit 睡眠 API 失败：{r.status_code}")
        return False

    sleeps = r.json().get("sleep", [])
    if not sleeps:
        print(f"{target_date} 无睡眠记录")
        return False

    added = False
    for s in sleeps:
        dur_min = s["duration"] // 60000
        if add_sleep_window(s["startTime"], s["endTime"]):
            print(
                f"  睡眠：{s['startTime']} → {s['endTime']}  "
                f"{dur_min}分钟  效率:{s['efficiency']}%"
            )
            added = True
    return added
