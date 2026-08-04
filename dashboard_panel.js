const api = window.api;

function number(value, digits = 0) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function stateLabel(value) {
  if (value === "sleeping") return "睡眠中";
  if (value === "awake") return "清醒";
  if (value === "uncertain") return "状态波动";
  return "等待数据";
}

function stateClass(value) {
  if (value === "sleeping") return "is-sleeping";
  if (value === "awake") return "is-awake";
  return "is-unknown";
}

function sourceLabel(value) {
  if (value === "ml") return "ML 模型";
  if (value === "heuristic") return "规则估计";
  return "来源未知";
}

function duration(range) {
  const [start, end] = String(range).split("-");
  if (!start || !end) return 0;
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  if (![sh, sm, eh, em].every(Number.isFinite)) return 0;
  return (eh * 60 + em - sh * 60 - sm + 1440) % 1440 || 1;
}

function renderTimeline(root, segments) {
  const timeline = root.querySelector("[data-fitbit-timeline]");
  const label = root.querySelector("[data-fitbit-timeline-label]");
  const totals = { sleeping: 0, awake: 0, unknown: 0 };
  timeline.replaceChildren();
  for (const item of segments) {
    const minutes = duration(item.range);
    totals[item.state] += minutes;
    const segment = document.createElement("span");
    segment.className = `fitbit-dashboard__segment is-${item.state}`;
    segment.style.flexGrow = String(Math.max(1, minutes));
    segment.title = `${item.range} · ${stateLabel(item.state)}`;
    timeline.appendChild(segment);
  }
  label.textContent = segments.length
    ? `睡眠 ${Math.round(totals.sleeping / 60 * 10) / 10} 小时 · 清醒 ${Math.round(totals.awake / 60 * 10) / 10} 小时`
    : "尚无最近 24 小时记录";
}

