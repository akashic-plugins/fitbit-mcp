#!/usr/bin/env python3
"""
生成 sleep model 与 Fitbit 官方睡眠真值差异的可视化 HTML 报告。
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import eval_replay
import sleep_model
from paths import DATA_DIR


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build sleep-vs-truth diff HTML report.")
    p.add_argument("--start-date", default="2026-03-02", help="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD, default=today")
    p.add_argument(
        "--binary-mode",
        choices=("keep", "uncertain_as_awake", "uncertain_as_sleeping"),
        default="keep",
        help="是否将 uncertain 折叠成二态结果",
    )
    p.add_argument(
        "--out",
        default="static/sleep_diff_report.html",
        help="output html path (relative to fitbit-monitor dir)",
    )
    return p.parse_args()


def _collapse_pred_state(pred_state: str, binary_mode: str) -> str:
    if pred_state != "uncertain":
        return pred_state
    if binary_mode == "uncertain_as_awake":
        return "awake"
    if binary_mode == "uncertain_as_sleeping":
        return "sleeping"
    return pred_state


def build_rows(start_d: date, end_d: date, binary_mode: str = "keep") -> list[dict]:
    model = sleep_model.load_model()
    if model is None:
        raise RuntimeError("sleep_model.pkl unavailable or failed to load")
    labels = sleep_model.load_labels()
    entries = eval_replay.load_entries(start_d, end_d)
    if not entries:
        raise RuntimeError("指定时间范围无数据")

    # 1. 按时间顺序回放日志，得到当前模型状态机输出。
    # 2. 用 Fitbit 官方 sleep window 生成二分类真值（sleeping/awake）。
    # 3. 生成前端可直接消费的时间点序列。
    sm = eval_replay.StateMachine()
    rows: list[dict] = []
    for e in entries:
        poll_time = str(e.get("poll_time", ""))
        signals = dict(e.get("signals", {}) or {})
        lag = e.get("data_lag_min")
        lag_int = int(lag) if isinstance(lag, (int, float)) else None
        data_time = str(e.get("data_time") or "")
        evidence_id = f"{poll_time[:10]} {data_time}" if data_time else poll_time
        signals["data_lag_min"] = lag_int
        signals["evidence_id"] = evidence_id
        signals["poll_time"] = poll_time

        prob = sleep_model.predict(model, signals, lag_int)
        prob = max(0.0, min(1.0, float(prob)))
        raw_state, raw_reason = eval_replay.raw_state_from_prob(prob, signals)
        pred_state, _ = sm.next_state(prob, raw_reason, signals)
        pred_state = _collapse_pred_state(pred_state, binary_mode)
        truth_sleep = sleep_model.is_sleeping(poll_time, labels)
        truth_state = "sleeping" if truth_sleep else "awake"
        rows.append(
            {
                "ts": poll_time,
                "pred": pred_state,
                "truth": truth_state,
                "prob": round(prob, 4),
            }
        )
    return rows


def render_html(
    rows: list[dict], start_d: date, end_d: date, binary_mode: str = "keep"
) -> str:
    data_json = json.dumps(rows, ensure_ascii=False)
    include_uncertain = binary_mode == "keep"
    mode_title = {
        "keep": "三态",
        "uncertain_as_awake": "二态（uncertain→awake）",
        "uncertain_as_sleeping": "二态（uncertain→sleeping）",
    }[binary_mode]
    uncertain_legend = (
        '<span class="tag"><i class="dot" style="background:var(--pred-uncertain)"></i>模型 uncertain</span>'
        if include_uncertain
        else ""
    )
    uncertain_error_legend = (
        '<span class="tag"><i class="dot" style="background:var(--err-low)"></i>sleeping→uncertain</span>'
        if include_uncertain
        else ""
    )
    uncertain_filter = (
        '<label><input id="fLow" type="checkbox" checked /> 显示 sleeping→uncertain</label>'
        if include_uncertain
        else ""
    )
    uncertain_hint = (
        "主时间轴共四层：真值、模型、错误高亮、睡眠中横跳强度（15分钟桶，颜色越红切换越频繁）"
        if include_uncertain
        else "主时间轴共四层：真值、模型（二态折叠后）、错误高亮、睡眠中横跳强度（15分钟桶，颜色越红切换越频繁）"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sleep Model 差异看板</title>
  <style>
    :root {{
      /* Deep Monet & Material Dark (High Contrast) */
      --bg: #000000;
      --card: #09090b;  /* Zinc 950 */
      --surface2: #18181b; /* Zinc 900 */
      --text: #ffffff;  /* Pure White */
      --muted: #e4e4e7; /* Zinc 200 - High Contrast Grey */
      
      --border: #3f3f46; /* Zinc 700 - Sharper border */
      --grid: #27272a;   /* Zinc 800 */

      /* Monet Palette (Consistent with index.html) */
      --truth-awake: #40c4ff;    /* Light Blue A200 */
      --truth-sleep: #69f0ae;    /* Green A200 */
      
      --pred-awake: #448aff;     /* Blue A200 */
      --pred-sleep: #00e676;     /* Green A400 */
      --pred-uncertain: #ffd740; /* Amber A200 */
      
      /* Error Levels */
      --err-high: #ff5252;       /* Red A200 */
      --err-mid: #ffab40;        /* Orange A200 */
      --err-low: #ffff00;        /* Yellow A200 */
      
      --axis: #d4d4d8; /* Zinc 300 */
      
      /* Jitter intensity */
      --jitter-0: #18181b;
      --jitter-1: #90caf9;
      --jitter-2: #ffcc80;
      --jitter-3: #ef9a9a;
      
      --shadow-1: none;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Roboto", "Segoe UI", sans-serif;
      padding: 24px;
      -webkit-font-smoothing: antialiased;
    }}
    .wrap {{ max-width: 1600px; margin: 0 auto; }}
    
    .back-link {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 16px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--muted);
      text-decoration: none;
      font-size: 0.9rem;
      font-weight: 600;
      margin-bottom: 20px;
      transition: all 0.2s;
    }}
    .back-link:hover {{ 
      background: var(--surface2); 
      color: var(--text);
      border-color: #fff;
    }}

    .card {{ 
      background: var(--card); 
      border: 1px solid var(--border); 
      border-radius: 12px; 
      padding: 24px; 
      margin-bottom: 24px; 
      box-shadow: none;
    }}
    
    .title {{ font-size: 1.25rem; margin: 0 0 8px; font-weight: 800; color: #ffffff; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 16px; font-weight: 500; }}
    
    .legend {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: 0.85rem; color: var(--muted); margin-bottom: 16px; font-weight: 500; }}
    .tag {{ display: inline-flex; align-items: center; gap: 6px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; box-shadow: 0 0 5px currentColor; opacity: 1; }}
    
    .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 20px; }}
    .kpi {{ 
      background: var(--surface2); 
      border: 1px solid #27272a; 
      border-radius: 8px; 
      padding: 16px; 
      transition: all 0.2s;
    }}
    .kpi:hover {{ border-color: #fff; background: #27272a; }}
    .kpi .label {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 700; }}
    .kpi .val {{ margin-top: 6px; font-size: 1.5rem; font-weight: 700; color: #ffffff; }}
    
    .timeline-wrap {{ 
      overflow-x: auto; 
      overflow-y: hidden; 
      border: 1px solid var(--border); 
      border-radius: 8px; 
      background: #000;
      padding: 10px 0;
    }}
    #timeline {{ position: relative; height: 260px; min-width: 1600px; }}
    
    .tick-line {{ position: absolute; top: 28px; bottom: 0; width: 1px; background: #27272a; }}
    .tick-label {{ position: absolute; top: 4px; font-size: 0.75rem; color: var(--muted); white-space: nowrap; font-weight: 500; }}
    .tick-midnight {{ color: #fff; font-weight: 700; }}
    
    .lane-title {{ position: absolute; left: 16px; width: 60px; font-size: 0.8rem; color: #fff; font-weight: 700; text-align: right; }}
    .lane-bg {{ 
      position: absolute; left: 90px; right: 16px; 
      border-radius: 4px; 
      background: var(--surface2);
      opacity: 0.8;
      border: 1px solid #27272a;
    }}
    .seg {{ position: absolute; border-radius: 4px; min-width: 2px; }}
    
    .filter {{ display: flex; gap: 16px; align-items: center; color: var(--muted); font-size: 0.85rem; margin: 16px 0; font-weight: 500; }}
    .filter label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; }}
    
    .hint {{ color: #a1a1aa; font-size: 0.8rem; margin-top: 12px; font-style: italic; opacity: 1; }}
    
    .heat-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; background: #000; }}
    .heat {{ border-collapse: collapse; width: max-content; min-width: 100%; font-size: 0.8rem; }}
    .heat th, .heat td {{ border: 1px solid var(--grid); padding: 8px; text-align: center; color: var(--muted); }}
    .heat th {{ position: sticky; top: 0; background: var(--surface2); color: #fff; z-index: 2; font-weight: 700; border-bottom: 1px solid var(--border); }}
    
    .issue-wrap {{ overflow: auto; max-height: 400px; border: 1px solid var(--border); border-radius: 8px; background: #000; }}
    .issues {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
    .issues th, .issues td {{ border-bottom: 1px solid var(--grid); padding: 10px 16px; text-align: left; color: var(--muted); }}
    .issues th {{ position: sticky; top: 0; background: var(--surface2); color: #fff; z-index: 2; font-weight: 700; }}
    .issues tr:hover td {{ background: rgba(255,255,255,0.08); color: #fff; }}
    
    .pill {{ font-size: 0.75rem; border-radius: 6px; padding: 2px 10px; display: inline-block; font-weight: 700; text-transform: uppercase; }}
    
    /* Scrollbar */
    ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: #374151; border-radius: 4px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: #4b5563; }}
  </style>
</head>
<body>
  <div class="wrap">
    <a class="back-link" href="/">← 返回 Fitbit 仪表板</a>
    <div class="card">
      <h1 class="title">Sleep Model 差异看板</h1>
      <div class="sub">时间范围：{start_d.isoformat()} ~ {end_d.isoformat()}，真值口径：Fitbit 非 sleeping 全视为 awake，当前视图：{mode_title}</div>
      <div class="legend">
        <span class="tag"><i class="dot" style="background:var(--truth-sleep)"></i>真值 sleeping</span>
        <span class="tag"><i class="dot" style="background:var(--truth-awake)"></i>真值 awake</span>
        <span class="tag"><i class="dot" style="background:var(--pred-sleep)"></i>模型 sleeping</span>
        <span class="tag"><i class="dot" style="background:var(--pred-awake)"></i>模型 awake</span>
        {uncertain_legend}
        <span class="tag"><i class="dot" style="background:var(--err-high)"></i>awake→sleeping（高危）</span>
        <span class="tag"><i class="dot" style="background:var(--err-mid)"></i>sleeping→awake</span>
        {uncertain_error_legend}
      </div>
      <div class="kpis" id="kpis"></div>
      <div class="filter">
        <label><input id="fHigh" type="checkbox" checked /> 仅看 awake→sleeping</label>
        <label><input id="fMid" type="checkbox" checked /> 显示 sleeping→awake</label>
        {uncertain_filter}
      </div>
      <div class="timeline-wrap">
        <div id="timeline"></div>
      </div>
      <div class="hint">{uncertain_hint}</div>
    </div>

    <div class="card">
      <div class="title" style="font-size:16px">错误热力图（日期 × 小时）</div>
      <div class="sub">格子值是该小时累计错判分钟（含 high/mid/low），颜色越深问题越重</div>
      <div class="heat-wrap"><table class="heat" id="heat"></table></div>
    </div>

    <div class="card">
      <div class="title" style="font-size:16px">问题清单（可按持续时长观察最坏区间）</div>
      <div class="issue-wrap"><table class="issues" id="issues"></table></div>
    </div>
  </div>
  <script>
    const DATA = {data_json};
    const INCLUDE_UNCERTAIN = {str(include_uncertain).lower()};
    const MS_HOUR = 3600000;
    const MS_15M = 900000;
    const COLORS = {{
      truth: {{ sleeping: 'var(--truth-sleep)', awake: 'var(--truth-awake)' }},
      pred: {{ sleeping: 'var(--pred-sleep)', awake: 'var(--pred-awake)', uncertain: 'var(--pred-uncertain)' }},
      err: {{ high: 'var(--err-high)', mid: 'var(--err-mid)', low: 'var(--err-low)' }},
    }};

    function toTs(s) {{
      return new Date(String(s).replace(' ', 'T')).getTime();
    }}

    function fmtDate(ts) {{
      const d = new Date(ts);
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      return y + '-' + m + '-' + day + ' ' + hh + ':' + mm;
    }}

    function fmtDay(ts) {{
      const d = new Date(ts);
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return m + '-' + day;
    }}

    function buildSegments(points, key) {{
      const segs = [];
      for (let i = 0; i < points.length - 1; i++) {{
        const start = points[i].ts;
        const end = points[i + 1].ts;
        if (end <= start) continue;
        const state = points[i][key];
        const last = segs.length ? segs[segs.length - 1] : null;
        if (last && last.state === state && start - last.end <= 600000) {{
          last.end = end;
        }} else {{
          segs.push({{ state, start, end }});
        }}
      }}
      return segs;
    }}

    function buildErrorSegments(points) {{
      const out = [];
      for (let i = 0; i < points.length - 1; i++) {{
        const p = points[i];
        const start = p.ts;
        const end = points[i + 1].ts;
        if (end <= start) continue;
        let type = '';
        let level = '';
        if (p.truth === 'awake' && p.pred === 'sleeping') {{
          type = 'awake_as_sleeping';
          level = 'high';
        }} else if (p.truth === 'sleeping' && p.pred === 'awake') {{
          type = 'sleeping_as_awake';
          level = 'mid';
        }} else if (p.truth === 'sleeping' && p.pred === 'uncertain') {{
          type = 'sleeping_as_uncertain';
          level = 'low';
        }}
        if (!type) continue;
        const last = out.length ? out[out.length - 1] : null;
        if (last && last.type === type && start - last.end <= 600000) {{
          last.end = end;
        }} else {{
          out.push({{ type, level, start, end }});
        }}
      }}
      return out;
    }}

    function buildJitterBuckets(points) {{
      const map = new Map();
      for (let i = 1; i < points.length; i++) {{
        const a = points[i - 1];
        const b = points[i];
        if (a.truth !== 'sleeping' || b.truth !== 'sleeping') continue;
        if (a.pred === b.pred) continue;
        const key = Math.floor(b.ts / MS_15M) * MS_15M;
        map.set(key, (map.get(key) || 0) + 1);
      }}
      const out = [];
      for (const [start, count] of map.entries()) {{
        out.push({{ start, end: start + MS_15M, count }});
      }}
      out.sort((a, b) => a.start - b.start);
      return out;
    }}

    function minuteSum(segs, level) {{
      return segs
        .filter((s) => !level || s.level === level)
        .reduce((acc, s) => acc + (s.end - s.start) / 60000, 0);
    }}

    function makeEl(tag, className) {{
      const el = document.createElement(tag);
      if (className) el.className = className;
      return el;
    }}

    function init() {{
      if (!DATA.length) return;
      // 1. 预处理点位与区间数据
      const points = DATA.map((d) => ({{
        ts: toTs(d.ts),
        pred: d.pred,
        truth: d.truth,
      }})).filter((d) => Number.isFinite(d.ts)).sort((a, b) => a.ts - b.ts);
      if (points.length < 2) return;
      const t0 = points[0].ts;
      const t1 = points[points.length - 1].ts;
      const pxPerHour = 36;
      const trackW = Math.max(1600, Math.ceil(((t1 - t0) / MS_HOUR) * pxPerHour) + 100);
      const xAt = (ts) => 80 + ((ts - t0) / Math.max(1, t1 - t0)) * (trackW - 92);

      const truthSegs = buildSegments(points, 'truth');
      const predSegs = buildSegments(points, 'pred');
      const errSegs = buildErrorSegments(points);
      const jitter = buildJitterBuckets(points);

      // 2. KPI 汇总，突出你最关心的高危错判和横跳
      const kpis = [
        ['高危 awake→sleeping', minuteSum(errSegs, 'high').toFixed(1) + ' min'],
        ['sleeping→awake', minuteSum(errSegs, 'mid').toFixed(1) + ' min'],
        ['睡眠中横跳(总次数)', String(jitter.reduce((a, b) => a + b.count, 0))],
        ['异常区间数', String(errSegs.length)],
      ];
      if (INCLUDE_UNCERTAIN) {{
        kpis.splice(2, 0, ['sleeping→uncertain', minuteSum(errSegs, 'low').toFixed(1) + ' min']);
      }}
      const kpiRoot = document.getElementById('kpis');
      kpis.forEach((k) => {{
        const box = makeEl('div', 'kpi');
        const l = makeEl('div', 'label');
        l.textContent = k[0];
        const v = makeEl('div', 'val');
        v.textContent = k[1];
        box.appendChild(l);
        box.appendChild(v);
        kpiRoot.appendChild(box);
      }});

      // 3. 主时间轴：四层轨道 + 每小时刻度文本
      const timeline = document.getElementById('timeline');
      timeline.style.width = trackW + 'px';
      const lanes = [
        {{ name: '真值', y: 34, h: 36 }},
        {{ name: '模型', y: 88, h: 36 }},
        {{ name: '错误', y: 142, h: 36 }},
        {{ name: '横跳', y: 196, h: 36 }},
      ];
      lanes.forEach((ln) => {{
        const t = makeEl('div', 'lane-title');
        t.style.top = ln.y + 10 + 'px';
        t.textContent = ln.name;
        timeline.appendChild(t);
        const bg = makeEl('div', 'lane-bg');
        bg.style.top = ln.y + 'px';
        bg.style.height = ln.h + 'px';
        timeline.appendChild(bg);
      }});

      const hourStart = Math.floor(t0 / MS_HOUR) * MS_HOUR;
      for (let ts = hourStart; ts <= t1; ts += MS_HOUR) {{
        const line = makeEl('div', 'tick-line');
        line.style.left = xAt(ts) + 'px';
        timeline.appendChild(line);
        const d = new Date(ts);
        if (d.getHours() % 2 !== 0 && d.getHours() !== 0) continue;
        const lab = makeEl('div', 'tick-label');
        lab.style.left = xAt(ts) + 2 + 'px';
        if (d.getHours() === 0) {{
          lab.classList.add('tick-midnight');
          lab.textContent = fmtDay(ts) + ' 00:00';
        }} else {{
          lab.textContent = String(d.getHours()).padStart(2, '0') + ':00';
        }}
        timeline.appendChild(lab);
      }}

      function addSeg(seg, lane, color) {{
        const el = makeEl('div', 'seg');
        el.style.top = lane.y + 4 + 'px';
        el.style.height = lane.h - 8 + 'px';
        el.style.left = xAt(seg.start) + 'px';
        el.style.width = Math.max(1, xAt(seg.end) - xAt(seg.start)) + 'px';
        el.style.background = color;
        el.title = fmtDate(seg.start) + ' ~ ' + fmtDate(seg.end);
        timeline.appendChild(el);
      }}

      truthSegs.forEach((s) => addSeg(s, lanes[0], COLORS.truth[s.state]));
      predSegs.forEach((s) => addSeg(s, lanes[1], COLORS.pred[s.state]));

      const fHigh = document.getElementById('fHigh');
      const fMid = document.getElementById('fMid');
      const fLow = document.getElementById('fLow');
      function drawIssues() {{
        timeline.querySelectorAll('.issue-seg').forEach((n) => n.remove());
        errSegs.forEach((s) => {{
          if (s.level === 'high' && !fHigh.checked) return;
          if (s.level === 'mid' && !fMid.checked) return;
          if (s.level === 'low' && (!fLow || !fLow.checked)) return;
          const el = makeEl('div', 'seg issue-seg');
          el.style.top = lanes[2].y + 4 + 'px';
          el.style.height = lanes[2].h - 8 + 'px';
          el.style.left = xAt(s.start) + 'px';
          el.style.width = Math.max(1, xAt(s.end) - xAt(s.start)) + 'px';
          el.style.background = COLORS.err[s.level];
          const mins = ((s.end - s.start) / 60000).toFixed(1);
          el.title = s.type + ' | ' + fmtDate(s.start) + ' ~ ' + fmtDate(s.end) + ' | ' + mins + ' min';
          timeline.appendChild(el);
        }});
      }}
      [fHigh, fMid, fLow].filter(Boolean).forEach((f) => f.addEventListener('change', drawIssues));
      drawIssues();

      jitter.forEach((j) => {{
        const el = makeEl('div', 'seg');
        el.style.top = lanes[3].y + 4 + 'px';
        el.style.height = lanes[3].h - 8 + 'px';
        el.style.left = xAt(j.start) + 'px';
        el.style.width = Math.max(1, xAt(j.end) - xAt(j.start)) + 'px';
        el.style.background = j.count >= 3 ? 'var(--jitter-3)' : (j.count === 2 ? 'var(--jitter-2)' : (j.count === 1 ? 'var(--jitter-1)' : 'var(--jitter-0)'));
        el.title = '横跳 ' + j.count + ' 次 | ' + fmtDate(j.start) + ' ~ ' + fmtDate(j.end);
        timeline.appendChild(el);
      }});

      // 4. 热力图：日期×小时的错判分钟
      const heatMap = new Map();
      for (let i = 0; i < points.length - 1; i++) {{
        const p = points[i];
        const next = points[i + 1];
        let bad = false;
        if (p.truth === 'awake' && p.pred === 'sleeping') bad = true;
        if (p.truth === 'sleeping' && p.pred === 'awake') bad = true;
        if (p.truth === 'sleeping' && p.pred === 'uncertain') bad = true;
        if (!bad) continue;
        const key = fmtDate(p.ts).slice(0, 13);
        const mins = Math.max(0, (next.ts - p.ts) / 60000);
        heatMap.set(key, (heatMap.get(key) || 0) + mins);
      }}

      const days = [];
      for (let ts = new Date(new Date(t0).setHours(0, 0, 0, 0)).getTime(); ts <= t1; ts += 24 * MS_HOUR) {{
        days.push(fmtDate(ts).slice(0, 10));
      }}
      const heat = document.getElementById('heat');
      const hr = makeEl('tr');
      const c0 = makeEl('th');
      c0.textContent = '日期';
      hr.appendChild(c0);
      for (let h = 0; h < 24; h++) {{
        const th = makeEl('th');
        th.textContent = String(h).padStart(2, '0');
        hr.appendChild(th);
      }}
      heat.appendChild(hr);
      days.forEach((d) => {{
        const tr = makeEl('tr');
        const td0 = makeEl('td');
        td0.textContent = d.slice(5);
        tr.appendChild(td0);
        for (let h = 0; h < 24; h++) {{
          const key = d + ' ' + String(h).padStart(2, '0');
          const v = Number((heatMap.get(key) || 0).toFixed(1));
          const td = makeEl('td');
          td.textContent = v > 0 ? String(v) : '';
          
          if (v > 0) {{
            // Use a more vibrant red (Red 400) for better visibility against dark bg
            // Clamp alpha between 0.15 and 0.95 so even small errors are visible
            const alpha = Math.min(0.95, Math.max(0.15, v / 40)); 
            td.style.background = 'rgba(239, 83, 80, ' + alpha + ')';
            // Switch text to dark if background is bright (high error)
            if (alpha > 0.5) {{
              td.style.color = '#000000';
              td.style.fontWeight = '600';
              td.style.textShadow = 'none';
            }} else {{
              td.style.color = '#ffcdd2'; // Light Red 100 for contrast on dark/low-alpha red
              td.style.fontWeight = 'normal';
              td.style.textShadow = '0 0 2px rgba(0,0,0,0.5)';
            }}
          }} else {{
            td.style.background = 'transparent';
          }}
          tr.appendChild(td);
        }}
        heat.appendChild(tr);
      }});

      // 5. 问题清单：持续时间降序，直接给最坏区间
      const issues = document.getElementById('issues');
      const head = makeEl('tr');
      ['类型', '开始', '结束', '持续(min)', '等级'].forEach((t) => {{
        const th = makeEl('th');
        th.textContent = t;
        head.appendChild(th);
      }});
      issues.appendChild(head);
      const rank = {{ high: 3, mid: 2, low: 1 }};
      errSegs
        .slice()
        .sort((a, b) => (b.end - b.start) - (a.end - a.start))
        .forEach((s) => {{
          const tr = makeEl('tr');
          const type = makeEl('td');
          type.textContent = s.type;
          const st = makeEl('td');
          st.textContent = fmtDate(s.start);
          const ed = makeEl('td');
          ed.textContent = fmtDate(s.end);
          const du = makeEl('td');
          du.textContent = ((s.end - s.start) / 60000).toFixed(1);
          const lv = makeEl('td');
          const pill = makeEl('span', 'pill');
          pill.style.background = COLORS.err[s.level];
          pill.style.color = '#0a101a';
          pill.textContent = s.level.toUpperCase() + ' (' + rank[s.level] + ')';
          lv.appendChild(pill);
          [type, st, ed, du, lv].forEach((n) => tr.appendChild(n));
          issues.appendChild(tr);
        }});
    }}
    init();
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    start_d = date.fromisoformat(args.start_date)
    end_d = date.fromisoformat(args.end_date) if args.end_date else date.today()
    if start_d > end_d:
        raise ValueError("start-date 不能晚于 end-date")

    rows = build_rows(start_d, end_d, args.binary_mode)
    html = render_html(rows, start_d, end_d, args.binary_mode)
    out_path = DATA_DIR / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[ok] report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
