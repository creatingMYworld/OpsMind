/* OpsMind dashboard.
   Talks only to the OpsMind API on the same origin. The browser never holds a
   Google Cloud credential: every GCP call happens server-side using the Cloud
   Run runtime service account. */
(() => {
"use strict";

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const state = {
  view: "overview", window: 15, params: {}, paused: false, logs: [], maxLogs: 1200,
  services: [], es: null, meta: null, charts: {}, incidents: [], scenarios: [],
};

/* ---------- helpers ---------- */
const nf = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
  : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const usd = (v, d = 4) => (v === null || v === undefined) ? "—" : "$" + Number(v).toFixed(d);
const inr = (v) => (v === null || v === undefined) ? "—" : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const pct = (v, d = 1) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d) + "%";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const hms  = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
const dur  = (s) => s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s/60)}m ${Math.round(s%60)}s` : `${(s/3600).toFixed(1)}h`;

/* The newest minute bucket is still being filled, so plotting it makes every
   chart dive toward zero at the right edge and a healthy system look like it
   just died. Charts therefore show completed minutes only; the hero tiles and
   the rule engine still use the live, partial minute. */
function complete(points) {
  if (!points || points.length < 2) return points || [];
  const nowMinute = Math.floor(Date.now() / 60000);
  return points[points.length - 1].minute >= nowMinute ? points.slice(0, -1) : points;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

/* ---------- charts ---------- */
function chartDefaults() {
  const grid = css("--border-soft"), tick = css("--text-faint");
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 220 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: css("--text-dim"), boxWidth: 10, boxHeight: 10, font: { size: 11 }, usePointStyle: true } },
      tooltip: { backgroundColor: css("--bg-elev2"), borderColor: css("--border"), borderWidth: 1,
                 titleColor: css("--text"), bodyColor: css("--text-dim"), padding: 9, displayColors: true },
    },
    scales: {
      x: { grid: { color: grid, drawBorder: false }, ticks: { color: tick, font: { size: 10 }, maxRotation: 0, autoSkipPadding: 18 } },
      y: { grid: { color: grid, drawBorder: false }, ticks: { color: tick, font: { size: 10 } }, beginAtZero: true },
    },
  };
}
function upsert(key, canvasId, type, data, optOverrides = {}) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  const base = chartDefaults();
  const options = Object.assign({}, base, optOverrides, {
    plugins: Object.assign({}, base.plugins, optOverrides.plugins || {}),
    scales: optOverrides.scales === null ? undefined
          : Object.assign({}, base.scales, optOverrides.scales || {}),
  });
  if (state.charts[key]) {
    const c = state.charts[key];
    c.data = data; c.options = options; c.update("none"); return;
  }
  state.charts[key] = new Chart(el.getContext("2d"), { type, data, options });
}
function destroyCharts() { Object.values(state.charts).forEach(c => c.destroy()); state.charts = {}; }

/* ---------- navigation ----------
   Routing lives in the URL hash, and filters travel with it. Drill-down is the
   point: see a spike, click it, land on the logs already filtered to it. That
   only works if the destination receives the context, which means it belongs
   in the URL rather than in a variable — and as a side effect every view
   becomes linkable and the browser's back button does the right thing. */

function parseHash() {
  const raw = (location.hash || "#overview").slice(1);
  const [view, query = ""] = raw.split("?");
  return { view: view || "overview", params: Object.fromEntries(new URLSearchParams(query)) };
}
function buildHash(view, params = {}) {
  const clean = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== "ALL");
  const q = new URLSearchParams(clean).toString();
  return `#${view}${q ? "?" + q : ""}`;
}
function go(view, params = {}) { location.hash = buildHash(view, params); }

function applyRoute() {
  const { view, params } = parseHash();
  // Errors now live inside Logs, and Projects inside Setup. Old links still land.
  const ALIAS = { errors: "logs", projects: "setup" };
  const target = ALIAS[view] || view;
  const known = $("#view-" + target);
  state.view = known ? target : "overview";
  state.params = params;

  $$("#nav button").forEach(x => x.classList.toggle("active", x.dataset.view === state.view));
  $$(".view").forEach(v => v.classList.remove("active"));
  $("#view-" + state.view).classList.add("active");
  const head = $("#view-" + state.view + " > h2:first-child");
  const lede = head && head.nextElementSibling && head.nextElementSibling.classList.contains("lede") ? head.nextElementSibling : null;
  $("#pageTitle").textContent = head ? head.childNodes[0].textContent.trim() : "";
  $("#pageSub").textContent = lede ? lede.textContent.trim() : "";

  // A drill-down carries its filters; adopt them before the view renders.
  if (state.view === "logs") {
    if (params.severity !== undefined) $("#logSev").value = params.severity;
    if (params.service !== undefined) $("#logSvc").value = params.service;
    if (params.q !== undefined) $("#logQ").value = params.q;
    state.logs = [];
    startStream();
  }
  if (params.window) {
    state.window = +params.window;
    $("#windowSel").value = String(state.window);
  }
  scheduleRefresh();
  refresh();
}

$$("#nav button").forEach(b => b.addEventListener("click", () => go(b.dataset.view)));
addEventListener("hashchange", applyRoute);
$("#windowSel").addEventListener("change", e => {
  state.window = +e.target.value;
  scheduleRefresh();
  refresh();
});

/* Refresh cadence follows the window. A 1-minute view is useless unless it
   actually moves; a 3-hour view re-polled every 5s is pure waste — of browser
   work, of server work, and of the Cloud Logging API quota behind it. */
const REFRESH_MS = { 1: 5000, 5: 10000, 15: 20000, 60: 45000, 180: 120000 };
let refreshTimer = null;
function scheduleRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  const ms = REFRESH_MS[state.window] || 20000;
  refreshTimer = setInterval(() => { if (!document.hidden) refresh(); }, ms);
  const sel = $("#windowSel");
  if (sel) sel.title = `Re-polled every ${Math.round(ms / 1000)}s at this window`;
  const lbl = $("#liveLabel");
  if (lbl) lbl.textContent = `Live ${Math.round(ms / 1000)}s`;
}
$("#themeBtn").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  destroyCharts(); refresh();
});

/* ---------- drawer ---------- */
function openDrawer(title, sub, html) {
  $("#drawerTitle").innerHTML = title; $("#drawerSub").innerHTML = sub || "";
  $("#drawerBody").innerHTML = html;
  $("#drawer").classList.add("open"); $("#drawerBg").classList.add("open");
}
function closeDrawer() { $("#drawer").classList.remove("open"); $("#drawerBg").classList.remove("open"); }
$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawerBg").addEventListener("click", closeDrawer);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });

/* ---------- meta / header ---------- */
async function loadMeta() {
  const m = await api("/api/v1/meta"); state.meta = m;
  const src = m.config.dataSource;
  const badge = $("#srcBadge");
  badge.className = "pill " + (src === "gcp" ? "ok" : "info");
  badge.innerHTML = `<span class="dot"></span> ${src === "gcp" ? "GCP — Cloud Logging" : "LOCAL — direct ingest"}`;
  const rp = $("#railProject");
  if (rp) rp.textContent = m.config.activeProject || m.config.projectId || "local";
  $("#railMeta").innerHTML =
    `${esc(m.store.bufferedEntries)} entries buffered<br>` +
    `${esc(m.store.errorGroups)} error groups<br>` +
    (m.config.projectId ? `project ${esc(m.config.projectId)}` : "no GCP project");
  if (m.ruleWarnings && m.ruleWarnings.length) {
    $("#railMeta").innerHTML += `<br><span style="color:var(--err)">${m.ruleWarnings.length} rule warning(s)</span>`;
  }
}

function setHealthPill(h) {
  const cls = { HEALTHY: "ok", DEGRADED: "warn", IMPAIRED: "err", CRITICAL: "crit" }[h.status] || "muted";
  const hp = $("#healthPill");
  hp.className = "pill " + cls;
  hp.innerHTML = `<span class="dot"></span> ${esc(h.label)} · ${nf(h.score, 1)}`;
  return cls;   // the hero tile is coloured to match
}

/* ---------- overview: key numbers ----------
   Four tiles, each a link to the page that explains it: the number, a
   sparkline of the same quantity across the window, and how the second half
   of the window compares with the first. */