function renderPredictionHistory(root, events) {
  const line = root.querySelector("[data-fitbit-prediction-line]");
  const markers = root.querySelector("[data-fitbit-prediction-markers]");
  const list = root.querySelector("[data-fitbit-prediction-events]");
  const empty = root.querySelector("[data-fitbit-prediction-empty]");
  const startLabel = root.querySelector("[data-fitbit-prediction-start]");
  const endLabel = root.querySelector("[data-fitbit-prediction-end]");
  const windowLabel = root.querySelector("[data-fitbit-prediction-window]");
  markers.replaceChildren();
  list.replaceChildren();
  empty.hidden = events.length > 0;
  line.setAttribute("d", "");
  if (!events.length) return;

  const available = [...events].reverse().filter((event) =>
    event.sleep_probability !== null && event.sleep_probability !== undefined
  );
  const newestTimestamp = eventTimestamp(available.at(-1)?.time);
  const cutoff = Number.isFinite(newestTimestamp) ? newestTimestamp - 24 * 60 * 60 * 1000 : null;
  const chronological = cutoff === null
    ? available
    : available.filter((event) => eventTimestamp(event.time) >= cutoff);
  if (!chronological.length) return;
  const times = chronological.map((event) => eventTimestamp(event.time));
  const firstTime = times.find(Number.isFinite);
  const lastTime = times.findLast(Number.isFinite);
  const timeSpan = Number.isFinite(firstTime) && Number.isFinite(lastTime) && lastTime > firstTime
    ? lastTime - firstTime
    : null;
  const coordinates = chronological.map((event, index) => {
    const timestamp = times[index];
    const xRatio = timeSpan && Number.isFinite(timestamp)
      ? (timestamp - firstTime) / timeSpan
      : (chronological.length === 1 ? 0.5 : index / (chronological.length - 1));
    const probability = Math.max(0, Math.min(1, Number(event.sleep_probability)));
    return { event, xRatio, probability };
  });
  line.setAttribute("d", coordinates.map(({ xRatio, probability }, index) => {
    const x = 8 + xRatio * 984;
    const y = 8 + (1 - probability) * 144;
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" "));
  startLabel.textContent = eventTimeLabel(chronological[0].time, true);
  endLabel.textContent = `现在 · ${eventTimeLabel(chronological.at(-1).time, false)}`;
  windowLabel.textContent = `最近 24 小时 · ${chronological.length} 次判断`;

  const highlighted = coordinates.filter(({ event }, index) => event.changed || index === coordinates.length - 1);
  for (const { event, xRatio, probability } of highlighted) {
    const marker = document.createElement("i");
    const percent = Math.round(probability * 100);
    marker.className = `fitbit-dashboard__prediction-marker ${stateClass(event.final_state)}`;
    marker.style.left = `${xRatio * 100}%`;
    marker.style.top = `${(1 - probability) * 100}%`;
    marker.title = `${eventTimeLabel(event.time, false)} · ${sourceLabel(event.source)}睡眠概率 ${percent}% · 最终${stateLabel(event.final_state)}`;
    markers.appendChild(marker);
  }

  for (const event of events.slice(0, 12)) {
    const item = document.createElement("li");
    const probability = event.sleep_probability === null || event.sleep_probability === undefined
      ? "概率不可用"
      : `睡眠概率 ${Math.round(Number(event.sleep_probability) * 100)}%`;
    item.className = event.changed ? "is-change" : "";
    const time = document.createElement("time");
    time.textContent = String(event.time || "—").slice(11, 16);
    const model = predictionValue("模型输出", `${sourceLabel(event.source)} · ${probability}`);
    const arrow = document.createElement("b");
    arrow.ariaHidden = "true";
    arrow.textContent = "→";
    const decision = predictionValue("最终状态", stateLabel(event.final_state));
    decision.querySelector("strong").className = stateClass(event.final_state);
    item.append(time, model, arrow, decision);
    if (event.changed) {
      const marker = document.createElement("em");
      marker.textContent = "状态切换";
      item.appendChild(marker);
    }
    item.title = event.reason || "";
    list.appendChild(item);
  }
}

function eventTimestamp(value) {
  return Date.parse(String(value || "").replace(" ", "T"));
}

function eventTimeLabel(value, includeDate) {
  const text = String(value || "—");
  return includeDate ? text.slice(5, 16) : text.slice(11, 16);
}

function predictionValue(label, value) {
  const group = document.createElement("span");
  const caption = document.createElement("small");
  const content = document.createElement("strong");
  caption.textContent = label;
  content.textContent = value;
  group.append(caption, content);
  return group;
}

function sparklinePath(series, width = 420, height = 116) {
  const values = series.map((point) => Number(point.value)).filter(Number.isFinite);
  if (values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = Math.max(1, max - min);
  return values.map((value, index) => {
    const x = 8 + index / (values.length - 1) * (width - 16);
    const y = 8 + (max - value) / span * (height - 16);
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
}

function setText(root, selector, value) {
  root.querySelector(selector).textContent = value;
}

function renderOverview(root, payload) {
  const current = payload.current;
  const freshness = payload.freshness;
  const probability = current.sleep_prob === null || current.sleep_prob === undefined
    ? null
    : Math.round(Number(current.sleep_prob) * 100);
  const sleepState = stateLabel(current.sleep_state);

  root.querySelector("[data-fitbit-state-surface]").className =
    `fitbit-dashboard__state-surface ${stateClass(current.sleep_state)}`;
  setText(root, "[data-fitbit-state]", sleepState);
  setText(root, "[data-fitbit-probability]", probability === null
    ? "模型输出尚不可用"
    : `${sourceLabel(payload.signals.prob_source)}输出 · 睡眠概率 ${probability}%`);
  setText(root, "[data-fitbit-reason]", current.sleep_reason);
  setText(root, "[data-fitbit-heart]", number(current.heart_rate));
  setText(root, "[data-fitbit-oxygen]", number(current.spo2, 1));
  setText(root, "[data-fitbit-steps]", number(current.steps));
  setText(root, "[data-fitbit-lag]", freshness.data_lag_min == null ? "更新时间未知" : `延迟 ${number(freshness.data_lag_min)} 分钟`);
  setText(root, "[data-fitbit-updated]", payload.last_updated ? `${payload.last_updated} 更新` : "等待首次同步");
  root.querySelector("[data-fitbit-status]").classList.toggle("is-stale", payload.stale || Number(freshness.data_lag_min) > 15);
  root.querySelector("[data-fitbit-content]").hidden = false;
  root.querySelector("[data-fitbit-loading]").hidden = true;
  root.querySelector("[data-fitbit-error]").hidden = true;

  const path = root.querySelector("[data-fitbit-heart-path]");
  const d = sparklinePath(payload.heart_rate_series);
  path.setAttribute("d", d);
  root.querySelector("[data-fitbit-heart-empty]").hidden = Boolean(d);
  renderTimeline(root, payload.sleep_24h);
  renderPredictionHistory(root, payload.prediction_events);
}

function showError(root, error) {
  root.querySelector("[data-fitbit-loading]").hidden = true;
  root.querySelector("[data-fitbit-content]").hidden = true;
  const errorNode = root.querySelector("[data-fitbit-error]");
  errorNode.hidden = false;
  errorNode.querySelector("span").textContent = error instanceof Error ? error.message : "Fitbit 数据读取失败";
}

function renderFitbitDashboard(container) {
  container.__fitbitDashboardDispose?.();
  container.innerHTML = `
    <main class="fitbit-dashboard" aria-labelledby="fitbit-dashboard-title">
      <header class="fitbit-dashboard__header">
        <div>
          <h1 id="fitbit-dashboard-title">Fitbit 健康</h1>
          <p class="fitbit-dashboard__subtitle">展示 Fitbit 观测与睡眠判断，不提供医疗建议。</p>
        </div>
        <div class="fitbit-dashboard__actions">
          <span class="fitbit-dashboard__sync" data-fitbit-status><i></i><span data-fitbit-updated>正在连接</span></span>
          <a class="fitbit-dashboard__button is-tonal" href="/api/dashboard/fitbit/auth/start" target="_blank" rel="noreferrer">连接 Fitbit</a>
          <button class="fitbit-dashboard__button" type="button" data-fitbit-refresh>刷新</button>
        </div>
      </header>

      <section class="fitbit-dashboard__loading" data-fitbit-loading role="status">正在读取当前健康状态…</section>
      <section class="fitbit-dashboard__error" data-fitbit-error role="alert" hidden><span></span><button type="button" data-fitbit-retry>重试</button></section>

      <div data-fitbit-content hidden>
        <section class="fitbit-dashboard__hero" aria-label="当前健康概览">
          <article class="fitbit-dashboard__state-surface is-unknown" data-fitbit-state-surface>
            <div class="fitbit-dashboard__state-reading">
              <span>当前判断</span>
              <strong data-fitbit-state>等待数据</strong>
              <small data-fitbit-probability>概率尚不可用</small>
            </div>
            <p data-fitbit-reason>等待首轮判断</p>
          </article>

          <dl class="fitbit-dashboard__metrics">
            <div class="fitbit-dashboard__metric is-heart">
              <dt><span>心率</span><small>当前</small></dt>
              <dd><strong data-fitbit-heart>—</strong><span>bpm</span></dd>
            </div>
            <div class="fitbit-dashboard__metric is-oxygen">
              <dt><span>血氧</span><small>最近睡眠</small></dt>
              <dd><strong data-fitbit-oxygen>—</strong><span>%</span></dd>
            </div>
            <div class="fitbit-dashboard__metric is-steps">
              <dt><span>步数</span><small data-fitbit-lag>更新时间未知</small></dt>
              <dd><strong data-fitbit-steps>—</strong><span>步</span></dd>
            </div>
          </dl>
        </section>

        <section class="fitbit-dashboard__insights" aria-label="当前数据趋势">
          <article class="fitbit-dashboard__trend">
            <header><div><span>心率趋势</span><strong>最近 60 个采样点</strong></div><small>实时序列</small></header>
            <svg viewBox="0 0 420 116" role="img" aria-label="最近心率趋势">
              <path class="fitbit-dashboard__spark-baseline" d="M8 108 H412" />
              <path class="fitbit-dashboard__spark" data-fitbit-heart-path d="" />
            </svg>
            <p data-fitbit-heart-empty hidden>数据点不足，暂时无法绘制趋势。</p>
          </article>

          <article class="fitbit-dashboard__rhythm">
            <header><div><span>最近 24 小时</span><strong data-fitbit-timeline-label>正在整理睡眠节律</strong></div><small>现在</small></header>
            <div class="fitbit-dashboard__timeline" data-fitbit-timeline role="img" aria-label="最近 24 小时睡眠节律"></div>
            <div class="fitbit-dashboard__legend"><span><i class="is-sleeping"></i>睡眠</span><span><i class="is-awake"></i>清醒</span><span><i class="is-unknown"></i>无数据</span></div>
          </article>
        </section>

        <details class="fitbit-dashboard__predictions">
          <summary>
            <div>
              <span id="fitbit-prediction-title">ML 判断记录</span>
              <strong>模型概率和最终状态是两层结果</strong>
            </div>
            <small data-fitbit-prediction-window>最近 24 小时</small>
          </summary>
          <div class="fitbit-dashboard__decision-flow" aria-label="睡眠判断流程">
            <span><small>第一层</small><strong>ML 睡眠概率</strong></span>
            <b aria-hidden="true">→</b>
            <span><small>第二层</small><strong>Viterbi 状态解码</strong></span>
            <b aria-hidden="true">→</b>
            <span><small>页面显示</small><strong>清醒 / 睡眠 / 波动</strong></span>
          </div>
          <div class="fitbit-dashboard__prediction-plot" aria-label="历史 ML 睡眠概率时间序列">
            <div class="fitbit-dashboard__prediction-y" aria-hidden="true">
              <span>100% 睡眠</span><span>50% 分界</span><span>0% 清醒</span>
            </div>
            <div class="fitbit-dashboard__prediction-chart">
              <div class="fitbit-dashboard__prediction-canvas">
                <svg viewBox="0 0 1000 160" preserveAspectRatio="none" role="img" aria-label="ML 睡眠概率折线">
                  <path class="fitbit-dashboard__prediction-grid" d="M8 8 H992 M8 80 H992 M8 152 H992" />
                  <path class="fitbit-dashboard__prediction-line" data-fitbit-prediction-line d="" />
                </svg>
                <div data-fitbit-prediction-markers></div>
              </div>
              <div class="fitbit-dashboard__prediction-x">
                <time data-fitbit-prediction-start>最早记录</time>
                <time data-fitbit-prediction-end>现在</time>
              </div>
            </div>
          </div>
          <p class="fitbit-dashboard__prediction-empty" data-fitbit-prediction-empty hidden>尚无模型判断记录。</p>
          <ol class="fitbit-dashboard__prediction-events" data-fitbit-prediction-events></ol>
        </details>
      </div>
    </main>`;

  let disposed = false;
  let timer;
  let inFlight = null;
  let lastLoadedAt = 0;
  const refreshIntervalMs = 60_000;
  const load = () => {
    if (inFlight) return inFlight;
    inFlight = api("/api/dashboard/fitbit/overview")
      .then((payload) => {
        if (!disposed) {
          renderOverview(container, payload);
          lastLoadedAt = Date.now();
        }
      })
      .catch((error) => {
        if (!disposed) showError(container, error);
      })
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  };
  const refresh = async () => {
    const button = container.querySelector("[data-fitbit-refresh]");
    button.disabled = true;
    button.textContent = "刷新中";
    try {
      await api("/api/dashboard/fitbit/refresh", { method: "POST" });
      window.setTimeout(() => void load(), 900);
    } catch (error) {
      showError(container, error);
    } finally {
      button.disabled = false;
      button.textContent = "刷新";
    }
  };
  container.querySelector("[data-fitbit-refresh]").addEventListener("click", refresh);
  container.querySelector("[data-fitbit-retry]").addEventListener("click", load);
  const onFocus = () => {
    if (Date.now() - lastLoadedAt >= refreshIntervalMs) void load();
  };
  window.addEventListener("focus", onFocus);
  timer = window.setInterval(load, refreshIntervalMs);
  container.__fitbitDashboardDispose = () => {
    disposed = true;
    window.clearInterval(timer);
    window.removeEventListener("focus", onFocus);
  };
  void load();
}

window.AkashicDashboard.registerPlugin({
  id: "fitbit_health",
  label: "Fitbit 健康",
  viewLabel: "Fitbit 健康",
  layout: "workbench",
  pageSize: 1,
  rowKey: "id",
  columns: [{ key: "id", label: "Fitbit", flex: true }],
  getCount() {
    return 1;
  },
  async fetchPage() {
    return { items: [], total: 0 };
  },
  renderMain(container) {
    renderFitbitDashboard(container);
  },
});
