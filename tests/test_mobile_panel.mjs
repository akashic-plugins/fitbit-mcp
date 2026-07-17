import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { parseHTML } from "linkedom";

const source = await readFile(new URL("../mobile_panel.js", import.meta.url), "utf8");
const styles = await readFile(new URL("../mobile_panel.css", import.meta.url), "utf8");
const panel = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const current = {
  freshness: { last_updated: "10:36", data_lag_min: 3, spo2_lag_min: 99 },
  current: { heart_rate: 72, spo2: 93.8, steps: 3409, sleep_state: "awake", sleep_prob: 0.02 },
  sleep_24h: [
    { range: "23:00-07:00", state: "sleeping", duration_min: 480 },
    { range: "07:00-10:00", state: "awake", duration_min: 180 },
  ],
};

const history = {
  sleep_summary: { days_with_data: 1, avg_duration_min: 373, avg_efficiency: 96, avg_deep_min: 95 },
  sleep_days: [{ date: "2026-07-16", duration_min: 373, efficiency: 96, deep_min: 95, no_data: false }],
};

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

test("mobile navigation describes the health task", () => {
  assert.equal(panel.default.navigation.label, "健康状态");
  assert.match(panel.default.navigation.description, /心率、血氧、步数和最近睡眠/);
  assert.equal(typeof panel.default.dashboard.mount, "function");
});

test("health labels remain concise and localised", () => {
  assert.equal(panel.sleepStateLabel("awake"), "清醒");
  assert.equal(panel.sleepStateLabel("sleeping"), "睡眠中");
  assert.equal(panel.durationLabel(373), "6 小时 13 分");
  assert.equal(panel.durationLabel(60), "1 小时");
});

test("timeline keeps the latest day and represents missing time explicitly", () => {
  const short = panel.normalizeTimeline([
    { range: "23:00-07:00", state: "sleeping", duration_min: 480 },
    { range: "07:00-10:00", state: "awake", duration_min: 180 },
  ]);
  assert.deepEqual(short[0], { range: "无数据", state: "unknown", duration_min: 780 });
  assert.equal(short.reduce((total, item) => total + item.duration_min, 0), 1440);

  const exact = panel.normalizeTimeline([{ range: "全天", state: "unknown", duration_min: 1440 }]);
  assert.equal(exact.length, 1);
  assert.equal(exact[0].duration_min, 1440);

  const long = panel.normalizeTimeline([
    { range: "旧", state: "awake", duration_min: 600 },
    { range: "最近", state: "sleeping", duration_min: 1200 },
  ]);
  assert.deepEqual(long, [
    { range: "旧", state: "awake", duration_min: 240 },
    { range: "最近", state: "sleeping", duration_min: 1200 },
  ]);
  assert.deepEqual(panel.normalizeTimeline([]), []);
  assert.match(panel.timelineLabel(short), /睡眠 8 小时，清醒 3 小时，无数据 13 小时/);
});

test("current health survives history failure and retries only history", async () => {
  const { document, window } = parseHTML('<main id="host"></main>');
  globalThis.document = document;
  globalThis.window = window;
  const host = document.querySelector("#host");
  const calls = [];
  let historyAttempts = 0;
  const context = {
    request(method) {
      calls.push(method);
      if (method === "fitbit.current") return Promise.resolve(current);
      historyAttempts += 1;
      return historyAttempts === 1 ? Promise.reject(new Error("401")) : Promise.resolve(history);
    },
  };

  const unmount = panel.default.dashboard.mount(host, context);
  await flush();
  assert.deepEqual(calls, ["fitbit.current", "fitbit.sleep_history"]);
  assert.equal(host.querySelector('[data-current="heart"]').textContent, "72");
  assert.equal(host.querySelector('[data-current="oxygen-freshness"]').textContent, "血氧延迟 99 分钟");
  assert.equal(host.querySelector('[data-current="oxygen-freshness"]').classList.contains("is-stale"), true);
  assert.equal(host.querySelector(".fitbit-mobile-timeline .unknown") !== null, true);
  assert.match(host.querySelector(".fitbit-mobile-timeline").getAttribute("aria-label"), /无数据 13 小时/);
  assert.equal(host.querySelector(".fitbit-mobile-timeline span").getAttribute("aria-hidden"), "true");
  const historyStatus = host.querySelector('[data-status="history"]');
  assert.match(historyStatus.textContent, /睡眠历史读取失败：401/);
  assert.equal(historyStatus.querySelector("button").hidden, false);

  historyStatus.querySelector("button").click();
  await flush();
  assert.deepEqual(calls, ["fitbit.current", "fitbit.sleep_history", "fitbit.sleep_history"]);
  assert.equal(host.querySelector('[data-current="heart"]').textContent, "72");
  assert.match(host.querySelector(".fitbit-mobile-week-summary").textContent, /平均 6 小时 13 分/);
  assert.equal(host.querySelectorAll(".fitbit-mobile-day").length, 1);
  unmount();
  assert.equal(host.classList.contains("fitbit-mobile"), false);
});