function sparkline(values, color) {
  const v = values.filter(x => Number.isFinite(x));
  if (v.length < 2) return "";
  const w = 84, h = 26, max = Math.max(...v), min = Math.min(...v), span = max - min || 1;
  const d = v.map((x, i) => `${i ? "L" : "M"}${(i / (v.length - 1) * w).toFixed(1)},${(h - 2 - (x - min) / span * (h - 4)).toFixed(1)}`).join(" ");
  return `<svg class="kspark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">` +
    `<path d="${d}" fill="none" stroke="var(${color})" stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}
function halves(values) {
  const v = values.filter(x => Number.isFinite(x));
  if (v.length < 4) return null;
  const mid = Math.floor(v.length / 2);
  const avg = a => a.reduce((s, x) => s + x, 0) / a.length;
  return { first: avg(v.slice(0, mid)), second: avg(v.slice(mid)) };
}
function kTile({ label, value, valueColor, spark, sparkColor, delta, href }) {
  return `<a class="card ktile" href="${href}">
    <div class="ktile-label">${esc(label)}<span class="ktile-go" aria-hidden="true">→</span></div>
    <div class="ktile-row">
      <div class="ktile-value"${valueColor ? ` style="color:var(${valueColor})"` : ""}>${value}</div>
      ${sparkline(spark, sparkColor)}
    </div>
    <div class="ktile-foot">${delta}</div>
  </a>`;
}
/* "↑ 12% vs first half". goodWhenUp decides green vs red. */
function deltaLine(h, goodWhenUp, fmt) {
  if (!h) return `<span class="faint">not enough data for a trend</span>`;
  const diff = h.second - h.first;
  if (Math.abs(diff) < 1e-9) return `<span class="faint">→ flat vs first half</span>`;
  const up = diff > 0, good = up === goodWhenUp;
  return `<span style="color:var(${good ? "--ok" : "--err"})">${up ? "↑" : "↓"}</span> ${fmt(Math.abs(diff))} vs first half`;
}
function renderKeyNumbers({ t, c, o, points, costSeries, incs }) {
  const req = points.map(p => p.requests);
  const errRate = points.map(p => p.requests ? (p.errors5xx / p.requests) * 100 : 0);
  const spend = costSeries.map(p => Object.values(p.usdPerHour || {}).reduce((s, x) => s + (+x || 0), 0));

  // Open incidents at each step: started before it, not resolved before it.
  const all = incs ? [...(incs.breaching || []), ...(incs.resolved || [])] : [];
  const openAt = points.map(p => {
    const ts = p.ts > 1e12 ? p.ts / 1000 : p.ts;
    return all.filter(i => i.startedAt <= ts && (!i.resolvedAt || i.resolvedAt > ts)).length;
  });

  const open = o.incidents.openCount;
  $("#heroTiles").innerHTML = [
    kTile({ label: "Modeled spend", value: usd(c.usdPerHour, 4) + "<span class='kunit'>/hr</span>",
            valueColor: "--cost", spark: spend, sparkColor: "--cost", href: "#cost",
            delta: deltaLine(halves(spend), false, x => usd(x, 4) + "/hr") }),
    kTile({ label: "Active incidents", value: nf(open), valueColor: open ? "--err" : "--ok",
            spark: openAt, sparkColor: "--err", href: "#incidents",
            delta: open ? `${nf(o.incidents.open.filter(i => i.severity === "CRITICAL").length)} critical · open now`
                        : `<span class="faint">none open</span>` }),
    kTile({ label: "Requests", value: nf(t.requests), spark: req, sparkColor: "--accent", href: "#logs",
            delta: deltaLine(halves(req), true, x => nf(Math.round(x)) + " req/min") }),
    kTile({ label: "Error rate", value: pct(t.errorRatePct, 2),
            valueColor: t.errorRatePct > 5 ? "--err" : t.errorRatePct > 1 ? "--warn" : null,
            spark: errRate, sparkColor: "--err", href: "#logs?severity=ERROR",
            delta: deltaLine(halves(errRate), false, x => x.toFixed(2) + " pp") }),
  ].join("");
}

/* ---------- overview: what is failing ---------- */
const svcShort = n => String(n || "").replace(/^cognikart-/, "");
function chip(text, tone) {
  return `<span class="sec-chip" style="--chip:var(${tone})"><span class="dot"></span>${esc(text)}</span>`;
}
function linkTile({ label, value, valueColor, foot, href, spark, sparkColor }) {
  return `<a class="card ktile" href="${href}">
    <div class="ktile-label">${esc(label)}<span class="ktile-go" aria-hidden="true">→</span></div>
    <div class="ktile-row"><div class="ktile-value"${valueColor ? ` style="color:var(${valueColor})"` : ""}>${value}</div>
      ${spark ? sparkline(spark, sparkColor) : ""}</div>
    <div class="ktile-foot">${foot}</div></a>`;
}
function renderFailing({ t, points, routes, actions }) {
  const cpuSeries = points.map(p => p.cpuPctMax);
  const cpu = cpuSeries.filter(v => v != null);
  const inst = points.map(p => p.instanceCount).filter(v => v != null);
  const cpuNow = cpu.length ? cpu[cpu.length - 1] : null;
  $("#failTiles").innerHTML = [
    linkTile({ label: "Server errors (5xx)", value: nf(t.errors5xx), valueColor: t.errors5xx ? "--err" : null,
               spark: points.map(p => p.errors5xx || 0), sparkColor: "--err",
               foot: `${pct(t.errorRatePct, 2)} of traffic · our fault`, href: "#logs?severity=ERROR" }),
    linkTile({ label: "Client errors (4xx)", value: nf(t.errors4xx), valueColor: t.errors4xx ? "--warn" : null,
               spark: points.map(p => p.errors4xx || 0), sparkColor: "--warn",
               foot: `${pct(t.clientErrorRatePct, 2)} of traffic · caller side`, href: "#logs?severity=WARNING" }),
    linkTile({ label: "Slow requests", value: routes ? nf(routes.slowRequests) : "—",
               spark: points.map(p => p.p95LatencyMs || 0), sparkColor: "--accent",
               foot: routes ? `over ${nf(routes.slowThresholdMs)}ms · line is p95 latency` : "route data unavailable", href: "#services" }),
    linkTile({ label: "CPU utilization", value: cpuNow == null ? "—" : `${nf(cpuNow, 0)}%`,
               valueColor: cpuNow > 80 ? "--err" : cpuNow > 60 ? "--warn" : null,
               spark: cpu.length > 1 ? cpuSeries.map(v => v ?? 0) : null, sparkColor: "--info",
               foot: cpu.length ? `${nf(Math.max(...inst, 0))} instance(s) · peak per minute` : "no CPU heartbeats yet", href: "#resources" }),
  ].join("");

  const firing = (actions && actions.actions) || [];
  $("#failChip").innerHTML = firing.length
    ? chip(firing.length > 1 ? `${firing[0].title}, and ${firing.length - 1} more need attention` : `${firing[0].title} needs attention`,
           firing[0].severity === "CRITICAL" || firing[0].severity === "HIGH" ? "--err" : "--warn")
    : chip("Nothing needs attention", "--ok");
}

const SEV_GROUPS = [
  { key: "INFO", label: "INFO", color: "--text-faint", from: ["DEBUG", "INFO", "DEFAULT", "NOTICE"] },
  { key: "WARNING", label: "WARNING", color: "--warn", from: ["WARNING"] },
  { key: "ERROR", label: "ERROR", color: "--err", from: ["ERROR", "CRITICAL", "ALERT", "EMERGENCY"] },
];
function sevOf(p, g) { return g.from.reduce((s, k) => s + ((p.severity || {})[k] || 0), 0); }

function renderSeverity(allPoints) {
  // The current minute is still filling; drawn as a bar it reads as a sudden
  // drop in traffic. It is left out of the chart, not out of the totals.
  const nowMin = Math.floor(Date.now() / 60000);
  const points = allPoints.filter(p => Math.floor(p.ts / 60) < nowMin);
  const labels = points.map(p => hhmm(p.ts));
  upsert("severity", "chSeverity", "bar", {
    labels,
    datasets: SEV_GROUPS.map(g => ({
      label: g.label, data: points.map(p => sevOf(p, g)),
      backgroundColor: g.key === "INFO" ? css("--text-faint") + "8c" : css(g.color),
      stack: "s", borderRadius: 2, barPercentage: .62, categoryPercentage: .9, maxBarThickness: 26,
    })),
  }, {
    plugins: { legend: { display: false } },
    scales: {
      x: { stacked: true, grid: { display: false }, ticks: { color: css("--text-faint"), font: { size: 10 }, maxRotation: 0, autoSkipPadding: 24 } },
      y: { stacked: true, grid: { color: css("--border-soft") }, ticks: { color: css("--text-faint"), font: { size: 10 } }, beginAtZero: true },
    },
  });
  $("#sevLegend").innerHTML = SEV_GROUPS.map(g =>
    `<span><i style="background:var(${g.color})"></i>${g.label}</span>`).join("");

  const totals = SEV_GROUPS.map(g => ({ ...g, n: allPoints.reduce((s, p) => s + sevOf(p, g), 0) }));
  const all = totals.reduce((s, x) => s + x.n, 0) || 1;
  const req = allPoints.reduce((s, p) => s + (p.requests || 0), 0);
  const e5 = allPoints.reduce((s, p) => s + (p.errors5xx || 0), 0);
  const e4 = allPoints.reduce((s, p) => s + (p.errors4xx || 0), 0);
  $("#sevSplit").innerHTML = totals.map(x => `
    <a class="sev-row" href="#logs?severity=${x.key === "INFO" ? "INFO" : x.key}">
      <div class="sev-top"><span class="sev-tag" style="--c:var(${x.color})"><span class="dot"></span>${x.label}</span>
        <span class="sev-n">${nf(x.n)}</span></div>
      <div class="sev-bar"><span style="width:${(x.n / all * 100).toFixed(1)}%;background:var(${x.color})"></span></div>
      <div class="sev-pct">${pct(x.n / all * 100, 1)} of all entries</div>
    </a>`).join("") + `
    <div class="sev-family">
      <div class="sev-family-h">By HTTP status family</div>
      <div><span><b>2xx</b> Success</span><span>${nf(Math.max(0, req - e5 - e4))}</span></div>
      <div><span><b>4xx</b> Client error</span><span>${nf(e4)}</span></div>
      <div><span><b>5xx</b> Server error</span><span>${nf(e5)}</span></div>
    </div>`;
}

/* ---------- overview: health by service ---------- */
function gradeOf(s) {
  if (s.errorRate > 0.05) return { word: "CRITICAL", cls: "err" };
  if (s.errorRate > 0.01 || (s.p95LatencyMs || 0) > 2000) return { word: "DEGRADED", cls: "warn" };
  return { word: "HEALTHY", cls: "ok" };
}
function renderServiceHealth(svcs, groups, series) {
  const tbl = $("#svcTable");
  if (!svcs.length) {
    tbl.innerHTML = `<tbody><tr><td class="empty">No services reporting yet.</td></tr></tbody>`;
    $("#svcChip").innerHTML = "";
    return;
  }
  const rows = svcs.map((s, i) => {
    const pts = series[i] || [];
    const requests = pts.reduce((a, p) => a + (p.requests || 0), 0);
    const top = groups.filter(g => g.service === s.service).sort((a, b) => b.count - a.count)[0];
    return { s, g: gradeOf(s), requests, pts, top };
  });
  const order = { err: 0, warn: 1, ok: 2 };
  rows.sort((a, b) => order[a.g.cls] - order[b.g.cls] || b.requests - a.requests);

  tbl.innerHTML = `<thead><tr><th>Service</th><th>Grade</th><th class="right">Requests</th>
    <th class="right">Error rate</th><th class="right">P95</th><th>Trend</th><th>Top failure</th></tr></thead><tbody>` +
    rows.map(({ s, g, requests, pts, top }) => {
      const tone = g.cls === "err" ? "--err" : "--accent";
      return `<tr class="clickable" data-svc="${esc(s.service)}">
        <td class="mono svc-name">${esc(svcShort(s.service))}</td>
        <td><span class="pill ${g.cls}"><span class="dot"></span>${g.word}</span></td>
        <td class="num">${nf(requests)}</td>
        <td class="num" style="color:${s.errorRate > 0.01 ? "var(--err)" : "inherit"}">${pct(s.errorRate * 100, 1)}</td>
        <td class="num" style="color:${(s.p95LatencyMs || 0) > 500 ? "var(--warn)" : "inherit"}">${s.p95LatencyMs == null ? "—" : nf(s.p95LatencyMs) + "ms"}</td>
        <td>${sparkline(pts.map(p => p.requests || 0), tone)}</td>
        <td class="mono faint top-fail">${top ? `${esc(top.errorCode || top.pattern || "error")} ×${nf(top.count)}` : "—"}</td>
      </tr>`;
    }).join("") + `</tbody>`;
  $$("#svcTable [data-svc]").forEach(tr => tr.addEventListener("click", () => go("resources", { service: tr.dataset.svc })));

  const worst = rows[0];
  $("#svcChip").innerHTML = worst.g.cls === "ok"
    ? chip("All services healthy", "--ok")
    : chip(`${svcShort(worst.s.service)} is ${worst.g.word.toLowerCase()}`, worst.g.cls === "err" ? "--err" : "--warn");
}

/* ---------- overview: failures and slow paths ---------- */
function ranked(items, empty) {
  if (!items.length) return `<div class="empty">${empty}</div>`;
  const max = Math.max(...items.map(x => x.value), 1);
  return items.map(x => `
    <a class="rank-row" href="${x.href}">
      <div class="rank-top"><span class="mono rank-label">${esc(x.label)}</span><span class="rank-val">${x.display}</span></div>
      <div class="rank-bar"><span style="width:${Math.max(2, x.value / max * 100).toFixed(1)}%;background:var(${x.color})"></span></div>
    </a>`).join("");
}
function renderTopFailures(groups) {
  const byLabel = new Map();
  for (const g of groups) {
    const label = g.errorCode || g.pattern || "error";
    const cur = byLabel.get(label) || { label, value: 0, sev: g.severity, service: g.service };
    cur.value += g.count;
    byLabel.set(label, cur);
  }
  const items = [...byLabel.values()].sort((a, b) => b.value - a.value).slice(0, 6).map((x, i) => ({
    ...x, display: nf(x.value),
    color: i === 0 || x.sev === "ERROR" || x.sev === "CRITICAL" ? "--err" : "--warn",
    href: buildHash("logs", { q: x.label }),
  }));
  $("#topFailures").innerHTML = ranked(items, "No failures in this window.");
}
function renderSlowRoutes(routes) {
  const rows = (routes && routes.routes || []).filter(r => r.p95LatencyMs != null).slice(0, 6);
  const items = rows.map(r => ({
    label: r.route, value: r.p95LatencyMs, display: nf(r.p95LatencyMs) + "ms",
    color: r.p95LatencyMs > 500 ? "--warn" : "--accent",
    href: buildHash("logs", { q: r.route }),
  }));
  $("#slowRoutes").innerHTML = ranked(items, routes ? "No requests in this window." : "Route data unavailable.");
}

/* ---------- overview: status strip ----------
   Project identity on one row, then the five numbers that say whether it is
   healthy. Same shape in every cell: name, value, qualifier. */
function renderStatusStrip(o, cls) {
  const m = state.meta ? state.meta.config : {};
  const h = o.health, t = o.technical, a = o.actions || { firingNow: 0, total: 0 };
  const svcs = o.services || [];
  const healthy = svcs.filter(s => !(s.errorRate > 0.01 || (s.p95LatencyMs || 0) > 2000)).length;
  const hasTraffic = t.requests > 0;
  const tone = { ok: "--ok", warn: "--warn", err: "--err", crit: "--crit" }[cls] || "--text-faint";
  const word = { HEALTHY: "Healthy", DEGRADED: "Degraded", IMPAIRED: "Impaired", CRITICAL: "Critical" }[h.status] || h.status;
  const project = m.activeProject || m.projectId || "Local";
  const meta = [m.dataSource === "gcp" ? "Google Cloud" : "local ingest", m.region].filter(Boolean).join(" · ");

  const cell = (label, value, sub, color) =>
    `<div class="strip-cell"><div class="strip-label">${label}</div>` +
    `<div class="strip-value"${color ? ` style="color:var(${color})"` : ""}>${value}</div>` +
    `<div class="strip-sub">${sub}</div></div>`;

  $("#statusStrip").innerHTML =
    `<div class="strip-head">
       <span class="strip-name">${esc(project)}</span>
       <span class="strip-meta mono">${esc(meta)}</span>
       <span class="strip-fresh">live · ${state.window}-minute window</span>
     </div>
     <div class="strip-row">
       <div class="strip-cell strip-lead" style="--lead:var(${tone})">
         <div class="strip-label">Overall health</div>
         <div class="strip-value" style="color:var(${tone})">${esc(word)}</div>
         <div class="strip-sub">${nf(t.requests)} req · ${nf(t.requestsPerMin, 1)}/min · ${nf(t.servicesReporting)} services</div>
       </div>
       ${cell("Services", svcs.length ? `${healthy}/${svcs.length}` : "—", svcs.length ? "healthy" : "none reporting",
              !svcs.length ? null : healthy === svcs.length ? "--ok" : "--warn")}
       ${cell("Fix now", nf(a.firingNow), `of ${nf(a.total)} open`, a.firingNow ? "--err" : "--ok")}
       ${cell("Availability", hasTraffic ? pct(100 - t.errorRatePct, 2) : "—", hasTraffic ? "requests without a 5xx" : "no traffic yet",
              !hasTraffic ? null : t.errorRatePct > 1 ? "--err" : "--ok")}
       ${cell("P95", t.worstServiceP95Ms == null ? "—" : nf(t.worstServiceP95Ms) + "ms", "worst service",
              (t.worstServiceP95Ms || 0) > 2000 ? "--warn" : null)}
     </div>`;
}

/* ---------- overview ---------- */
async function loadOverview() {
  const [o, f] = await Promise.all([
    api(`/api/v1/overview?window=${state.window}`),
    api(`/api/v1/funnel?window=${state.window}`),
  ]);
  const h = o.health, t = o.technical, b = o.business, c = o.cost;

  const cls = setHealthPill(h);
  renderStatusStrip(o, cls);

  // Series for the sparklines, fetched once and shared with the traffic chart.
  const [pts, incs] = await Promise.all([
    api(`/api/v1/metrics/series?window=${state.window}`),
    api(`/api/v1/incidents?window=${state.window}`).catch(() => null),
  ]);
  const points = complete(pts.points);
  const costSeries = complete(c.series || []);
  renderKeyNumbers({ t, c, o, points, costSeries, incs });

  // Data for the lower sections. Failures, routes and per-service series are
  // independent, so they load together; a failure in one leaves the others.
  const svcNames = (o.services || []).map(s => s.service);
  const [errs, routes, ...svcSeries] = await Promise.all([
    api(`/api/v1/errors?window=${state.window}&limit=50`).catch(() => ({ groups: [] })),
    api(`/api/v1/routes?window=${state.window}`).catch(() => null),
    ...svcNames.map(n => api(`/api/v1/metrics/series?window=${state.window}&service=${encodeURIComponent(n)}`)
                          .then(r => complete(r.points)).catch(() => [])),
  ]);
  renderFailing({ t, points, routes, actions: o.actions });
  renderSeverity(points);
  renderServiceHealth(o.services || [], errs.groups || [], svcSeries);
  renderTopFailures(errs.groups || []);
  renderSlowRoutes(routes);
  const cs = complete(c.series || []);
  upsert("cost", "chCost", "line", {
    labels: cs.map(p => hhmm(p.ts)),
    datasets: [
      ds("CPU", cs.map(p => p.usdPerHour.cpu), css("--accent"), true),
      ds("Memory", cs.map(p => p.usdPerHour.memory), css("--info"), true),
      ds("Requests", cs.map(p => p.usdPerHour.requests), css("--ok"), true),
      ds("Logging", cs.map(p => p.usdPerHour.logging), css("--cost"), true),
    ],
  }, { scales: { y: { stacked: true, grid: { color: css("--border-soft") }, ticks: { color: css("--text-faint"), font: { size: 10 }, callback: v => "$" + Number(v).toFixed(3) } }, x: { stacked: true, grid: { color: css("--border-soft") }, ticks: { color: css("--text-faint"), font: { size: 10 }, autoSkipPadding: 18 } } } });

  renderFunnel(f);

  renderRecSummary(o.recommendations);

  if (o.actions) renderActions(o.actions);

  const badge = $("#incBadge");
  badge.style.display = o.incidents.openCount ? "inline-block" : "none";
  badge.textContent = o.incidents.openCount;
}

function renderActions(q) {
  const box = $("#actionQueue");
  // Heading states the count; the pill says an explanation is one click away.
  // It only claims "AI" when a model is actually configured; otherwise the
  // explanation is the deterministic write-up built from the same evidence.
  const n = q.total;
  $("#actionTitle").textContent = n
    ? `${n} ${n === 1 ? "thing needs" : "things need"} attention`
    : "Nothing needs attention";
  const ai = !!(state.meta && state.meta.config && state.meta.config.aiEnabled);
  const pill = $("#actionCount");
  pill.className = n ? "pill info explain-pill" : "pill ok";
  pill.innerHTML = n
    ? `<span aria-hidden="true">✦</span> ${ai ? "AI explanation · Gemini on Vertex AI" : "Explanation available"}`
    : "all clear";
  pill.title = n
    ? (ai ? `Open an incident and press Explain. Powered by ${state.meta.config.aiModel || "Gemini"} on Vertex AI, checked against the incident's own numbers.`
          : "Open an incident and press Explain. AI is off, so the write-up is built directly from the evidence.")
    : "";
  pill.onclick = n ? () => go("incidents") : null;
  if (!q.actions.length) {
    box.innerHTML = `<div class="empty">Nothing needs attention. Traffic is healthy and no recommendation is outstanding.</div>`;
    return;
  }
  box.innerHTML = q.actions.map((a, i) => {
    const sev = { CRITICAL: "crit", HIGH: "err", MEDIUM: "warn", LOW: "muted" }[a.severity] || "muted";
    const imp = a.impact || {};
    const bits = [];
    if (imp.revenueAtRiskInr) bits.push(`<span class="saving">${inr(imp.revenueAtRiskInr)} at risk</span>`);
    if (imp.cloudCostPerHourUsd) bits.push(`<span class="delta-up">${usd(imp.cloudCostPerHourUsd)}/hr</span>`);
    if (imp.savingPerMonthUsd) bits.push(`<span class="saving">saves ${usd(imp.savingPerMonthUsd)}/mo</span>`);
    const cond = (a.evidence && a.evidence.conditions) || [];
    return `<div class="rec ${a.severity}" data-act="${i}" style="cursor:${a.link ? "pointer" : "default"}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:3px">
        <span class="pill ${a.firing ? "err" : "muted"}"><span class="dot"></span>${a.firing ? "firing" : "standing"}</span>
        <span class="pill ${sev}">${esc(a.severity)}</span>
        <strong>${esc(a.title)}</strong>
        ${a.ageMinutes ? `<span class="faint" style="font-size:11.5px">${nf(a.ageMinutes, 0)}m</span>` : ""}
      </div>
      <div class="why">${esc(a.whyItMatters)}</div>
      <div style="font-size:12.5px;margin-top:5px"><span class="faint">First step —</span> ${esc(a.firstStep)}</div>
      <div style="font-size:12px;margin-top:3px" class="faint">You'll know it worked when: ${esc(a.howYouWillKnow)}</div>
      ${cond.length ? `<div class="faint" style="font-size:11.5px;margin-top:5px">${cond.length} condition(s): ${cond.map(c => esc(c.rule)).join(" · ")}</div>` : ""}
      ${bits.length ? `<div style="font-size:12.5px;margin-top:5px">${bits.join(" &nbsp;·&nbsp; ")}</div>` : ""}
    </div>`;
  }).join("");
  $$("#actionQueue [data-act]").forEach(el => el.addEventListener("click", () => {
    const a = q.actions[+el.dataset.act];
    if (!a.link) return;
    const { view, ...rest } = a.link;
    if (view === "incidents" && a.link.incident) showIncident(a.link.incident);
    else go(view, rest);
  }));
}

