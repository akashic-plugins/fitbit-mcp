import assert from "node:assert/strict";
import test from "node:test";
import { parseHTML } from "linkedom";


const overview = {
  last_updated: "13:27:01",
  stale: false,
  current: {
    heart_rate: 72,
    spo2: 97.4,
    steps: 4823,
    sleep_state: "awake",
    sleep_reason: "高概率清醒",
    sleep_prob: 0.18,
  },
  freshness: { data_lag_min: 3, spo2_lag_min: 12 },
  signals: { prob_source: "ml", hr_avg: 71.5 },
  sleep_24h: [
    { range: "23:00-07:00", state: "sleeping" },
    { range: "07:00-13:27", state: "awake" },
  ],
  prediction_events: [
    {
      time: "2026-08-01 13:27:00",
      source: "ml",
      sleep_probability: 0.18,
      final_state: "awake",
      reason: "Viterbi 判定清醒",
      changed: false,
    },
    {
      time: "2026-08-01 12:27:00",
      source: "ml",
      sleep_probability: 0.75,
      final_state: "sleeping",
      reason: "Viterbi 判定睡眠",
      changed: true,
    },
    {
      time: "2026-08-01 11:27:00",
      source: "ml",
      sleep_probability: 0.12,
      final_state: "awake",
      reason: "Viterbi 判定清醒",
      changed: false,
    },
  ],
  heart_rate_series: [
    { time: "13:25:00", value: 69 },
    { time: "13:26:00", value: 72 },
  ],
  steps_series: [],
};


test("Dashboard panel renders the current monitor snapshot without a table", async () => {
  const { window } = parseHTML("<html><body><div id='host'></div></body></html>");
  const registered = [];
  window.api = async () => overview;
  window.setInterval = setInterval;
  window.clearInterval = clearInterval;
  window.setTimeout = setTimeout;
  window.AkashicDashboard = { registerPlugin: (plugin) => registered.push(plugin) };
  globalThis.window = window;
  globalThis.document = window.document;

  await import(`../dashboard_panel.js?test=${Date.now()}`);
  assert.equal(registered.length, 1);
  assert.equal(registered[0].layout, "workbench");

  const host = window.document.querySelector("#host");
  registered[0].renderMain(host, {});
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(host.querySelector("[data-fitbit-state]").textContent, "清醒");
  assert.equal(host.querySelector("[data-fitbit-heart]").textContent, "72");
  assert.equal(host.querySelector("[data-fitbit-oxygen]").textContent, "97.4");
  assert.equal(host.querySelector("[data-fitbit-steps]").textContent, "4,823");
  assert.equal(host.querySelectorAll("table").length, 0);
  assert.equal(host.querySelectorAll("[data-fitbit-timeline] > span").length, 2);
  assert.equal(host.querySelectorAll("[data-fitbit-prediction-events] > li").length, 3);
  assert.match(host.querySelector("[data-fitbit-prediction-events]").textContent, /ML 模型 · 睡眠概率 18%/);
  assert.match(host.querySelector("[data-fitbit-prediction-events]").textContent, /最终状态清醒/);
  assert.match(host.querySelector("[data-fitbit-prediction-line]").getAttribute("d"), /^M.*L.*L/);
  assert.equal(host.querySelectorAll("[data-fitbit-prediction-markers] > i").length, 2);
  assert.equal(host.querySelector("[data-fitbit-prediction-start]").textContent, "08-01 11:27");
  assert.equal(host.querySelector("[data-fitbit-prediction-end]").textContent, "现在 · 13:27");
  assert.equal(host.querySelector("[data-fitbit-prediction-window]").textContent, "最近 24 小时 · 3 次判断");
  assert.equal(host.querySelector(".fitbit-dashboard__eyebrow"), null);
  assert.match(host.querySelector(".fitbit-dashboard__subtitle").textContent, /^18765 实时数据/);
  assert.match(host.querySelector("[data-fitbit-heart-path]").getAttribute("d"), /^M/);

  host.__fitbitDashboardDispose();
});
