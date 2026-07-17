function number(value, digits = 0) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(Number(value));
}

export function durationLabel(value) {
  if (value === null || value === undefined) return "—";
  const minutes = Math.max(0, Math.round(Number(value)));
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest} 分钟`;
  return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分`;
}

export function sleepStateLabel(value) {
  if (value === "sleeping") return "睡眠中";
  if (value === "awake") return "清醒";
  if (value === "uncertain") return "状态波动";
  return "等待数据";
}

export function normalizeTimeline(segments) {
  if (!Array.isArray(segments) || segments.length === 0) return [];
  let remaining = 24 * 60;
  const recent = [];
  for (let index = segments.length - 1; index >= 0 && remaining > 0; index -= 1) {
    const segment = segments[index];
    const duration = Math.max(0, Math.round(Number(segment.duration_min)));
    if (duration === 0) continue;
    const keptDuration = Math.min(duration, remaining);
    recent.unshift({ ...segment, duration_min: keptDuration });
    remaining -= keptDuration;
  }
  if (remaining > 0) {
    recent.unshift({ range: "无数据", state: "unknown", duration_min: remaining });
  }
  return recent;
}

export function timelineLabel(segments) {
  const totals = { sleeping: 0, awake: 0, unknown: 0 };
  for (const segment of segments) {
    totals[segment.state] += Number(segment.duration_min);
  }
  return [
    `最近 24 小时：睡眠 ${durationLabel(totals.sleeping)}`,
    `清醒 ${durationLabel(totals.awake)}`,
    `无数据 ${durationLabel(totals.unknown)}`,
  ].join("，");
}

function dayLabel(value) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return String(value || "—");
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", weekday: "short" }).format(date);
}

function sleepDayRow(item) {
  const row = document.createElement("li");
  row.className = "fitbit-mobile-day";
  const date = document.createElement("time");
  date.dateTime = String(item.date || "");
  date.textContent = dayLabel(item.date);
  const duration = document.createElement("strong");
  duration.textContent = item.no_data ? "没有数据" : durationLabel(item.duration_min);
  const detail = document.createElement("span");
  detail.textContent = item.no_data
    ? "Fitbit 当天没有可用睡眠记录"
    : `深睡 ${durationLabel(item.deep_min)} · 效率 ${number(item.efficiency)}%`;
  row.append(date, duration, detail);
  return row;
}

function rhythmSegment(item) {
  const segment = document.createElement("span");
  segment.className = item.state;
  segment.style.flex = `${Math.max(1, Number(item.duration_min))} 1 0px`;
  segment.setAttribute("aria-hidden", "true");
  const stateLabel = item.state === "sleeping" ? "睡眠" : item.state === "awake" ? "清醒" : "无数据";
  segment.title = `${item.range} · ${stateLabel}`;
  return segment;
}

function lagLabel(value, prefix = "") {
  if (value === null || value === undefined) return `${prefix}更新时间未知`;
  const lag = Number(value);
  if (!Number.isFinite(lag)) return `${prefix}更新时间未知`;
  return `${prefix}延迟 ${number(lag)} 分钟`;
}

function overallFreshnessLabel(freshness) {
  return `${freshness.last_updated || "刚刚"} 更新 · ${lagLabel(freshness.data_lag_min)}`;
}

function setStatus(status, message, { error = false, retry = false } = {}) {
  status.hidden = false;
  status.classList.toggle("error", error);
  status.querySelector("span").textContent = message;
  status.querySelector("button").hidden = !retry;
}

function errorMessage(error, fallback) {
  return error instanceof Error ? `${fallback}：${error.message}` : fallback;
}