/* ---------- tile icons ----------
   One stroke icon set (24px grid, 2px stroke) in a tinted square, as the
   header of every Overview tile and card. Tint comes from the existing
   colour tokens, so no new colours are introduced. */
const ICONS = {
  heart: '<path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4 3 5.5l7 7Z"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  cart: '<circle cx="8" cy="21" r="1"/><circle cx="19" cy="21" r="1"/><path d="M2 2h3l2.7 12.4a2 2 0 0 0 2 1.6h9.7a2 2 0 0 0 2-1.6L23 6H6"/>',
  coins: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  bars: '<path d="M3 3v18h18"/><path d="M8 17v-4"/><path d="M13 17V8"/><path d="M18 17v-7"/>',
  nodes: '<circle cx="12" cy="5" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><path d="m10.5 7.6-4 8.8M13.5 7.6l4 8.8M8 19h8"/>',
  funnel: '<path d="M3 4h18l-7 8.5V19l-4 2v-8.5Z"/>',
  alert: '<path d="m10.3 3.9-8.2 14A2 2 0 0 0 3.8 21h16.4a2 2 0 0 0 1.7-3.1l-8.2-14a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  bolt: '<path d="M13 2 3 14h9l-1 8 10-12h-9Z"/>',
  tag: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8Z"/><circle cx="7.5" cy="7.5" r="1.5"/>'
};
const TINT = { ok: "--ok", warn: "--warn", err: "--err", crit: "--crit", cost: "--cost", accent: "--accent", info: "--info", money: "--money" };
function ico(name, tint) {
  return `<span class="ico" style="--ico:var(${TINT[tint] || "--accent"})" aria-hidden="true">` +
    `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg></span>`;
}
const NAV_ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  alert: '<path d="m10.3 3.9-8.2 14A2 2 0 0 0 3.8 21h16.4a2 2 0 0 0 1.7-3.1l-8.2-14a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8Z"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
  scroll: '<path d="M8 21h11a2 2 0 0 0 2-2v-1H10v1a2 2 0 1 1-4 0V5a2 2 0 0 0-2-2 2 2 0 0 0-2 2v2h4"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M10 8h6M10 12h6"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  banknote: '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/><path d="M6 12h.01M18 12h.01"/>',
  bell: '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Z"/><path d="M10.3 21a2 2 0 0 0 3.4 0"/>',
  settings: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>'
};
function decorateNav() {
  $$("#nav button[data-nicon]:not([data-iconized])").forEach(b => {
    b.insertAdjacentHTML("afterbegin",
      `<svg class="nico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${NAV_ICONS[b.dataset.nicon] || ""}</svg>`);
    b.dataset.iconized = "1";
  });
}