test("sleep history survives current failure", async () => {
  const { document, window } = parseHTML('<main id="host"></main>');
  globalThis.document = document;
  globalThis.window = window;
  const host = document.querySelector("#host");
  panel.default.dashboard.mount(host, {
    request(method) {
      return method === "fitbit.current"
        ? Promise.reject(new Error("snapshot unavailable"))
        : Promise.resolve(history);
    },
  });

  await flush();
  assert.match(host.querySelector('[data-status="current"]').textContent, /当前健康状态读取失败/);
  assert.equal(host.querySelectorAll(".fitbit-mobile-day").length, 1);
  assert.match(host.querySelector(".fitbit-mobile-week-summary").textContent, /效率 96%/);
});

test("expired Fitbit authorization stays inside the health panel", async () => {
  const { document, window } = parseHTML('<main id="host"></main>');
  globalThis.document = document;
  globalThis.window = window;
  const host = document.querySelector("#host");
  panel.default.dashboard.mount(host, {
    request(method) {
      return method === "fitbit.current"
        ? Promise.resolve(current)
        : Promise.resolve({ available: false, reason: "fitbit_oauth_required" });
    },
  });

  await flush();
  const historyStatus = host.querySelector('[data-status="history"]');
  assert.match(historyStatus.textContent, /Fitbit 授权已过期/);
  assert.equal(historyStatus.querySelector("button").hidden, true);
  assert.equal(host.querySelector('[data-current="heart"]').textContent, "72");
});

test("pending requests cannot mutate the panel after unmount", async () => {
  const { document, window } = parseHTML('<main id="host"></main>');
  globalThis.document = document;
  globalThis.window = window;
  const host = document.querySelector("#host");
  const resolvers = [];
  const unmount = panel.default.dashboard.mount(host, {
    request() {
      return new Promise((resolve) => resolvers.push(resolve));
    },
  });
  const before = host.innerHTML;
  unmount();
  resolvers[0](current);
  resolvers[1](history);
  await flush();

  assert.equal(host.innerHTML, before);
  assert.equal(host.classList.contains("fitbit-mobile"), false);
});

test("plugin owns a task-first panel without copying the desktop dashboard", () => {
  assert.match(source, /最近 24 小时/);
  assert.match(source, /7 天睡眠/);
  assert.match(source, /fitbit\.current/);
  assert.match(source, /fitbit\.sleep_history/);
  assert.doesNotMatch(source, /fitbit\.overview/);
  assert.doesNotMatch(source, /window\.AkashicDashboard/);
  assert.match(styles, /\.fitbit-mobile \[hidden\][\s\S]*display: none !important/);
  assert.doesNotMatch(styles, /linear-gradient|box-shadow|backdrop-filter/);
});

test("domain colors use oklch and lists stay on the page plane", () => {
  assert.match(styles, /--fitbit-heart: oklch/);
  assert.match(styles, /--fitbit-oxygen: oklch/);
  assert.match(styles, /--fitbit-steps: oklch/);
  assert.match(styles, /--fitbit-sleep: oklch/);
  assert.match(styles, /--fitbit-sleep-ink: oklch\(0\.22/);
  assert.match(styles, /\.fitbit-mobile-day[\s\S]*border-bottom/);
});
