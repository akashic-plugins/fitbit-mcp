import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { parseHTML } from "linkedom";

import { activate } from "../web_module.js";


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


function response(payload, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => payload };
}


async function settle() {
  await setImmediate();
}

function installPanel(http) {
  const { window } = parseHTML("<html><body><div id='host'></div></body></html>");
  window.setInterval = setInterval;
  window.clearInterval = clearInterval;
  window.setTimeout = setTimeout;
  window.clearTimeout = clearTimeout;
  globalThis.window = window;
  globalThis.document = window.document;
  let panel;
  let released = false;
  const release = activate({
    ui: {
      inject(contract, callback) {
        assert.equal(contract, "workbench.panels.v1");
        return callback({
          register(definition) {
            panel = definition;
            return () => { released = true; };
          },
        });
      },
    },
    http: { request: http },
  });
  return { window, host: window.document.querySelector("#host"), panel, release, wasReleased: () => released };
}


test("Workbench panel renders the current monitor snapshot through ctx.http", async () => {
  const fixture = installPanel(async (path) => {
    assert.equal(path, "/api/dashboard/fitbit/overview");
    return response(overview);
  });

  assert.equal(fixture.panel.id, "fitbit");
  const dispose = fixture.panel.render(fixture.host);
  await settle();

  assert.equal(fixture.host.querySelector("[data-fitbit-state]").textContent, "清醒");
  assert.equal(fixture.host.querySelector("[data-fitbit-heart]").textContent, "72");
  assert.equal(fixture.host.querySelector("[data-fitbit-oxygen]").textContent, "97.4");
  assert.equal(fixture.host.querySelector("[data-fitbit-steps]").textContent, "4,823");
  assert.equal(fixture.host.querySelectorAll("table").length, 0);
  assert.equal(fixture.host.querySelectorAll("[data-fitbit-timeline] > span").length, 2);
  assert.equal(fixture.host.querySelectorAll("[data-fitbit-prediction-events] > li").length, 3);
  assert.match(fixture.host.querySelector("[data-fitbit-prediction-line]").getAttribute("d"), /^M.*L.*L/);
  assert.equal(fixture.host.querySelectorAll("[data-fitbit-prediction-markers] > i").length, 2);

  dispose();
  fixture.release();
  assert.equal(fixture.host.childNodes.length, 0);
  assert.equal(fixture.wasReleased(), true);
});


test("Workbench panel coalesces focus loads and disposes its request and interval", () => {
  let requests = 0;
  let signal;
  const fixture = installPanel((_path, init) => {
    requests += 1;
    signal = init.signal;
    return new Promise(() => {});
  });
  let cleared = null;
  fixture.window.setInterval = () => 17;
  fixture.window.clearInterval = (timer) => { cleared = timer; };

  const dispose = fixture.panel.render(fixture.host);
  fixture.window.dispatchEvent(new fixture.window.Event("focus"));
  fixture.window.dispatchEvent(new fixture.window.Event("focus"));

  assert.equal(requests, 1);
  dispose();
  fixture.window.dispatchEvent(new fixture.window.Event("focus"));
  assert.equal(requests, 1);
  assert.equal(cleared, 17);
  assert.equal(signal.aborted, true);
});


test("Workbench panel clears its delayed refresh callback on dispose", async () => {
  const fixture = installPanel(async () => response(overview));
  const timeouts = [];
  let cleared = null;
  fixture.window.setTimeout = (callback, delay) => {
    timeouts.push({ callback, delay });
    return 23;
  };
  fixture.window.clearTimeout = (timer) => { cleared = timer; };

  const dispose = fixture.panel.render(fixture.host);
  await settle();
  fixture.host.querySelector("[data-fitbit-refresh]").click();
  await settle();

  assert.equal(timeouts.length, 1);
  assert.equal(timeouts[0].delay, 900);
  dispose();
  assert.equal(cleared, 23);
});


test("Workbench refresh starts a new overview read after a slow initial load", async () => {
  const overviewRequests = [];
  let refreshCalls = 0;
  const fixture = installPanel((path, init) => {
    if (path.endsWith("/refresh")) {
      refreshCalls += 1;
      return response({ status: "refreshing" }, 202);
    }
    return new Promise((resolve) => overviewRequests.push({ resolve, signal: init.signal }));
  });
  let delayedRefresh;
  fixture.window.setTimeout = (callback) => { delayedRefresh = callback; return 23; };

  const dispose = fixture.panel.render(fixture.host);
  fixture.host.querySelector("[data-fitbit-refresh]").click();
  await settle();
  delayedRefresh();

  assert.equal(refreshCalls, 1);
  assert.equal(overviewRequests.length, 2);
  assert.equal(overviewRequests[0].signal.aborted, true);
  overviewRequests[1].resolve(response(overview));
  await settle();
  assert.equal(fixture.host.querySelector("[data-fitbit-state]").textContent, "清醒");
  dispose();
});


test("Workbench panel requests a plugin-owned authorization URL before opening it", async () => {
  const calls = [];
  const fixture = installPanel(async (path) => {
    calls.push(path);
    return response(path.endsWith("/auth/start")
      ? { url: "https://www.fitbit.com/oauth2/authorize?client_id=test" }
      : overview);
  });
  const opened = [];
  const authorizationWindow = {
    close() {},
    location: { replace(url) { opened.push(["replace", url]); } },
  };
  fixture.window.open = (...args) => {
    opened.push(args);
    return authorizationWindow;
  };

  const dispose = fixture.panel.render(fixture.host);
  await settle();
  fixture.host.querySelector("[data-fitbit-auth]").click();
  await settle();

  assert.deepEqual(calls, ["/api/dashboard/fitbit/overview", "/api/dashboard/fitbit/auth/start"]);
  assert.equal(opened.length, 2);
  assert.equal(opened[0][0], "about:blank");
  assert.equal(opened[0][1], "_blank");
  assert.equal(opened[1][0], "replace");
  assert.equal(opened[1][1], "https://www.fitbit.com/oauth2/authorize?client_id=test");
  assert.equal(authorizationWindow.opener, null);
  assert.equal(fixture.host.querySelector("a[href]"), null);
  dispose();
});


test("Workbench disposer closes an authorization popup before navigation commits", async () => {
  const fixture = installPanel((path) => path.endsWith("/auth/start")
    ? new Promise(() => {})
    : Promise.resolve(response(overview)));
  let closed = false;
  fixture.window.open = () => ({
    close() { closed = true; },
    location: { replace() {} },
  });

  const dispose = fixture.panel.render(fixture.host);
  await settle();
  fixture.host.querySelector("[data-fitbit-auth]").click();
  dispose();

  assert.equal(closed, true);
});