// Static card headings opt in with data-icon / data-tint in index.html.
function decorateHeadings(root = document) {
  root.querySelectorAll("h3[data-icon]:not([data-iconized])").forEach(h => {
    h.insertAdjacentHTML("afterbegin", ico(h.dataset.icon, h.dataset.tint));
    h.classList.add("with-ico");
    h.dataset.iconized = "1";
  });
}

function tile(label, value, foot, cls, icon, tint) {
  const color = cls ? ({ ok: "--ok", warn: "--warn", err: "--err", crit: "--crit", cost: "--cost" }[cls]) : null;
  return `<div class="card tile">
    <div class="tile-head">${icon ? ico(icon, tint || cls) : ""}<div class="label">${esc(label)}</div></div>
    <div class="value"${color ? ` style="color:var(${color})"` : ""}>${value}</div>
    <div class="foot">${foot}</div></div>`;
}
function ds(label, data, color, fill) {
  return { label, data, borderColor: color, backgroundColor: fill ? color + "33" : color,
           fill: !!fill, tension: .3, borderWidth: 2, pointRadius: 0, pointHoverRadius: 3 };
}
function drawTraffic(points) {
  const labels = points.map(p => hhmm(p.ts));
  upsert("traffic", "chTraffic", "bar", {
    labels,
    datasets: [
      { label: "2xx/3xx", data: points.map(p => Math.max(0, p.requests - p.errors5xx - p.errors4xx)), backgroundColor: css("--ok") + "cc", stack: "s", borderRadius: 2 },
      { label: "4xx client", data: points.map(p => p.errors4xx), backgroundColor: css("--warn") + "cc", stack: "s", borderRadius: 2 },
      { label: "5xx server", data: points.map(p => p.errors5xx), backgroundColor: css("--err") + "dd", stack: "s", borderRadius: 2 },
      { label: "p95 latency (ms)", data: points.map(p => p.p95LatencyMs), type: "line", yAxisID: "y1",
        borderColor: css("--accent"), borderWidth: 2, pointRadius: 0, tension: .3, fill: false },
    ],
  }, { scales: {
      x: { stacked: true, grid: { color: css("--border-soft") }, ticks: { color: css("--text-faint"), font: { size: 10 }, autoSkipPadding: 18 } },
      y: { stacked: true, grid: { color: css("--border-soft") }, ticks: { color: css("--text-faint"), font: { size: 10 } }, beginAtZero: true },
      y1: { position: "right", grid: { display: false }, ticks: { color: css("--accent"), font: { size: 10 } }, beginAtZero: true },
  } });
}
function renderServiceTable(svcs) {
  if (!svcs.length) { $("#svcTable").innerHTML = `<tbody><tr><td class="empty">No services reporting yet.</td></tr></tbody>`; return; }
  $("#svcTable").innerHTML =
    `<thead><tr><th>Service</th><th class="right">req/min</th><th class="right">5xx</th><th class="right">p95</th><th class="right">CPU</th><th class="right">Mem</th><th></th></tr></thead><tbody>` +
    svcs.map(s => {
      const bad = s.errorRate > 0.05, warn = s.errorRate > 0.01 || (s.p95LatencyMs || 0) > 2000;
      return `<tr class="clickable" data-svc="${esc(s.service)}">
        <td><strong>${esc(s.service.replace("cognikart-", ""))}</strong><div class="faint" style="font-size:11px">${esc(s.service)}</div></td>
        <td class="num">${nf(s.requestsPerMin, 1)}</td>
        <td class="num" style="color:${s.errors5xx ? "var(--err)" : "inherit"}">${nf(s.errors5xx)}</td>
        <td class="num">${s.p95LatencyMs === null ? "—" : nf(s.p95LatencyMs) + "ms"}</td>
        <td class="num">${s.cpuPctMax === null ? "—" : pct(s.cpuPctMax)}</td>
        <td class="num">${s.memoryUtilisationPct === null ? "—" : pct(s.memoryUtilisationPct)}</td>
        <td><span class="pill ${bad ? "err" : warn ? "warn" : "ok"}"><span class="dot"></span>${bad ? "failing" : warn ? "degraded" : "ok"}</span></td>
      </tr>`;
    }).join("") + `</tbody>`;
  $$("#svcTable [data-svc]").forEach(tr => tr.addEventListener("click", () =>
    go("logs", { service: tr.dataset.svc, severity: "" })));
}
function renderFunnel(f) {
  const top = f.stages[0].count || 1;
  $("#funnelBox").innerHTML = f.stages.map(s => `
    <div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:3px">
        <span>${esc(s.stage)}</span><span class="num">${nf(s.count)} <span class="faint">(${pct(s.pctOfTop)})</span></span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, 100 * s.count / top)}%"></div></div>
    </div>`).join("") +
    (f.failedCheckouts ? `<div class="faint" style="font-size:12px;margin-top:8px">${nf(f.failedCheckouts)} checkouts failed in this window.</div>` : "");
}
function incRow(i) {
  const sev = { CRITICAL: "crit", HIGH: "err", MEDIUM: "warn", LOW: "muted" }[i.severity] || "muted";
  const cost = i.impact?.cost?.deltaUsdPerHour, rev = i.impact?.business?.revenueAtRiskInr;
  return `<div class="rec ${i.severity}" data-inc="${esc(i.id)}" style="cursor:pointer">
    <div class="title">${esc(i.title)}</div>
    <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:4px 0">
      <span class="pill ${sev}"><span class="dot"></span>${esc(i.severity)}</span>
      <span class="pill ${i.status === "OPEN" ? "err" : "ok"}">${esc(i.status)}</span>
      <span class="faint" style="font-size:12px">${dur(i.durationS)} · ${i.triggerCount} condition(s)</span>
    </div>
    <div style="font-size:12.5px">
      ${cost != null ? `<span class="muted">cloud</span> <span class="${cost > 0 ? "delta-up" : "delta-down"}">${cost > 0 ? "+" : ""}${usd(cost)}/hr</span>` : ""}
      ${rev ? ` &nbsp;·&nbsp; <span class="muted">revenue at risk</span> <span class="saving">${inr(rev)}</span>` : ""}
    </div></div>`;
}
/* Two lines: how many actions and what they save; then the biggest one. */
function renderRecSummary(rec) {
  const box = $("#ovRecs");
  if (!rec || !rec.total) { box.innerHTML = `<span class="faint">No cost-saving actions in this window.</span>`; return; }
  const top = rec.top || [];
  const saving = top.reduce((s, r) => s + (r.estimatedSavingUsdPerMonth || 0), 0);
  const first = top[0];
  box.innerHTML =
    `<div class="recs-line"><strong>${nf(rec.total)} action${rec.total === 1 ? "" : "s"}</strong>` +
    (saving > 0 ? ` could save about <strong class="saving">${usd(saving, 2)}/month</strong>.` : " open, savings not yet estimable.") +
    `</div><div class="recs-line faint">Biggest: ${esc(first ? first.title : "—")}. <span class="recs-go">See all in Cost →</span></div>`;
}

function recCard(r) {
  const sav = r.estimatedSavingUsdPerMonth !== null
    ? `<span class="saving">${usd(r.estimatedSavingUsdPerMonth, 4)}/mo</span>`
    : `<span class="saving none">${esc(r.savingStatus)}</span>`;
  return `<div class="rec ${r.severity}">
    <div class="title">${esc(r.title)}</div>
    <div class="why">${esc(r.recommendation)}</div>
    <div style="font-size:12px" class="faint">
      ${esc(r.observedMetric)} = <strong>${esc(r.observedValue)}${esc(r.unit)}</strong> over ${esc(r.timeWindow)} ·
      confidence ${esc(r.confidence)} · ${sav}
    </div></div>`;
}

/* ---------- logs (SSE) ---------- */
function sevOrder(s) { return { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 }[s] ?? 1; }
function logRow(e) {
  return `<tr data-log='${esc(JSON.stringify(e))}' class="clickable">
    <td class="faint">${hms(e.ts)}</td>
    <td><span class="sev ${esc(e.severity)}">${esc(e.severity)}</span></td>
    <td>${esc((e.service || "").replace("cognikart-", ""))}</td>
    <td class="faint">${esc(e.event || "")}</td>
    <td><span class="msg">${esc(e.message || "")}</span></td>
    <td class="num" style="color:${(e.httpStatus || 0) >= 500 ? "var(--err)" : (e.httpStatus || 0) >= 400 ? "var(--warn)" : "inherit"}">${e.httpStatus ?? ""}</td>
    <td class="num">${e.latencyMs != null ? nf(e.latencyMs) + "ms" : ""}</td>
  </tr>`;
}
function renderLogs() {
  const body = $("#logBody");
  body.innerHTML = state.logs.slice(0, 400).map(logRow).join("");
  $("#logCount").textContent = `${state.logs.length} buffered${state.paused ? " · paused" : " · live"}`;
  $$("#logBody tr").forEach(tr => tr.addEventListener("click", () => showLogDetail(JSON.parse(tr.dataset.log))));
}
function startStream() {
  if (state.es) state.es.close();
  const sev = $("#logSev").value, svc = $("#logSvc").value, q = $("#logQ").value.trim();
  const p = new URLSearchParams();
  if (sev) p.set("severity", sev);
  if (svc) p.set("service", svc);
  if (q) p.set("q", q);
  const es = new EventSource("/api/v1/logs/stream?" + p.toString());
  state.es = es;
  es.addEventListener("logs", ev => {
    if (state.paused) return;
    const batch = JSON.parse(ev.data);
    state.logs = batch.reverse().concat(state.logs).slice(0, state.maxLogs);
    if (state.view === "logs") renderLogs();
  });
  es.onerror = () => { /* EventSource reconnects on its own */ };
}
["logSev", "logSvc"].forEach(id => $("#" + id).addEventListener("change", () => { state.logs = []; startStream(); loadLogsInitial(); }));
let qTimer; $("#logQ").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.logs = []; startStream(); loadLogsInitial(); }, 350); });
$("#logPause").addEventListener("click", () => {
  state.paused = !state.paused;
  $("#logPause").textContent = state.paused ? "▶ Resume" : "⏸ Pause";
  renderLogs();
});
$("#logClear").addEventListener("click", () => { state.logs = []; renderLogs(); });

async function loadLogsInitial() {
  const sev = $("#logSev").value, svc = $("#logSvc").value, q = $("#logQ").value.trim();
  const p = new URLSearchParams({ limit: "300" });
  if (sev) p.set("severity", sev);
  if (svc) p.set("service", svc);
  if (q) p.set("q", q);
  const r = await api("/api/v1/logs?" + p.toString());
  state.logs = r.entries; renderLogs();
  const pts = await api(`/api/v1/metrics/series?window=${state.window}`);
  const lv = complete(pts.points);
  upsert("logvol", "chLogVol", "line", {
    labels: lv.map(p => hhmm(p.ts)),
    datasets: [ds("MiB/min", lv.map(p => p.logBytes / 1048576), css("--cost"), true)],
  });
}
async function showLogDetail(e) {
  let traceHtml = `<div class="faint">No trace id on this entry.</div>`;
  if (e.trace) {
    const tid = e.trace.split("/").pop();
    try {
      const t = await api("/api/v1/trace/" + tid);
      traceHtml = renderWaterfall(t);
    } catch { traceHtml = `<div class="faint">Trace ${esc(tid)} is no longer in the buffer.</div>`; }
  }
  openDrawer(esc(e.event || "log entry"),
    `${esc(e.service)} · ${hms(e.ts)} · <span class="sev ${esc(e.severity)}">${esc(e.severity)}</span>`,
    `<h3>Message</h3><div>${esc(e.message)}</div>
     <h3>Trace across services</h3>${traceHtml}
     <h3>Full entry</h3><pre class="json">${esc(JSON.stringify(e, null, 1))}</pre>`);
}
function renderWaterfall(t) {
  const t0 = t.entries[0].ts, span = Math.max(t.spanSeconds, 0.001);
  return `<div class="faint" style="font-size:12px;margin-bottom:8px">
      ${t.entryCount} entries · ${t.services.length} services · ${nf(t.spanSeconds * 1000)}ms end to end</div>` +
    t.entries.map(e => {
      const off = ((e.ts - t0) / span) * 100;
      const w = Math.max(1.5, ((e.latencyMs || 10) / 1000 / span) * 100);
      const k = (e.httpStatus || 0) >= 500 ? "err" : e.severity === "WARNING" ? "warn" : "";
      return `<div class="wf-row">
        <div class="faint nowrap" style="overflow:hidden;text-overflow:ellipsis">${esc((e.service || "").replace("cognikart-", ""))}</div>
        <div class="wf-bar" title="${esc(e.event)}"><span class="${k}" style="left:${Math.min(off, 97)}%;width:${Math.min(w, 100 - off)}%"></span></div>
        <div class="num faint">${e.latencyMs != null ? nf(e.latencyMs) + "ms" : ""}</div>
      </div><div class="faint" style="font-size:11px;margin:-2px 0 6px 149px">${esc(e.event)}${e.errorCode ? " · " + esc(e.errorCode) : ""}</div>`;
    }).join("");
}