const dashboard = {
  mount(host, context) {
    let active = true;
    host.classList.add("fitbit-mobile");
    host.innerHTML = `
      <div class="fitbit-mobile-status" data-status="current" role="status">
        <span>正在读取当前健康状态…</span>
        <button type="button" hidden>重试当前状态</button>
      </div>
      <div class="fitbit-mobile-current" hidden>
        <p class="fitbit-mobile-freshness"></p>
        <section class="fitbit-mobile-overview" aria-label="当前健康状态">
          <div class="fitbit-mobile-sleep">
            <span>当前状态</span>
            <strong data-current="sleep">等待数据</strong>
            <small data-current="probability"></small>
          </div>
          <dl class="fitbit-mobile-metrics">
            <div class="fitbit-mobile-heart"><dt>心率</dt><dd><strong data-current="heart">—</strong> bpm</dd></div>
            <div class="fitbit-mobile-oxygen">
              <dt>血氧</dt><dd><strong data-current="oxygen">—</strong>%</dd>
              <small data-current="oxygen-freshness"></small>
            </div>
            <div class="fitbit-mobile-steps"><dt>步数</dt><dd><strong data-current="steps">—</strong> 步</dd></div>
          </dl>
        </section>
        <section class="fitbit-mobile-rhythm" aria-labelledby="fitbit-rhythm-title">
          <header><h2 id="fitbit-rhythm-title">最近 24 小时</h2><span>紫色为睡眠</span></header>
          <p class="fitbit-mobile-empty" data-empty="rhythm" hidden>暂无可用睡眠节律</p>
          <div class="fitbit-mobile-timeline" role="img"></div>
        </section>
      </div>
      <section class="fitbit-mobile-week" aria-labelledby="fitbit-week-title">
        <header><h2 id="fitbit-week-title">7 天睡眠</h2><span data-week="coverage"></span></header>
        <div class="fitbit-mobile-status" data-status="history" role="status">
          <span>正在读取睡眠历史…</span>
          <button type="button" hidden>重试睡眠历史</button>
        </div>
        <div class="fitbit-mobile-history" hidden>
          <p class="fitbit-mobile-week-summary"></p>
          <p class="fitbit-mobile-empty" data-empty="history" hidden>最近 7 天没有可用睡眠记录</p>
          <ul class="fitbit-mobile-days"></ul>
        </div>
      </section>`;

    const currentStatus = host.querySelector('[data-status="current"]');
    const historyStatus = host.querySelector('[data-status="history"]');
    const currentRetry = currentStatus.querySelector("button");
    const historyRetry = historyStatus.querySelector("button");
    const currentContent = host.querySelector(".fitbit-mobile-current");
    const historyContent = host.querySelector(".fitbit-mobile-history");

    const loadCurrent = () => {
      setStatus(currentStatus, "正在读取当前健康状态…");
      context.query("fitbit.current").then((overview) => {
        if (!active) return;
        const freshness = overview.freshness || {};
        const current = overview.current || {};
        const freshnessNode = host.querySelector(".fitbit-mobile-freshness");
        freshnessNode.textContent = overallFreshnessLabel(freshness);
        freshnessNode.classList.toggle("is-stale", Number(freshness.data_lag_min) > 15);
        host.querySelector('[data-current="sleep"]').textContent = sleepStateLabel(current.sleep_state);
        host.querySelector('[data-current="probability"]').textContent = current.sleep_prob === null || current.sleep_prob === undefined
          ? ""
          : `睡眠概率 ${number(Number(current.sleep_prob) * 100)}%`;
        host.querySelector('[data-current="heart"]').textContent = number(current.heart_rate);
        host.querySelector('[data-current="oxygen"]').textContent = number(current.spo2, 1);
        host.querySelector('[data-current="steps"]').textContent = number(current.steps);
        const oxygenFreshness = host.querySelector('[data-current="oxygen-freshness"]');
        oxygenFreshness.textContent = lagLabel(freshness.spo2_lag_min, "血氧");
        oxygenFreshness.classList.toggle("is-stale", Number(freshness.spo2_lag_min) > 15);

        const timeline = host.querySelector(".fitbit-mobile-timeline");
        const rawSegments = Array.isArray(overview.sleep_24h) ? overview.sleep_24h : [];
        const segments = normalizeTimeline(rawSegments);
        timeline.replaceChildren(...segments.map(rhythmSegment));
        timeline.hidden = segments.length === 0;
        timeline.setAttribute("aria-label", timelineLabel(segments));
        host.querySelector('[data-empty="rhythm"]').hidden = segments.length !== 0;
        currentStatus.hidden = true;
        currentContent.hidden = false;
      }).catch((error) => {
        if (!active) return;
        setStatus(currentStatus, errorMessage(error, "当前健康状态读取失败"), { error: true, retry: true });
      });
    };

    const loadHistory = () => {
      setStatus(historyStatus, "正在读取睡眠历史…");
      context.query("fitbit.sleep_history").then((overview) => {
        if (!active) return;
        if (overview.available === false && overview.reason === "projection_not_ready") {
          setStatus(historyStatus, "睡眠历史投影尚未生成，请等待后台首次同步", { error: true });
          historyContent.hidden = true;
          return;
        }
        const summary = overview.sleep_summary || {};
        const freshness = overview.freshness || {};
        host.querySelector('[data-week="coverage"]').textContent = `${number(summary.days_with_data)} / 7 天有数据`;
        const summaryNode = host.querySelector(".fitbit-mobile-week-summary");
        const summaryParts = [
          `平均 ${durationLabel(summary.avg_duration_min)}`,
          `效率 ${number(summary.avg_efficiency)}%`,
          `深睡 ${durationLabel(summary.avg_deep_min)}`,
        ];
        if (freshness.state === "stale") summaryParts.push("后台数据待刷新");
        summaryNode.textContent = summaryParts.join(" · ");
        summaryNode.classList.toggle("is-stale", freshness.state === "stale");
        const days = Array.isArray(overview.sleep_days) ? overview.sleep_days : [];
        host.querySelector(".fitbit-mobile-days").replaceChildren(...days.map(sleepDayRow));
        host.querySelector('[data-empty="history"]').hidden = days.length !== 0;
        historyStatus.hidden = true;
        historyContent.hidden = false;
      }).catch((error) => {
        if (!active) return;
        setStatus(historyStatus, errorMessage(error, "睡眠历史读取失败"), { error: true, retry: true });
      });
    };

    currentRetry.addEventListener("click", loadCurrent);
    historyRetry.addEventListener("click", loadHistory);
    loadCurrent();
    loadHistory();
    return () => {
      active = false;
      currentRetry.removeEventListener("click", loadCurrent);
      historyRetry.removeEventListener("click", loadHistory);
      host.classList.remove("fitbit-mobile");
    };
  },
};

export default {
  slots: {},
  dashboard,
};