/* ---------- errors ---------- */
async function loadErrors() {
  const [e, pts] = await Promise.all([
    api(`/api/v1/errors?window=${state.window}`),
    api(`/api/v1/metrics/series?window=${state.window}`),
  ]);
  const t = e.totals;
  $("#errTiles").innerHTML = [
    tile("Server errors (5xx)", nf(t.errors5xx), "genuine service faults", t.errors5xx ? "err" : "ok"),
    tile("Client errors (4xx)", nf(t.errors4xx), "bad requests — not service faults", "warn"),
    tile("Error groups", nf(e.groups.length), esc(e.groupingKey)),
  ].join("");
  const ep = complete(pts.points);
  upsert("errors", "chErrors", "line", {
    labels: ep.map(p => hhmm(p.ts)),
    datasets: [ds("5xx", ep.map(p => p.errors5xx), css("--err"), true),
               ds("4xx", ep.map(p => p.errors4xx), css("--warn"), true)],
  });
  upsert("errsplit", "chErrSplit", "doughnut", {
    labels: ["5xx server", "4xx client", "successful"],
    datasets: [{ data: [t.errors5xx, t.errors4xx, Math.max(0, t.requests - t.errors5xx - t.errors4xx)],
                 backgroundColor: [css("--err"), css("--warn"), css("--ok")], borderWidth: 0 }],
  }, { scales: null, cutout: "62%" });

  $("#errTable").innerHTML = e.groups.length === 0
    ? `<tbody><tr><td class="empty">No errors in this window.</td></tr></tbody>`
    : `<thead><tr><th>Error code</th><th>Service</th><th>Route</th><th class="right">Count</th><th class="right">Revenue at risk</th><th>Last seen</th></tr></thead><tbody>` +
      e.groups.map((g, i) => `<tr class="clickable" data-g="${i}">
        <td><strong style="color:var(--err)">${esc(g.errorCode)}</strong><div class="faint" style="font-size:11px">${esc(g.errorClass)}</div></td>
        <td>${esc(g.service.replace("cognikart-", ""))}</td>
        <td class="mono faint">${esc(g.route || "—")}</td>
        <td class="num">${nf(g.count)}</td>
        <td class="num">${g.revenueAtRiskInr ? inr(g.revenueAtRiskInr) : "—"}</td>
        <td class="faint">${hms(g.lastSeen)}</td></tr>`).join("") + `</tbody>`;
  $$("#errTable [data-g]").forEach(tr => {
    tr.addEventListener("click", ev => {
      const g = e.groups[+tr.dataset.g];
      // Shift-click goes straight to the filtered logs; a plain click opens
      // the group, which is the more common intent.
      if (ev.shiftKey) go("logs", { q: g.errorCode, service: g.service, severity: "WARNING" });
      else showErrorGroup(g);
    });
  });
}
async function showErrorGroup(g) {
  let trace = "";
  if (g.sampleTraces && g.sampleTraces.length) {
    try { trace = renderWaterfall(await api("/api/v1/trace/" + g.sampleTraces[0].split("/").pop())); }
    catch { trace = `<div class="faint">Sample trace no longer buffered.</div>`; }
  }
  openDrawer(esc(g.errorCode), `${esc(g.service)} · ${nf(g.count)} occurrences`,
    `<h3>Normalised pattern</h3><div class="mono">${esc(g.pattern)}</div>
     <h3>Grouping key</h3><div class="faint" style="font-size:12.5px">service + errorCode + errorClass + route + normalised message — deterministic, no ML.</div>
     <h3>Window</h3><dl class="kv">
       <dt>First seen</dt><dd>${hms(g.firstSeen)}</dd>
       <dt>Last seen</dt><dd>${hms(g.lastSeen)}</dd>
       <dt>Occurrences</dt><dd>${nf(g.count)}</dd>
       <dt>Revenue at risk</dt><dd>${g.revenueAtRiskInr ? inr(g.revenueAtRiskInr) : "—"}</dd></dl>
     <h3>Sample trace</h3>${trace || '<div class="faint">none</div>'}
     <h3>Sample entries</h3><pre class="json">${esc(JSON.stringify(g.samples.slice(0, 3), null, 1))}</pre>`);
}

/* ---------- resources ---------- */
async function loadResources() {
  const [s, pts, plat] = await Promise.all([
    api(`/api/v1/services?window=${state.window}`),
    api(`/api/v1/metrics/series?window=${state.window}`),
    api("/api/v1/metrics/platform"),
  ]);
  $("#resCards").innerHTML = s.services.map(x => {
    const mem = x.memoryUtilisationPct || 0, cpu = x.cpuPctMax || 0;
    return `<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong>${esc(x.service.replace("cognikart-", ""))}</strong>
        <span class="pill ${x.healthy ? "ok" : "err"}"><span class="dot"></span>${x.healthy ? "ok" : "failing"}</span>
      </div>
      <div class="faint" style="font-size:11px;margin:5px 0 9px">${nf(x.requestsPerMin, 1)} req/min · ${x.instanceCount} instance(s)</div>
      <div style="font-size:11.5px;display:flex;justify-content:space-between"><span class="faint">CPU</span><span class="num">${pct(cpu)}</span></div>
      <div class="bar-track" style="margin-bottom:9px"><div class="bar-fill ${cpu > 85 ? "err" : cpu > 60 ? "warn" : "ok"}" style="width:${Math.min(100, cpu)}%"></div></div>
      <div style="font-size:11.5px;display:flex;justify-content:space-between"><span class="faint">Memory</span><span class="num">${nf(x.rssMbMax)} / ${nf(x.memoryGibProvisioned * 1024)} MiB</span></div>
      <div class="bar-track"><div class="bar-fill ${mem > 80 ? "err" : mem > 50 ? "warn" : "ok"}" style="width:${Math.min(100, mem)}%"></div></div>
      <div class="faint" style="font-size:11px;margin-top:5px">${pct(mem)} of provisioned${mem < 40 ? ' · <span style="color:var(--money)">over-provisioned</span>' : ""}</div>
    </div>`;
  }).join("") || `<div class="empty">No services reporting.</div>`;

  const svcNames = [...new Set(s.services.map(x => x.service))];
  const palette = [css("--accent"), css("--ok"), css("--warn"), css("--err"), css("--info")];
  const series = await Promise.all(svcNames.map(n => api(`/api/v1/metrics/series?window=${state.window}&service=${encodeURIComponent(n)}`)));
  const base = complete(pts.points);
  const labels = base.map(p => hhmm(p.ts));
  upsert("cpu", "chCpu", "line", {
    labels,
    datasets: series.map((r, i) => ds(svcNames[i].replace("cognikart-", ""),
      alignTo(base, r.points, "cpuPctMax"), palette[i % palette.length], false)),
  });
  upsert("mem", "chMem", "line", {
    labels,
    datasets: series.map((r, i) => ds(svcNames[i].replace("cognikart-", ""),
      alignTo(base, r.points, "rssMbMax"), palette[i % palette.length], false)),
  });

  $("#platformMetrics").innerHTML = !plat.available
    ? `<div class="empty">${esc(plat.reason || "not available")}</div>`
    : Object.entries(plat.metrics).map(([k, m]) => `
        <div style="margin-bottom:12px">
          <div style="font-size:12.5px"><strong>${esc(m.label)}</strong> <span class="faint mono">${esc(m.metricType)}</span></div>
          ${m.series.map(sr => `<div style="font-size:12px;display:flex;justify-content:space-between">
             <span class="faint">${esc(sr.service)}</span><span class="num">${nf(sr.latest, 3)} ${esc(m.unit)}</span></div>`).join("") || '<div class="faint" style="font-size:12px">no data yet</div>'}
        </div>`).join("") +
      `<div class="faint" style="font-size:11.5px;margin-top:10px">${esc(plat.note)}</div>`;
}
function alignTo(base, rows, key) {
  const by = new Map(rows.map(r => [r.minute, r[key]]));
  return base.map(b => by.has(b.minute) ? by.get(b.minute) : null);
}

/* ---------- projects ---------- */
async function loadProjects() {
  const d = await api("/api/v1/projects");
  $("#projLede").textContent = d.note || "";

  $("#projError").innerHTML = d.error
    ? `<div class="notice bad" style="margin-bottom:14px">
         <strong>Cannot list projects.</strong>
         <div style="margin-top:5px">${esc(d.error)}</div>
         ${d.howToFix ? `<div class="mono" style="margin-top:9px;font-size:11.5px">${esc(d.howToFix)}</div>` : ""}
       </div>` : "";

  if (!d.projects.length) {
    $("#projGrid").innerHTML = `<div class="empty">No projects visible to this service account.</div>`;
    return;
  }

  $("#projGrid").innerHTML = d.projects.map(p => {
    const state = p.connected === true ? "ok" : p.connected === false ? "err" : "muted";
    const label = p.connected === true ? "connected"
                : p.connected === false ? "no log access" : "not checked";
    return `<div class="card" style="${p.active ? "border-color:var(--accent)" : ""}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <strong style="font-size:15px">${esc(p.displayName)}</strong>
        ${p.active ? `<span class="pill info"><span class="dot"></span>viewing</span>` : ""}
      </div>
      <div class="mono faint" style="font-size:11.5px;margin-top:3px">${esc(p.projectId)}</div>
      <div style="margin-top:11px"><span class="pill ${state}"><span class="dot"></span>${label}</span></div>
      <div class="faint" style="font-size:12px;margin-top:8px;min-height:2.6em">${esc(p.reason || p.note || "Access not yet checked.")}</div>
      <div style="display:flex;gap:8px;margin-top:12px">
        <button class="btn" data-check="${esc(p.projectId)}">Check access</button>
        <button class="btn primary" data-open="${esc(p.projectId)}" ${p.active ? "disabled" : ""}>
          ${p.active ? "Open" : "Open"}</button>
      </div>
      <div class="fixhint faint mono" style="font-size:11px;margin-top:9px"></div>
    </div>`;
  }).join("");

  $$("#projGrid [data-check]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "Checking…";
    const r = await api(`/api/v1/projects/${encodeURIComponent(b.dataset.check)}/access?force=true`);
    const card = b.closest(".card");
    card.querySelector(".pill").className = "pill " + (r.connected ? "ok" : "err");
    card.querySelector(".pill").innerHTML =
      `<span class="dot"></span>${r.connected ? "connected" : "no log access"}`;
    card.querySelectorAll(".faint")[1].textContent = r.reason || "";
    if (r.howToFix) card.querySelector(".fixhint").textContent = r.howToFix;
    b.disabled = false; b.textContent = "Check access";
  }));

  $$("#projGrid [data-open]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; b.innerHTML = '<span class="spin"></span>';
    try {
      // Switching clears the working set on purpose: the buffered entries
      // belong to the project being left, and showing them under another
      // project's name would be a lie that is very hard to spot.
      await api("/api/v1/projects/select", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ projectId: b.dataset.open }),
      });
      state.logs = [];
      toast(`Now viewing ${b.dataset.open}`);
      go("overview");
    } catch (e) {
      toast("Could not switch: " + e.message);
      b.disabled = false; b.textContent = "Open";
    }
  }));
}

/* ---------- insights: patterns and anomalies ---------- */
async function loadInsights() {
  const [an, pat] = await Promise.all([
    api(`/api/v1/anomalies?window=${Math.max(state.window, 20)}`),
    api(`/api/v1/patterns?window=${state.window}&limit=25`),
  ]);

  $("#anomalyList").innerHTML = an.anomalies.length === 0
    ? `<div class="empty">${esc(an.note || "No series is departing from its baseline.")}</div>`
    : an.anomalies.map(a => `
      <div class="rec ${a.severity}">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <strong>${esc(a.label)}</strong>
          <span class="pill ${a.direction === "up" ? "err" : "info"}">
            ${a.changePct === null ? "" : (a.changePct > 0 ? "+" : "") + nf(a.changePct, 1) + "%"}</span>
          <span class="pill muted">z = ${nf(a.zScore, 1)}</span>
        </div>
        <div class="why">${esc(a.whyItMatters)}</div>
        <div class="faint" style="font-size:12px;margin-top:4px">${esc(a.evidence)}</div>
      </div>`).join("")
      + `<div class="faint" style="font-size:11.5px;margin-top:8px">${esc(an.method || "")}</div>`;

  $("#patternHint").textContent =
    `${nf(pat.total)} log lines in this window collapsed into ${nf(pat.distinctPatterns)} distinct patterns. Click one to see its logs.`;
  $("#patternTable").innerHTML = pat.patterns.length === 0
    ? `<tbody><tr><td class="empty">No log lines in this window.</td></tr></tbody>`
    : `<thead><tr><th>Pattern</th><th>Services</th><th class="right">Count</th><th class="right">Share</th><th class="right">p95</th><th class="right">At risk</th></tr></thead><tbody>`
      + pat.patterns.map((p, i) => `<tr class="clickable" data-p="${i}">
          <td><span class="sev ${esc(p.severity)}">${esc(p.severity)}</span>
              <span class="mono" style="margin-left:6px">${esc(p.pattern)}</span></td>
          <td class="faint">${esc((p.services || []).map(x => x.replace("cognikart-", "")).join(", "))}</td>
          <td class="num">${nf(p.count)}</td>
          <td class="num">${nf(p.sharePct, 1)}%</td>
          <td class="num">${p.p95LatencyMs === null ? "—" : nf(p.p95LatencyMs) + "ms"}</td>
          <td class="num">${p.revenueAtRiskInr ? inr(p.revenueAtRiskInr) : "—"}</td>
        </tr>`).join("") + `</tbody>`;

  $$("#patternTable [data-p]").forEach(tr => tr.addEventListener("click", () => {
    const p = pat.patterns[+tr.dataset.p];
    // The template has masking tokens in it, so search on the most specific
    // literal we have instead: an error code, else the event name.
    const needle = (p.errorCodes && p.errorCodes[0]) || p.topEvent || "";
    go("logs", { q: needle, service: (p.services || [])[0] || "", severity: "" });
  }));
}

/* ---------- services ---------- */
async function loadServices() {
  const s = await api(`/api/v1/services?window=${state.window}`);
  if (!s.services.length) {
    $("#serviceCards").innerHTML = `<div class="empty">No services reporting yet.</div>`;
    return;
  }
  $("#serviceCards").innerHTML = s.services.map(x => {
    const bad = x.errorRate5xx > 0.05, warn = x.errorRate5xx > 0.01 || (x.p95LatencyMs || 0) > 2000;
    const mem = x.memoryUtilisationPct || 0;
    return `<div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
        <div>
          <strong style="font-size:15px">${esc(x.service.replace("cognikart-", ""))}</strong>
          <div class="faint" style="font-size:11px">${esc(x.service)}</div>
        </div>
        <span class="pill ${bad ? "err" : warn ? "warn" : "ok"}"><span class="dot"></span>${bad ? "failing" : warn ? "degraded" : "healthy"}</span>
      </div>
      <dl class="kv" style="margin-top:12px">
        <dt>Requests</dt><dd>${nf(x.requests)} <span class="faint">(${nf(x.requestsPerMin, 1)}/min)</span></dd>
        <dt>Server errors</dt><dd style="color:${x.errors5xx ? "var(--err)" : "inherit"}">${nf(x.errors5xx)} <span class="faint">(${pct(x.errorRate5xx * 100, 2)})</span></dd>
        <dt>Client errors</dt><dd>${nf(x.errors4xx)}</dd>
        <dt>Latency p50 / p95</dt><dd>${x.p50LatencyMs === null ? "—" : nf(x.p50LatencyMs) + "ms"} / ${x.p95LatencyMs === null ? "—" : nf(x.p95LatencyMs) + "ms"}</dd>
        <dt>CPU peak</dt><dd>${x.cpuPctMax === null ? "—" : pct(x.cpuPctMax)}</dd>
        <dt>Memory</dt><dd>${nf(x.rssMbMax)} / ${nf(x.memoryGibProvisioned * 1024)} MiB <span class="faint">(${pct(mem)})</span></dd>
        <dt>Instances</dt><dd>${nf(x.instanceCount)}</dd>
        <dt>Log volume</dt><dd>${nf(x.logBytes / 1048576, 2)} MiB</dd>
      </dl>
      ${mem && mem < 40 ? `<div class="faint" style="font-size:11.5px;margin-top:8px;color:var(--money)">Over-provisioned: using ${pct(mem)} of what it reserves.</div>` : ""}
      <button class="btn" style="margin-top:11px;width:100%" data-svclogs="${esc(x.service)}">Open its logs</button>
    </div>`;
  }).join("");
  $$("#serviceCards [data-svclogs]").forEach(b => b.addEventListener("click", () =>
    go("logs", { service: b.dataset.svclogs, severity: "" })));
}

/* ---------- setup ---------- */
async function loadSetup() {
  const m = state.meta || await api("/api/v1/meta");
  const c = m.config;
  $("#setupTop").innerHTML = `
    <div class="card">
      <h3>Source</h3>
      <div class="hint">Where this dashboard's data comes from.</div>
      <dl class="kv">
        <dt>Mode</dt><dd><span class="pill ${c.dataSource === "gcp" ? "ok" : "info"}"><span class="dot"></span>${esc(c.dataSource)}</span></dd>
        <dt>Project</dt><dd>${esc(c.projectId || "— (local mode)")}</dd>
        <dt>Region</dt><dd>${esc(c.region)}</dd>
        <dt>Watching</dt><dd>${(c.watchedServices || []).map(x => esc(x)).join("<br>")}</dd>
        <dt>Billing model</dt><dd>${esc(c.billingModel)}</dd>
        <dt>Pricing verified</dt><dd>${esc(c.pricingVerifiedOn || "—")}</dd>
        <dt>AI explanation</dt><dd>${c.aiEnabled ? esc(c.aiModel) : "disabled (deterministic narrative)"}</dd>
      </dl>
    </div>
    <div class="card">
      <h3>Working set</h3>
      <div class="hint">OpsMind keeps a bounded in-memory view. Cloud Logging is the durable store; this is not a copy of it.</div>
      <dl class="kv">
        <dt>Buffered entries</dt><dd>${nf(m.store.bufferedEntries)} / ${nf(m.store.bufferCapacity)}</dd>
        <dt>Minute buckets</dt><dd>${nf(m.store.minuteBuckets)}</dd>
        <dt>Error groups</dt><dd>${nf(m.store.errorGroups)}</dd>
        <dt>Ingested total</dt><dd>${nf(m.store.ingestedTotal)}</dd>
        <dt>Duplicates dropped</dt><dd>${nf(m.store.droppedDuplicates)}</dd>
        <dt>Last ingest</dt><dd>${m.store.lastIngestAgeS === null ? "—" : nf(m.store.lastIngestAgeS, 1) + "s ago"}</dd>
        <dt>Uptime</dt><dd>${dur(m.uptimeS)}</dd>
      </dl>
      ${(m.ruleWarnings || []).length
        ? `<div class="notice" style="margin-top:10px;color:var(--err)">${m.ruleWarnings.length} alert rule(s) reference a metric the store does not produce and can never fire.</div>`
        : `<div class="faint" style="font-size:11.5px;margin-top:10px">Every alert rule resolves to a real metric.</div>`}
    </div>`;

  $("#tierList").innerHTML = m.tiers.map(t => `
    <div style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:9px">
        <span class="tier ${t.tier === "LIVE" ? "live" : t.tier === "NEAR_REAL_TIME" ? "near" : "auth"}">${esc(t.tier.replace(/_/g, " "))}</span>
        <strong style="font-size:13px">${esc(t.latency)}</strong>
        <span class="faint" style="font-size:12px">${esc(t.source)}</span>
      </div>
      <div class="faint" style="font-size:12px;margin-top:3px">${esc(t.carries)}</div>
    </div>`).join("");

  $("#collectorList").innerHTML = Object.entries(m.collectors).map(([k, v]) => `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border-soft);font-size:12.5px">
      <span><strong>${esc(k)}</strong> <span class="faint">${esc(v.collector || "")}</span></span>
      <span style="display:flex;gap:8px;align-items:center">
        ${v.lastError ? `<span class="pill err" title="${esc(v.lastError)}">error</span>` : `<span class="pill ${v.running ? "ok" : "muted"}"><span class="dot"></span>${v.running ? "running" : "idle"}</span>`}
        ${v.lastPollAgeS !== undefined && v.lastPollAgeS !== null ? `<span class="faint">${nf(v.lastPollAgeS, 0)}s ago</span>` : ""}
      </span>
    </div>`).join("");
}

/* ---------- cost ---------- */
async function loadCost() {
  const [c, ft, billed, recs] = await Promise.all([
    api(`/api/v1/cost?window=${state.window}`),
    api(`/api/v1/cost/freetier?window=${state.window}`),
    api("/api/v1/cost/billed"),
    api(`/api/v1/recommendations?window=${Math.max(state.window, 15)}`),
  ]);
  $("#costTiles").innerHTML = [
    tile("Modeled rate", usd(c.usdPerHour, 4) + "<span class='faint' style='font-size:14px'>/hr</span>", "measured usage × list price", "cost"),
    tile("Projected / day", usd(c.projectedUsdPerDay, 3), "if this rate continued 24h"),
    tile("Projected / month", usd(c.projectedUsdPerMonth, 2), "if this rate continued 24/7"),
    tile("Calculated savings", usd(recs.totalCalculatedSavingUsdPerMonth, 4) + "<span class='faint' style='font-size:14px'>/mo</span>",
         `${recs.count} recommendation(s)`, "ok"),
  ].join("");

  upsert("costdrv", "chCostDriver", "doughnut", {
    labels: ["CPU", "Memory", "Requests", "Logging"],
    datasets: [{ data: [c.byDriver.cpu, c.byDriver.memory, c.byDriver.requests, c.byDriver.logging],
                 backgroundColor: [css("--accent"), css("--info"), css("--ok"), css("--cost")], borderWidth: 0 }],
  }, { scales: null, cutout: "60%" });

  upsert("costsvc", "chCostSvc", "bar", {
    labels: c.byService.map(s => s.service.replace("cognikart-", "")),
    datasets: [{ label: "USD/hour", data: c.byService.map(s => s.usdPerHour.total),
                 backgroundColor: css("--cost") + "cc", borderRadius: 3 }],
  }, { indexAxis: "y", plugins: { legend: { display: false } } });

  $("#ftHint").innerHTML = esc(ft.note);
  $("#freeTier").innerHTML = ft.lines.map(l => {
    const p = Math.min(100, l.pctOfFreeTier ?? 0);
    const k = (l.pctOfFreeTier ?? 0) > 100 ? "err" : (l.pctOfFreeTier ?? 0) > 60 ? "warn" : "ok";
    return `<div style="margin-bottom:11px">
      <div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:3px">
        <span>${esc(l.resource)}</span>
        <span class="num ${k === "err" ? "delta-up" : ""}">${pct(l.pctOfFreeTier, 2)}</span></div>
      <div class="bar-track"><div class="bar-fill ${k}" style="width:${p}%"></div></div>
      <div class="faint" style="font-size:11px;margin-top:2px">${nf(l.projectedMonthly, 1)} of ${nf(l.freeAllotment)} free per month</div>
    </div>`;
  }).join("") + `<div class="pill ${ft.allWithinFreeTier ? "ok" : "warn"}" style="margin-top:4px"><span class="dot"></span>${ft.allWithinFreeTier ? "All within free tier" : "Tightest: " + esc(ft.tightestConstraint)}</div>`;

  $("#billed").innerHTML = `
    <div class="pill muted" style="margin-bottom:9px"><span class="dot"></span>not configured</div>
    <div style="font-size:12.5px" class="muted">${esc(billed.reason)}</div>
    <h3 style="margin:14px 0 6px;font-size:12px;letter-spacing:.5px;color:var(--text-dim)">LATENCY</h3>
    <div style="font-size:12.5px" class="muted">${esc(billed.latencyCharacteristics)}</div>
    <h3 style="margin:14px 0 6px;font-size:12px;letter-spacing:.5px;color:var(--text-dim)">RECONCILIATION PLAN</h3>
    <div style="font-size:12.5px" class="muted">${esc(billed.reconciliationPlan)}</div>`;

  $("#recHint").innerHTML = esc(recs.note);
  $("#recs").innerHTML = recs.recommendations.length === 0
    ? `<div class="empty">No recommendations in this window.</div>`
    : recs.recommendations.map(r => `<div class="rec ${r.severity}">
        <div class="title">${esc(r.title)} <span class="pill muted" style="margin-left:5px">${esc(r.provenance)}</span></div>
        <div class="why">${esc(r.recommendation)}</div>
        <div class="why faint" style="font-size:12px">${esc(r.rationale)}</div>
        <div style="font-size:12px;display:flex;gap:14px;flex-wrap:wrap;margin-top:5px">
          <span class="faint">${esc(r.observedMetric)}: <strong>${esc(r.observedValue)}${esc(r.unit)}</strong></span>
          <span class="faint">${esc(r.timeWindow)}</span>
          <span class="faint">confidence ${esc(r.confidence)}</span>
          ${r.estimatedSavingUsdPerMonth !== null
            ? `<span class="saving">saves ${usd(r.estimatedSavingUsdPerMonth, 4)}/mo</span>`
            : `<span class="saving none">${esc(r.savingStatus)}</span>`}
        </div>
        ${r.savingBasis ? `<div class="faint" style="font-size:11px;margin-top:4px">basis: ${esc(r.savingBasis)}</div>` : ""}
        <code>${esc(r.suggestedAction)}</code></div>`).join("") +
      `<div class="faint" style="font-size:12px;margin-top:10px"><strong>Google Recommender:</strong> ${esc(recs.googleRecommender.reason)}</div>`;
}

/* ---------- alerts ---------- */
async function loadAlerts() {
  const a = await api("/api/v1/alerts");
  $("#alertSources").innerHTML = `<h3>Where alerts come from</h3><div class="hint">${esc(a.note)}</div>` +
    a.sources.map(s => `<div style="margin-bottom:9px;font-size:12.5px">
      <span class="pill ${s.source === "fast-path" ? "info" : "ok"}">${esc(s.source)}</span>
      <span class="faint" style="margin-left:7px">latency ${esc(s.latency)}</span>
      <div class="muted" style="margin-top:3px">${esc(s.description)}</div></div>`).join("");

  $("#alertList").innerHTML = a.rules.map(r => `
    <div class="card" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
        <strong>${esc(r.name)}</strong>
        <span class="pill ${r.category === "cost" ? "cost" : r.category === "business" ? "warn" : "info"}">${esc(r.category)}</span>
        <span class="pill muted">${esc(r.source)}</span>
        ${r.breachCount ? `<span class="pill err"><span class="dot"></span>${r.breachCount} breaching</span>` : `<span class="pill ok"><span class="dot"></span>ok</span>`}
        <span style="margin-left:auto;display:flex;gap:7px;align-items:center">
          <label class="faint" style="font-size:12px">threshold</label>
          <input type="number" step="any" value="${r.threshold}" data-th="${esc(r.id)}" style="width:100px">
          <span class="faint" style="font-size:12px">${esc(r.unit)}</span>
          <button class="btn" data-save="${esc(r.id)}">Save</button>
          <button class="btn" data-tog="${esc(r.id)}" data-on="${r.enabled}">${r.enabled ? "Disable" : "Enable"}</button>
        </span>
      </div>
      <div class="muted" style="font-size:12.5px;margin-top:7px">${esc(r.description)}</div>
      <div class="faint" style="font-size:12px;margin-top:4px"><strong>Why this rule:</strong> ${esc(r.rationale)}</div>
      ${r.currentlyBreaching.map(b => `<div style="font-size:12px;margin-top:6px;color:var(--err)">
          ▸ ${esc(b.scope)} — observed <strong>${nf(b.observed, 2)}${esc(b.unit)}</strong> vs threshold ${nf(b.threshold, 2)}${esc(b.unit)}</div>`).join("")}
    </div>`).join("");

  $$("#alertList [data-save]").forEach(btn => btn.addEventListener("click", async () => {
    const id = btn.dataset.save;
    const v = parseFloat($(`#alertList [data-th="${id}"]`).value);
    btn.disabled = true;
    await api(`/api/v1/alerts/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ threshold: v }) });
    await loadAlerts();
  }));
  $$("#alertList [data-tog]").forEach(btn => btn.addEventListener("click", async () => {
    const id = btn.dataset.tog, on = btn.dataset.on === "true";
    btn.disabled = true;
    await api(`/api/v1/alerts/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !on }) });
    await loadAlerts();
  }));
}

/* ---------- incidents ---------- */
async function loadIncidents() {
  const d = await api(`/api/v1/incidents?window=${Math.max(state.window, 360)}`);
  const st = d.stats;

  $("#incTiles").innerHTML = [
    tile("Breaching now", nf(st.breachingNow), "Thresholds currently crossed",
         st.breachingNow ? "err" : "ok"),
    tile("Resolved", nf(st.resolvedInWindow), "Stopped breaching during this window"),
    tile("Total episodes", nf(st.totalEpisodes),
         `Across ${nf(st.distinctIncidents)} distinct incident${st.distinctIncidents === 1 ? "" : "s"}`),
    tile("Critical", nf(st.critical), "Highest-severity incidents",
         st.critical ? "err" : "muted"),
  ].join("");

  // Say plainly which numbers are live and which are a day behind. A reader
  // who does not know the difference will trust the stale one.
  $("#incLegend").innerHTML = (d.dataSources || []).map(x => `
    <span class="k" title="${esc(x.note || "")}">
      <i style="background:${x.ok ? "var(--ok)" : "var(--clay, var(--warn))"}"></i>
      ${esc(x.label)} · ${esc(x.freshness)}
    </span>`).join("");

  $("#rootCauseBanner").innerHTML = d.suspectedRootCauseService
    ? `<div class="card" style="border-color:var(--err)">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span class="pill err"><span class="dot"></span>suspected root cause</span>
          <strong style="font-size:16px">${esc(d.suspectedRootCauseService)}</strong>
          <span class="faint" style="font-size:12.5px">identified from failing-dependency citations — inspect this service first, not the ones that alerted.</span>
        </div></div>` : "";

  renderIncidentGroups("#incBreaching", d.breaching, "Nothing is breaching.");
  renderIncidentGroups("#incResolved", d.resolved, "Nothing resolved in this window.");

  const badge = $("#incBadge");
  badge.style.display = st.breachingNow ? "inline-block" : "none";
  badge.textContent = st.breachingNow;
}

function renderIncidentGroups(sel, groups, emptyMsg) {
  const box = $(sel);
  if (!groups.length) { box.innerHTML = `<div class="empty">${esc(emptyMsg)}</div>`; return; }
  box.innerHTML = groups.map((g, i) => {
    const spark = (g.sparkline || []).map(v =>
      `<i class="${v > 0.55 ? "hot" : ""}" style="height:${Math.max(8, v * 100)}%"></i>`).join("");
    const when = g.status === "BREACHING" ? "just now" : ago(g.resolvedAt);
    return `<div class="inc-row ${esc(g.severity)}" data-grp="${esc(sel)}-${i}">
      <div class="inc-top">
        <div style="flex:1;min-width:240px">
          <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
            <h4>${esc(g.title)}</h4>
            <span class="pill ${{ CRITICAL: "crit", HIGH: "err", MEDIUM: "warn" }[g.severity] || "muted"}">${esc(g.severity)}</span>
            ${g.episodes > 1 ? `<span class="pill muted">${g.episodes} episodes</span>` : ""}
          </div>
          <div class="inc-sum">${esc(g.summary)}</div>
          <div class="inc-meta">
            <span>Breaching for<strong>${durShort(g.breachingForS)}</strong></span>
            <span>Across<strong>${durShort(g.acrossS)}</strong></span>
            <span>Matching entries<strong>${nf(g.matchingEntries)}</strong></span>
            ${g.ruleWindowMinutes ? `<span>Rule window<strong>${g.ruleWindowMinutes}m</strong></span>` : ""}
            <span>Started<strong>${hms(g.startedAt)}</strong></span>
          </div>
        </div>
        <div class="inc-right">
          <div class="spark">${spark}</div>
          <span class="faint" style="font-size:12px;min-width:62px;text-align:right">${when}</span>
          <button class="chev" data-exp="${esc(g.primaryIncidentId)}" title="Show impact, root cause, errors and trace">▾</button>
        </div>
      </div>
      <div class="inc-detail" id="det-${esc(g.primaryIncidentId)}"></div>
    </div>`;
  }).join("");

  $$(sel + " [data-exp]").forEach(btn => btn.addEventListener("click", async () => {
    const id = btn.dataset.exp;
    const panel = $("#det-" + CSS.escape(id));
    if (panel.classList.contains("open")) {
      panel.classList.remove("open"); btn.textContent = "▾"; return;
    }
    panel.classList.add("open"); btn.textContent = "▴";
    panel.innerHTML = `<div class="empty"><span class="spin"></span> loading evidence…</div>`;
    panel.innerHTML = await incidentDetailHtml(id);
    wireExplain(panel, id);
  }));
}

function durShort(s) {
  if (s === null || s === undefined) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}
function ago(ts) {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/* The evidence that sits beneath an incident, in the order a responder reads
   it: what it cost, what probably caused it, what actually failed, one request
   end to end, then a narrative. Everything is built from the deterministic
   evidence bundle -- the explanation narrates it and adds nothing. */
async function incidentDetailHtml(id) {
  let d;
  try { d = await api("/api/v1/incidents/" + id); }
  catch (e) { return `<div class="empty">Could not load evidence: ${esc(e.message)}</div>`; }

  const ev = d.evidence, c = ev.correlation, md = ev.metricsDelta;
  const cost = ev.impact.cost, biz = ev.impact.business;

  const deltaRow = (label, p, unit = "", dec = 2) => {
    if (!p || (p.before == null && p.during == null)) return "";
    const up = (p.during ?? 0) > (p.before ?? 0);
    return `<tr><td>${esc(label)}</td><td class="num">${nf(p.before, dec)}${unit}</td>
      <td class="num ${up ? "delta-up" : "delta-down"}">${nf(p.during, dec)}${unit}</td></tr>`;
  };

  const citations = Object.entries(c.dependencyCitations || {});
  const maxCite = citations.length ? Math.max(...citations.map(([, v]) => v)) : 1;

  return `
    <h5>Impact</h5>
    <div class="grid g2">
      <div class="card"><div class="label faint" style="font-size:11px">CLOUD COST (modeled)</div>
        <div style="font-size:23px;font-weight:700" class="${(cost.deltaUsdPerHour || 0) > 0 ? "delta-up" : "delta-down"}">
          ${(cost.deltaUsdPerHour || 0) > 0 ? "+" : ""}${usd(cost.deltaUsdPerHour)}<span class="faint" style="font-size:13px">/hr</span></div>
        <div class="faint" style="font-size:11.5px">baseline ${usd(cost.baselineUsdPerHour)}/hr · dominant driver ${esc(cost.dominantDriver || "—")}</div>
        <div class="faint" style="font-size:11.5px">incurred so far ${usd(cost.incurredUsdSoFar, 5)}</div></div>
      <div class="card"><div class="label faint" style="font-size:11px">REVENUE AT RISK (measured)</div>
        <div style="font-size:23px;font-weight:700" class="saving">${inr(biz.revenueAtRiskInr)}</div>
        <div class="faint" style="font-size:11.5px">${nf(biz.failedCheckouts)} failed checkouts · success ${pct(biz.checkoutSuccessRatePct)}</div></div>
    </div>
    <div class="faint" style="font-size:11px;margin-top:6px">${esc(cost.disclaimer)}</div>

    <h5>Suspected root cause</h5>
    ${c.suspectedRootCauseService ? `
      <div style="display:flex;align-items:center;gap:9px;margin-bottom:8px;flex-wrap:wrap">
        <span class="pill err"><span class="dot"></span>${esc(c.suspectedRootCauseService)}</span>
        ${c.isLikelyDownstream ? `<span class="faint" style="font-size:12px">this service is downstream, not at fault</span>` : ""}
      </div>
      <div class="faint" style="font-size:12px;margin-bottom:7px">${esc(c.rootCauseBasis || "")}</div>
      ${citations.map(([k, v]) => `
        <div style="font-size:12px;display:flex;justify-content:space-between"><span>${esc(k)}</span><span class="num">${nf(v)}</span></div>
        <div class="bar-track" style="margin-bottom:5px"><div class="bar-fill err" style="width:${100 * v / maxCite}%"></div></div>`).join("")}`
      : `<div class="faint">Not determinable from this window.</div>`}

    <h5>Top errors</h5>
    ${ev.topErrors.length ? ev.topErrors.map(e => `
      <div style="font-size:12.5px;display:flex;justify-content:space-between;padding:3px 0">
        <span><strong style="color:var(--err)">${esc(e.errorCode)}</strong>
          <span class="faint">${esc(e.service)} ${esc(e.route || "")}</span></span>
        <span class="num">${nf(e.count)}</span></div>`).join("")
      : '<div class="faint">None grouped in this window.</div>'}

    <h5>Sample trace</h5>
    ${ev.sampleTrace.length ? ev.sampleTrace.map(e => `
      <div style="font-size:12px;display:flex;gap:9px;padding:2px 0">
        <span class="faint" style="width:78px">${esc((e.service || "").replace("cognikart-", ""))}</span>
        <span class="mono" style="flex:1">${esc(e.event)}</span>
        <span class="num faint">${e.latencyMs != null ? nf(e.latencyMs) + "ms" : ""}</span>
        <span style="width:34px;text-align:right;color:${(e.httpStatus || 0) >= 500 ? "var(--err)" : "inherit"}">${e.httpStatus ?? ""}</span>
      </div>`).join("")
      : '<div class="faint">No trace still buffered for this window.</div>'}

    <h5>Explain</h5>
    <button class="btn primary" data-explain="${esc(id)}">Explain this incident</button>
    <div class="explain-out" style="margin-top:11px"></div>

    <h5>Before vs during</h5>
    <table><thead><tr><th>Metric</th><th class="right">Before</th><th class="right">During</th></tr></thead><tbody>
      ${deltaRow("Requests / min", md.requestsPerMin)}
      ${deltaRow("5xx / min", md.errors5xxPerMin)}
      ${deltaRow("p95 latency", md.p95LatencyMs, "ms", 0)}
      ${deltaRow("Retries / min", md.retriesPerMin)}
      ${deltaRow("Payment attempts / min", md.paymentAttemptsPerMin)}
      ${deltaRow("Log volume", md.logMibPerMin, " MiB", 3)}
      ${deltaRow("Instances (max)", md.instanceCountMax, "", 0)}
    </tbody></table>

    <h5>Timeline</h5>
    <div class="tl">${d.timeline.map(t => `<div class="tl-item ${esc(t.kind)}">
      <div class="t">${hms(t.ts)} · ${esc(t.kind)}</div>
      <div style="font-size:12.5px">${esc(t.text)}</div></div>`).join("")}</div>`;
}

function wireExplain(root, id) {
  const btn = root.querySelector("[data-explain]");
  const out = root.querySelector(".explain-out");
  if (!btn || !out) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.innerHTML = `<span class="spin"></span> analysing evidence…`;
    try {
      const r = await api(`/api/v1/incidents/${id}/analyze`, { method: "POST" });
      const g = r.grounding;
      out.innerHTML = `
        <div style="display:flex;gap:7px;margin-bottom:9px;flex-wrap:wrap">
          <span class="pill ${r.provider === "vertex-ai" ? "info" : "muted"}">${esc(r.provider)}${r.model ? " · " + esc(r.model) : ""}</span>
          <span class="pill ${g.grounded ? "ok" : "warn"}"><span class="dot"></span>${g.grounded ? "fully grounded" : g.unsupportedNumbers.length + " unverified number(s)"}</span>
          <span class="pill muted">${g.checkedNumbers} numbers checked</span>
          ${r.cached ? `<span class="pill muted">cached</span>` : ""}
        </div>
        ${r.fallbackUsed ? `<div class="faint" style="font-size:11.5px;margin-bottom:8px">Deterministic narrative (${esc(r.fallbackReason)}). Built from the same evidence bundle.</div>` : ""}
        <pre class="json" style="white-space:pre-wrap;color:var(--text)">${esc(r.narrative)}</pre>
        <div class="faint" style="font-size:11px">${esc(g.method)}</div>`;
    } catch (e) {
      out.innerHTML = `<div class="faint">Explanation failed: ${esc(e.message)}</div>`;
    }
    btn.disabled = false;
  });
}

/* Opened from the action queue, which links to a specific incident. */
async function showIncident(id) {
  openDrawer("Loading incident…", "", `<div class="empty"><span class="spin"></span></div>`);
  let head = { title: "Incident", sub: "" };
  try {
    const d = await api("/api/v1/incidents/" + id);
    head = {
      title: esc(d.title),
      sub: `<span class="pill ${d.status === "OPEN" ? "err" : "ok"}">${esc(d.status)}</span>
            <span class="pill ${{ CRITICAL: "crit", HIGH: "err", MEDIUM: "warn" }[d.severity] || "muted"}">${esc(d.severity)}</span>
            <span class="faint">${dur(d.durationS)} · started ${hms(d.startedAt)}</span>`,
    };
  } catch (e) { /* fall through to the error the body renders */ }
  openDrawer(head.title, head.sub, await incidentDetailHtml(id));
  wireExplain($("#drawerBody"), id);
}

/* ---------- header controls ----------
   No fault-injection control here, deliberately. OpsMind holds read-only IAM
   roles and exposes a read-only API: it observes and advises, and cannot act
   on -- or break -- the application it watches. Injection lives in CogniKart's
   own Scenario Lab, which is where it belongs. */

$("#refreshBtn").addEventListener("click", async () => {
  const b = $("#refreshBtn");
  const label = b.querySelector("span");
  b.disabled = true; b.classList.add("spinning"); label.textContent = "Refreshing";
  await refresh();
  b.classList.remove("spinning"); b.disabled = false; label.textContent = "Refreshed";
  setTimeout(() => { label.textContent = "Refresh"; }, 1400);
});

let notifOpen = false;
$("#bellBtn").addEventListener("click", async () => {
  notifOpen = !notifOpen;
  $("#notifPanel").hidden = !notifOpen;
  if (notifOpen) await loadNotifications();
});
document.addEventListener("click", e => {
  if (!notifOpen) return;
  if (e.target.closest("#notifPanel") || e.target.closest("#bellBtn")) return;
  notifOpen = false; $("#notifPanel").hidden = true;
});

async function loadNotifications() {
  const n = await api("/api/v1/notifications?limit=25");
  $("#notifNote").textContent = n.note;
  $("#notifList").innerHTML = n.notifications.length === 0
    ? `<div class="empty">Nothing has opened or resolved recently.</div>`
    : n.notifications.map(x => `
      <div class="notif-item" data-inc="${esc(x.incidentId)}">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="pill ${x.kind === "opened" ? "err" : "ok"}"><span class="dot"></span>${x.kind}</span>
          <span class="pill ${{ CRITICAL: "crit", HIGH: "err", MEDIUM: "warn" }[x.severity] || "muted"}">${esc(x.severity)}</span>
          <span class="faint" style="font-size:11.5px;margin-left:auto">${hms(x.ts)}</span>
        </div>
        <div style="font-weight:600;font-size:13px;margin-top:6px">${esc(x.title)}</div>
        <div class="faint" style="font-size:12px;margin-top:2px">${esc(x.summary)}</div>
        ${x.episode > 1 ? `<div class="faint" style="font-size:11px;margin-top:3px">episode ${x.episode}</div>` : ""}
      </div>`).join("");
  $$("#notifList [data-inc]").forEach(el => el.addEventListener("click", () => {
    notifOpen = false; $("#notifPanel").hidden = true;
    go("incidents");
  }));
}

function paintBell(unread) {
  const b = $("#bellBadge");
  b.hidden = !unread;
  b.textContent = unread > 99 ? "99+" : unread;
}

/* ---------- orchestration ---------- */
async function refresh() {
  try {
    await loadMeta();
    if (!$("#logSvc").options.length || $("#logSvc").options.length === 1) {
      const s = await api("/api/v1/services?window=60");
      $("#logSvc").innerHTML = `<option value="">all services</option>` +
        s.services.map(x => `<option value="${esc(x.service)}">${esc(x.service)}</option>`).join("");
    }
    if (state.view === "overview") await loadOverview();
    else if (state.view === "logs") { await loadLogsInitial(); await loadErrors(); }
    else if (state.view === "insights") await loadInsights();
    else if (state.view === "services") await loadServices();
    else if (state.view === "setup") { await loadProjects(); await loadSetup(); }
    else if (state.view === "resources") await loadResources();
    else if (state.view === "cost") await loadCost();
    else if (state.view === "alerts") await loadAlerts();
    else if (state.view === "incidents") await loadIncidents();
    if (state.view !== "overview") {
      const o = await api(`/api/v1/overview?window=${state.window}`);
      setHealthPill(o.health);
      const open = (o.incidents && o.incidents.openCount) || 0;
      const badge = $("#incBadge");
      badge.style.display = open ? "inline-block" : "none";
      badge.textContent = open;
    }
    const n = await api("/api/v1/notifications?limit=25");
    paintBell(n.unread);
    if (notifOpen) await loadNotifications();
  } catch (e) { console.error("refresh failed", e); }
}

(async function init() {
  decorateNav();
  decorateHeadings();
  startStream();
  applyRoute();          // reads the hash, sets the view, starts the timer
})();
})();
