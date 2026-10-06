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
  services: [], es: null, meta: null, charts: {}, incidents: [], scenarios: [], knownEvents: new Set(),
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
/* Global Chart.js look. Recessive grid and axes, no x gridlines, theme fonts,
   one tooltip style, rounded bar ends, 2px lines with no resting points. */
function applyChartTheme() {
  if (typeof Chart === "undefined") return;
  const d = Chart.defaults;
  d.font.family = css("--sans") || "system-ui";
  d.font.size = 11;
  d.color = css("--text-faint");
  d.borderColor = css("--border-soft");
  d.elements.line.borderWidth = 2;
  d.elements.line.tension = .32;
  d.elements.point.radius = 0;
  d.elements.point.hoverRadius = 4;
  d.elements.point.hoverBorderWidth = 2;
  d.elements.bar.borderRadius = 3;
  d.plugins.legend.labels.usePointStyle = true;
  d.plugins.legend.labels.pointStyle = "rectRounded";
  d.plugins.legend.labels.boxWidth = 8;
  d.plugins.legend.labels.boxHeight = 8;
  d.plugins.legend.labels.padding = 14;
  d.plugins.legend.labels.color = css("--text-dim");
  Object.assign(d.plugins.tooltip, {
    backgroundColor: css("--bg-elev2"), borderColor: css("--border"), borderWidth: 1,
    titleColor: css("--text"), bodyColor: css("--text-dim"), padding: 10, cornerRadius: 8,
    boxPadding: 4, usePointStyle: true,
    titleFont: { weight: "600", size: 12 }, bodyFont: { size: 12 },
  });
}

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
      x: { grid: { display: false }, border: { color: css("--border") }, ticks: { color: tick, font: { size: 10.5 }, maxRotation: 0, autoSkipPadding: 22 } },
      y: { grid: { color: grid }, border: { display: false }, ticks: { color: tick, font: { size: 10.5 }, padding: 6, maxTicksLimit: 6 }, beginAtZero: true },
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
    if (params.severity !== undefined) setSel($("#logSev"), params.severity);
    if (params.service !== undefined) setSel($("#logSvc"), params.service);
    if (params.q !== undefined) $("#logQ").value = params.q;
    if (params.status !== undefined) setSel($("#logStatus"), params.status);
    if (params.route !== undefined) setSel($("#logRoute"), params.route);
    if (params.event !== undefined) setSel($("#logEvent"), params.event);
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
  // Charts are not redrawn while hidden (alerts still arrive on the live
  // stream); returning to the tab refreshes immediately instead of waiting.
  if (!scheduleRefresh.bound) {
    scheduleRefresh.bound = true;
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }
  const sel = $("#windowSel");
  if (sel) sel.title = `Re-polled every ${Math.round(ms / 1000)}s at this window`;
  const lbl = $("#liveLabel");
  if (lbl) lbl.textContent = `Live ${Math.round(ms / 1000)}s`;
}
$("#themeBtn").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  applyChartTheme(); destroyCharts(); refresh();
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

/* ---------- toast ----------
   Short confirmation in the corner. Setup's project switch already called
   toast() before it existed, so a successful switch threw and never reached
   the Overview; defining it fixes that path too. */
function toast(msg) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast"; el.setAttribute("role", "status"); el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ---------- project picker ----------
   Same switch as the Open button in Setup: POST /projects/select. Projects
   this installation can list but not read are shown, disabled, so a missing
   permission is visible rather than silently absent. */
const MANAGE = "__manage__";
async function loadProjectPicker() {
  const sel = $("#projectSel");
  if (!sel) return;
  let d;
  try { d = await api("/api/v1/projects"); } catch { return; }
  const list = d.projects || [];
  sel.innerHTML = list.map(p =>
    `<option value="${esc(p.projectId)}"${p.active ? " selected" : ""}${p.connected === false ? " disabled" : ""}>` +
    `${esc(p.displayName || p.projectId)}${p.connected === false ? " (no log access)" : ""}</option>`).join("") +
    `<option disabled>──────────</option><option value="${MANAGE}">Manage projects…</option>`;
  sel.dataset.current = (list.find(p => p.active) || {}).projectId || "";
}
$("#projectSel").addEventListener("change", async e => {
  const sel = e.target, id = sel.value;
  if (id === MANAGE) { sel.value = sel.dataset.current; go("setup"); return; }
  if (!id || id === sel.dataset.current) return;
  sel.disabled = true;
  try {
    await api("/api/v1/projects/select", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ projectId: id }),
    });
    sel.dataset.current = id;
    state.logs = [];
    toast(`Now viewing ${sel.options[sel.selectedIndex].text}`);
    await refresh();
  } catch (err) {
    sel.value = sel.dataset.current;
    toast("Could not switch: " + err.message);
  } finally {
    sel.disabled = false;
  }
});

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
/* One label-and-value line. Used wherever evidence is listed out. */
function fact(label, valueHtml) {
  return `<div class="fact"><span class="fk">${esc(label)}</span><span class="fv">${valueHtml}</span></div>`;
}
/* Only text a model actually wrote carries this tag. Deterministic fallbacks
   are shown untagged, because nothing generated them. */
const AI_TAG = `<span class="pill ai">✦ AI · Gemini on Vertex AI</span>`;
// Without an href the tile is plain: no arrow, nothing to click. A tile that
// links to the page it sits on promises a destination that does not exist.
function linkTile({ label, value, valueColor, foot, href, spark, sparkColor }) {
  const tag = href ? "a" : "div";
  return `<${tag} class="card ktile${href ? "" : " static"}"${href ? ` href="${href}"` : ""}>
    <div class="ktile-label">${esc(label)}${href ? '<span class="ktile-go" aria-hidden="true">→</span>' : ""}</div>
    <div class="ktile-row"><div class="ktile-value"${valueColor ? ` style="color:var(${valueColor})"` : ""}>${value}</div>
      ${spark ? sparkline(spark, sparkColor) : ""}</div>
    <div class="ktile-foot">${foot}</div></${tag}>`;
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
      // 2px surface gap between stacked segments, so adjacent severities never touch.
      borderColor: css("--bg-elev"), borderWidth: { top: 2, right: 0, bottom: 0, left: 0 }, borderSkipped: false,
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
       <span class="strip-fresh" title="Last refreshed">
         <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/></svg>
         Refreshed ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
         <span class="strip-fresh-sep">·</span>${state.window}-min window
       </span>
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
/* ---------- today vs yesterday (Firestore history) ----------
   Rendered from the daily rollups. It never estimates a missing day: with
   fewer than two days of data the panel says it is still collecting, because
   a comparison against a day nobody observed is worse than no comparison. */
const HIST_ROWS = [
  { key: "requests", label: "Requests", fmt: v => nf(v) },
  { key: "errors", label: "Errors (4xx + 5xx)", fmt: v => nf(v), worseUp: true },
  { key: "errors5xx", label: "Server errors (5xx)", fmt: v => nf(v), worseUp: true },
  { key: "p95LatencyMs", label: "Latency (mean of per-minute p95)", fmt: v => nf(v) + " ms", worseUp: true },
  { key: "cpuPct", label: "CPU", fmt: v => pct(v) , worseUp: true },
  { key: "memoryMb", label: "Memory", fmt: v => nf(v) + " MB", worseUp: true },
  { key: "logMib", label: "Log volume", fmt: v => nf(v, 2) + " MiB", worseUp: true },
  { key: "checkoutsConfirmed", label: "Checkouts confirmed", fmt: v => nf(v) },
  { key: "modeledUsdPerHour", label: "Modeled cost", fmt: v => "$" + nf(v, 4) + "/hr", worseUp: true },
];

function histDelta(m, worseUp) {
  if (m.changePct === null || m.changePct === undefined)
    return `<span class="faint">${esc(m.reason || "—")}</span>`;
  const up = m.changePct > 0;
  const bad = worseUp ? up : !up;
  const tone = Math.abs(m.changePct) < 1 ? "--text-faint" : (bad ? "--err" : "--ok");
  return `<span style="color:var(${tone})">${up ? "+" : ""}${m.changePct}%</span>`;
}

async function loadHistory() {
  const card = $("#histCard"), hint = $("#histHint"), table = $("#histTable");
  if (!card) return;
  let h;
  try { h = await api("/api/v1/history/compare"); }
  catch (e) { h = { available: false, message: "Historical data is being collected." }; }

  if (!h.available) {
    $("#histChip").innerHTML = chip(h.enabled ? "collecting" : "not enabled", "--text-faint");
    hint.hidden = false;
    hint.textContent = h.message || "Historical data is being collected.";
    table.innerHTML = "";
    return;
  }
  $("#histChip").innerHTML = chip(`${esc(h.today.date)} vs ${esc(h.yesterday.date)}`, "--ok");
  hint.hidden = false;
  hint.textContent = h.note || "";
  table.innerHTML =
    `<thead><tr><th>Metric</th><th class="right">Today</th><th class="right">Yesterday</th><th class="right">Change</th></tr></thead><tbody>` +
    HIST_ROWS.map(r => {
      const m = (h.metrics || {})[r.key] || {};
      if (m.today === null && m.yesterday === null) return "";
      return `<tr><td>${esc(r.label)}</td>
        <td class="num">${m.today === null || m.today === undefined ? "—" : r.fmt(m.today)}</td>
        <td class="num">${m.yesterday === null || m.yesterday === undefined ? "—" : r.fmt(m.yesterday)}</td>
        <td class="num">${histDelta(m, r.worseUp)}</td></tr>`;
    }).join("") +
    `</tbody>`;
}

/* Modeled cost checked against Google's invoice. Shown only when there is
   billed data to check against -- an accuracy claim with nothing behind it
   would be worse than no claim. */
async function loadReconcile() {
  const el = $("#billed");
  if (!el) return;
  try {
    const r = await api("/api/v1/cost/reconcile");
    if (r.accuracyPct === null || r.accuracyPct === undefined) return;
    const d = document.createElement("div");
    d.style.cssText = "margin-top:10px;padding:10px;border:1px solid var(--border);border-radius:8px";
    d.innerHTML = `<div style="font-size:13px"><strong>Modeled cost is ${r.accuracyPct}% accurate</strong> against billed, over ${r.windowDays} days.</div>
      <div class="faint" style="font-size:11.5px;margin-top:4px">${esc(r.caveat || "")}</div>`;
    el.appendChild(d);
  } catch (e) { /* the billed panel stands on its own */ }
}

async function loadOverview() {
  const [o, f] = await Promise.all([
    api(`/api/v1/overview?window=${state.window}`),
    api(`/api/v1/funnel?window=${state.window}`),
  ]);
  loadHistory();   // not awaited: history must never delay the live overview
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
      // Stacked bands: each fills only down to the band beneath it, so the
      // tints never overlap into a muddy grey.
      { ...ds("CPU", cs.map(p => p.usdPerHour.cpu), css("--s1"), true), fill: "origin" },
      { ...ds("Memory", cs.map(p => p.usdPerHour.memory), css("--s2"), true), fill: "-1" },
      { ...ds("Requests", cs.map(p => p.usdPerHour.requests), css("--s3"), true), fill: "-1" },
      { ...ds("Logging", cs.map(p => p.usdPerHour.logging), css("--s4"), true), fill: "-1" },
    ],
  }, { scales: { y: { stacked: true, grid: { color: css("--border-soft") }, ticks: { color: css("--text-faint"), font: { size: 10 }, callback: v => "$" + Number(v).toFixed(3) } }, x: { stacked: true, grid: { display: false }, ticks: { color: css("--text-faint"), font: { size: 10.5 }, maxRotation: 0, autoSkipPadding: 22 } } } });

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
    ? `<span aria-hidden="true">✦</span> ${ai ? "Gemini" : "Explain"}`
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
  // Closed: two lines (status + title, then one-line reason). Open: the same
  // facts in fixed, labelled rows, so each one is found by position.
  const openKey = state.openAction;
  box.innerHTML = q.actions.map((a, i) => {
    const sev = { CRITICAL: "crit", HIGH: "err", MEDIUM: "warn", LOW: "muted" }[a.severity] || "muted";
    const imp = a.impact || {};
    const bits = [];
    if (imp.revenueAtRiskInr) bits.push(`${inr(imp.revenueAtRiskInr)} revenue at risk`);
    if (imp.cloudCostPerHourUsd) bits.push(`${usd(imp.cloudCostPerHourUsd)}/hr cloud cost`);
    if (imp.savingPerMonthUsd) bits.push(`saves ${usd(imp.savingPerMonthUsd)}/mo`);
    const cond = (a.evidence && a.evidence.conditions) || [];
    const key = a.id || a.title;
    const open = openKey === key;
    const row = (label, html) => html ? `<div class="act-row"><dt>${label}</dt><dd>${html}</dd></div>` : "";
    return `<div class="act ${a.severity}${open ? " open" : ""}" data-act="${i}" data-key="${esc(key)}">
      <button class="act-head" aria-expanded="${open}">
        <span class="act-line1">
          <span class="pill ${a.firing ? "err" : "muted"}"><span class="dot"></span>${a.firing ? "firing" : "standing"}</span>
          <span class="pill ${sev}">${esc(a.severity)}</span>
          <strong class="act-title">${esc(a.title)}</strong>
          <span class="act-age faint">${a.ageMinutes ? nf(a.ageMinutes, 0) + "m" : ""}</span>
          <span class="act-chev" aria-hidden="true">▾</span>
        </span>
        <span class="act-line2">${esc(a.whyItMatters)}</span>
      </button>
      <div class="act-body"${open ? "" : " hidden"}>
        <dl>
          ${row("Why it matters", esc(a.whyItMatters))}
          ${row("First step", esc(a.firstStep))}
          ${row("Fixed when", esc(a.howYouWillKnow))}
          ${row("Impact", bits.map(esc).join(" · "))}
          ${row("Conditions", cond.map(c => `<code>${esc(c.rule)}</code>`).join(" "))}
        </dl>
        ${a.link ? `<button class="btn act-go" data-go="${i}">Investigate →</button>` : ""}
      </div>
    </div>`;
  }).join("");

  $$("#actionQueue .act-head").forEach(h => h.addEventListener("click", () => {
    const el = h.closest(".act"), key = el.dataset.key;
    state.openAction = state.openAction === key ? null : key;
    $$("#actionQueue .act").forEach(x => {
      const on = x.dataset.key === state.openAction;
      x.classList.toggle("open", on);
      x.querySelector(".act-head").setAttribute("aria-expanded", on);
      x.querySelector(".act-body").hidden = !on;
    });
  }));
  $$("#actionQueue [data-go]").forEach(btn => btn.addEventListener("click", () => {
    const a = q.actions[+btn.dataset.go];
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
  cloud: '<path d="M17.5 19a4.5 4.5 0 1 0-1.3-8.8A6 6 0 1 0 6 18h11.5"/>',
  pin: '<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21Z"/><circle cx="12" cy="9.5" r="2.5"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
  sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"/>',
  scroll: '<path d="M8 21h11a2 2 0 0 0 2-2v-1H10v1a2 2 0 1 1-4 0V5a2 2 0 0 0-2-2 2 2 0 0 0-2 2v2h4"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
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
  gauge: '<path d="M12 14l4-4"/><path d="M3.3 19a10 10 0 1 1 17.4 0"/>',
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
/* Every filter goes to the server, for the list and the live stream alike,
   so the count, the rows and the stream always agree. */
function logParams() {
  const p = new URLSearchParams();
  const set = (k, v) => { if (v) p.set(k, v); };
  set("severity", $("#logSev").value);
  set("service", $("#logSvc").value);
  set("q", $("#logQ").value.trim());
  set("event", $("#logEvent").value);
  set("status", $("#logStatus").value);
  set("route", $("#logRoute").value);
  return p;
}
/* Select a value even when its option has not been loaded yet (a drill-down
   link can arrive before the choices do); setting .value alone would
   silently fall back to "All". */
function setSel(sel, v) {
  if (v && ![...sel.options].some(o => o.value === v)) sel.add(new Option(v, v));
  sel.value = v || "";
}
function logRow(e) {
  const s = e.httpStatus;
  return `<tr data-log='${esc(JSON.stringify(e))}' class="clickable">
    <td class="num faint">${hms(e.ts)}</td>
    <td><span class="sev-tag" style="--c:var(${sevVar(e.severity)})"><span class="dot"></span>${esc(e.severity)}</span></td>
    <td class="mono">${esc(e.event || "")}</td>
    <td class="faint">${esc(svcShort(e.service))}</td>
    <td class="mono faint">${esc(e.route || "")}</td>
    <td class="num" style="color:${s >= 500 ? "var(--err)" : s >= 400 ? "var(--warn)" : s ? "var(--ok)" : "inherit"}">${s ?? ""}</td>
    <td class="num" style="color:${(e.latencyMs || 0) > 500 ? "var(--warn)" : "inherit"}">${e.latencyMs != null ? nf(e.latencyMs) + "ms" : ""}</td>
  </tr>`;
}
const sevVar = s => ({ ERROR: "--err", CRITICAL: "--err", WARNING: "--warn" }[s] || "--text-faint");
function renderLogs() {
  // Newest first: the initial fetch and the stream arrive in ingest order.
  const rows = state.logs.slice().sort((a, b) => b.ts - a.ts);
  const body = $("#logBody");
  body.innerHTML = rows.length
    ? rows.slice(0, 300).map(logRow).join("")
    : `<tr><td colspan="7" class="empty">No entries match these filters.</td></tr>`;
  const total = state.logTotal ?? state.logs.length;
  $("#logCount").innerHTML = `<b>${nf(total)}</b> matching · ${nf(Math.min(rows.length, 300))} shown${state.paused ? " · paused" : ""}`;
  $$("#logBody tr[data-log]").forEach(tr => tr.addEventListener("click", () => showLogDetail(JSON.parse(tr.dataset.log))));
}
function startStream() {
  if (state.es) state.es.close();
  const es = new EventSource("/api/v1/logs/stream?" + logParams().toString());
  state.es = es;
  es.addEventListener("logs", e => {
    if (state.paused) return;
    const batch = JSON.parse(e.data);
    state.logs = batch.reverse().concat(state.logs).slice(0, state.maxLogs);
    if (state.logTotal != null) state.logTotal += batch.length;
    if (state.view === "logs") renderLogs();
  });
  // Incidents opening and resolving arrive here the moment they happen,
  // whether or not this tab is in front.
  es.addEventListener("notify", e => onNotify(JSON.parse(e.data)));
  es.addEventListener("stats", e => {
    const open = JSON.parse(e.data).openIncidents || 0;
    const badge = $("#incBadge");
    badge.style.display = open ? "inline-block" : "none";
    badge.textContent = open;
  });
  es.onerror = () => { /* EventSource reconnects on its own */ };
}
function logFiltersChanged() { state.logs = []; startStream(); loadLogsInitial(); }
["logSev", "logSvc", "logEvent", "logStatus", "logRoute"].forEach(id => $("#" + id).addEventListener("change", logFiltersChanged));
let qTimer; $("#logQ").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(logFiltersChanged, 350); });
const PAUSE_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 5v14M15 5v14"/></svg>';
const PLAY_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><path d="M7 5v14l12-7z"/></svg>';
$("#logPause").innerHTML = PAUSE_ICON;
$("#logPause").addEventListener("click", () => {
  state.paused = !state.paused;
  $("#logPause").innerHTML = state.paused ? PLAY_ICON : PAUSE_ICON;
  $("#logPause").title = state.paused ? "Resume live stream" : "Pause live stream";
  renderLogs();
});

/* An error filter is "Error severity or worse, or 5xx status". */
const errorFilterOn = () => ["ERROR", "CRITICAL"].includes($("#logSev").value) || $("#logStatus").value === "5";

async function loadLogsInitial() {
  const sev = $("#logSev").value, svc = $("#logSvc").value;
  const p = logParams();
  p.set("limit", "500"); p.set("sinceS", String(state.window * 60));
  const [r, pts, routes] = await Promise.all([
    api("/api/v1/logs?" + p.toString()),
    api(`/api/v1/metrics/series?window=${state.window}${svc ? "&service=" + encodeURIComponent(svc) : ""}`),
    api(`/api/v1/routes?window=${state.window}&limit=200`).catch(() => null),
  ]);
  state.logs = r.entries; state.logTotal = r.total;

  // Route and event choices come from what is actually in the window.
  const keep = (sel, values) => {
    const cur = sel.value;
    sel.innerHTML = `<option value="">All</option>` + values.map(v => `<option${v === cur ? " selected" : ""}>${esc(v)}</option>`).join("");
  };
  keep($("#logRoute"), [...new Set((routes ? routes.routes : []).map(x => x.route))].sort());
  r.entries.forEach(x => x.event && state.knownEvents.add(x.event));
  keep($("#logEvent"), [...state.knownEvents].sort());
  renderLogs();

  // Matching volume: severities at or above the chosen one.
  const nowMin = Math.floor(Date.now() / 60000);
  const lv = complete(pts.points).filter(x => Math.floor(x.ts / 60) < nowMin);
  const floor = sevOrder(sev || "DEBUG");
  const groups = SEV_GROUPS.filter(g => g.from.some(k => sevOrder(k) >= floor));
  upsert("logvol", "chLogVol", "bar", {
    labels: lv.map(x => hhmm(x.ts)),
    datasets: groups.map(g => ({
      label: g.label, data: lv.map(x => sevOf(x, g)),
      backgroundColor: g.key === "INFO" ? css("--text-faint") + "8c" : css(g.color),
      stack: "s", borderRadius: 2, barPercentage: .62, categoryPercentage: .9, maxBarThickness: 18,
      borderColor: css("--bg-elev"), borderWidth: { top: 2, right: 0, bottom: 0, left: 0 }, borderSkipped: false,
    })),
  }, { plugins: { legend: { display: false } }, scales: {
      x: { stacked: true, grid: { display: false }, ticks: { color: css("--text-faint"), maxRotation: 0, autoSkipPadding: 26 } },
      y: { stacked: true, grid: { color: css("--border-soft") }, border: { display: false }, ticks: { color: css("--text-faint"), maxTicksLimit: 5 }, beginAtZero: true } } });
  $("#logVolLegend").innerHTML = groups.map(g => `<span><i style="background:var(${g.color})"></i>${g.label}</span>`).join("");

  $("#errBlock").hidden = !errorFilterOn();
  if (errorFilterOn()) await loadErrors();
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
  const svc = $("#logSvc").value;
  // Carry the log list's severity, so the two panels cannot disagree.
  const sev = $("#logSev").value;
  const e = await api(`/api/v1/errors?window=${state.window}&limit=50`
                      + (sev ? `&severity=${encodeURIComponent(sev)}` : ""));
  const groups = e.groups.filter(g => !svc || g.service === svc);
  const t = e.totals;
  const top = groups[0];
  const atRisk = groups.reduce((s, g) => s + (g.revenueAtRiskInr || 0), 0);
  $("#errTiles").innerHTML = [
    linkTile({ label: "Server errors (5xx)", value: nf(t.errors5xx), valueColor: t.errors5xx ? "--err" : "--ok",
               foot: `${t.requests ? pct(t.errors5xx / t.requests * 100, 2) : "0%"} of requests`, href: "#logs?severity=ERROR" }),
    linkTile({ label: "Error groups", value: nf(groups.length), foot: svc ? svcShort(svc) : "all services", href: "#logs?severity=ERROR" }),
    linkTile({ label: "Top error", value: top ? nf(top.count) : "0", valueColor: top ? "--err" : null,
               foot: top ? `${esc(top.errorCode)} · ${esc(svcShort(top.service))}` : "none", href: "#logs?severity=ERROR" }),
    linkTile({ label: "Revenue at risk", value: atRisk ? inr(atRisk) : "—", valueColor: atRisk ? "--warn" : null,
               foot: "from failed checkouts", href: "#cost" }),
  ].join("");
  $("#errChip").innerHTML = groups.length ? chip(`${nf(groups.length)} error group${groups.length === 1 ? "" : "s"}`, "--err") : chip("No errors", "--ok");

  $("#errTable").innerHTML = groups.length === 0
    ? `<tbody><tr><td class="empty">No errors in this window.</td></tr></tbody>`
    : `<thead><tr><th>Error code</th><th>Service</th><th>Route</th><th class="right">Count</th><th class="right">Revenue at risk</th><th class="right">Last seen</th></tr></thead><tbody>` +
      groups.map((g, i) => `<tr class="clickable" data-g="${i}">
        <td><div class="mono" style="color:var(--err);font-weight:600">${esc(g.errorCode)}</div><div class="faint pat-sub">${esc(g.errorClass || g.pattern || "")}</div></td>
        <td class="mono">${esc(svcShort(g.service))}</td>
        <td class="mono faint">${esc(g.route || "—")}</td>
        <td class="num pat-count">${nf(g.count)}</td>
        <td class="num">${g.revenueAtRiskInr ? inr(g.revenueAtRiskInr) : "—"}</td>
        <td class="num faint">${ago(g.lastSeen)}</td></tr>`).join("") + `</tbody>`;
  $$("#errTable [data-g]").forEach(tr => tr.addEventListener("click", () => showErrorGroup(groups[+tr.dataset.g])));
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
  // Series slots only -- status colours never stand in for a series.
  const palette = [css("--s1"), css("--s2"), css("--s3"), css("--s4")];
  const series = await Promise.all(svcNames.map(n => api(`/api/v1/metrics/series?window=${state.window}&service=${encodeURIComponent(n)}`)));
  const base = complete(pts.points);
  const labels = base.map(p => hhmm(p.ts));
  // One chart per unit, each labelling its own axis, so nothing needs two
  // y-axes and no tick is left reading "00MB".
  const yBase = chartDefaults().scales.y;  // keep the shared grid, drop only the tick text
  const resChart = (key, canvas, field, tick, extraTicks) => upsert(key, canvas, "line", {
    labels,
    datasets: series.map((r, i) => ds(svcShort(svcNames[i]),
      alignTo(base, r.points, field), palette[i % palette.length], false)),
  }, { scales: { y: Object.assign({}, yBase, { ticks: Object.assign({}, yBase.ticks, { callback: tick }, extraTicks || {}) }) } });
  resChart("cpu", "chCpu", "cpuPctMax", v => nf(v) + "%");
  resChart("mem", "chMem", "rssMbMax", v => nf(v) + " MiB");
  resChart("inflight", "chInflight", "inflightMax", v => nf(v));
  // Whole instances only -- fractional ticks would repeat the same label.
  resChart("inst", "chInst", "instanceCount", v => nf(v), { stepSize: 1, precision: 0 });

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

  $("#projError").innerHTML = d.error
    ? `<div class="notice bad" style="margin-bottom:12px"><strong>Cannot list projects.</strong> ${esc(d.error)}
         ${d.howToFix ? `<div class="mono" style="margin-top:6px;font-size:11.5px">${esc(d.howToFix)}</div>` : ""}</div>` : "";

  if (!d.projects.length) {
    $("#projGrid").innerHTML = `<div class="empty">No projects visible to this service account.</div>`;
    return;
  }

  $("#projGrid").innerHTML = d.projects.map(p => {
    const state = p.connected === true ? "ok" : p.connected === false ? "err" : "muted";
    const label = p.connected === true ? "Connected" : p.connected === false ? "No log access" : "Not checked";
    return `<div class="card proj-tile${p.active ? " active" : ""}">
      <div class="proj-top">
        ${ico("cloud", "accent")}
        <div class="proj-id">
          <div class="proj-name">${esc(p.displayName)}</div>
          <div class="mono faint">${esc(p.projectId)}</div>
        </div>
        ${p.active ? `<span class="pill info">Viewing</span>` : ""}
      </div>
      <div class="proj-status"><span class="pill ${state}"><span class="dot"></span>${label}</span></div>
      <div class="fixhint faint mono"></div>
      <div class="proj-actions">
        <button class="btn" data-check="${esc(p.projectId)}">Check access</button>
        <button class="btn primary" data-open="${esc(p.projectId)}" data-active="${p.active ? 1 : 0}">Open</button>
      </div>
    </div>`;
  }).join("");

  $$("#projGrid [data-check]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "Checking…";
    const card = b.closest(".card");
    try {
      const r = await api(`/api/v1/projects/${encodeURIComponent(b.dataset.check)}/access?force=true`);
      const pill = card.querySelector(".proj-status .pill");
      pill.className = "pill " + (r.connected ? "ok" : "err");
      pill.innerHTML = `<span class="dot"></span>${r.connected ? "Connected" : "No log access"}`;
      card.querySelector(".fixhint").textContent = r.connected ? "" : (r.howToFix || r.reason || "");
    } catch (e) {
      card.querySelector(".fixhint").textContent = "Check failed: " + e.message;
    }
    b.disabled = false; b.textContent = "Check access";
  }));

  $$("#projGrid [data-open]").forEach(b => b.addEventListener("click", async () => {
    if (b.dataset.active === "1") { go("overview"); return; }
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
      loadProjectPicker();
      go("overview");
    } catch (e) {
      toast("Could not switch: " + e.message);
      b.disabled = false; b.textContent = "Open";
    }
  }));
}

/* ---------- insights: patterns and anomalies ---------- */
async function loadInsights() {
  const [an, pat, inc, ov, errs] = await Promise.all([
    api(`/api/v1/anomalies?window=${Math.max(state.window, 20)}`),
    api(`/api/v1/patterns?window=${state.window}&limit=60`),
    api(`/api/v1/incidents?window=${Math.max(state.window, 60)}`),
    api(`/api/v1/overview?window=${state.window}`),
    api(`/api/v1/errors?window=${state.window}&limit=100`).catch(() => ({ groups: [] })),
  ]);
  const anoms = an.anomalies || [];
  const groups = [...(inc.breaching || []), ...(inc.resolved || [])];
  const actions = (ov.actions && ov.actions.actions) || [];
  const failing = (pat.patterns || []).filter(p => p.severity !== "INFO" && p.severity !== "DEBUG");
  const watched = (state.meta && state.meta.config.watchedServices) || [];

  // Likely cause, from evidence: the most frequent error on that service.
  const topErr = svc => (errs.groups || []).filter(g => !svc || g.service === svc).sort((a, b) => b.count - a.count)[0];
  const causeHtml = svc => {
    const e = topErr(svc);
    return e ? `<span class="mono">${esc(e.errorCode)}</span> <span class="faint">×${nf(e.count)}${e.route ? " on " + esc(e.route) : ""}</span>`
             : `<span class="faint">no dominant error</span>`;
  };
  const serviceOf = text => watched.find(w => String(text || "").includes(w)) || watched.find(w => String(text || "").includes(svcShort(w)));
  const actionFor = g => actions.find(a => (a.link && a.link.incident && g.incidentIds && g.incidentIds.includes(a.link.incident))
                                       || (g.service && a.title && a.title.includes(g.service)));
  const impactOf = a => {
    const i = (a && a.impact) || {}, bits = [];
    if (i.revenueAtRiskInr) bits.push(inr(i.revenueAtRiskInr) + " at risk");
    if (i.cloudCostPerHourUsd) bits.push(usd(i.cloudCostPerHourUsd) + "/hr");
    return bits.join(" · ");
  };
  const atRisk = actions.reduce((s, a) => s + ((a.impact && a.impact.revenueAtRiskInr) || 0), 0);
  const top = failing[0];

  $("#insTiles").innerHTML = [
    linkTile({ label: "Anomalies", value: nf(anoms.length), valueColor: anoms.length ? "--warn" : "--ok",
               foot: `beyond ${nf(an.sigma || 3, 1)}σ of their own baseline` }),
    linkTile({ label: "Breaching now", value: nf(inc.stats.breachingNow), valueColor: inc.stats.breachingNow ? "--err" : "--ok",
               foot: `${nf(inc.stats.distinctIncidents)} incident(s) in window`, href: "#incidents" }),
    linkTile({ label: "Revenue at risk", value: atRisk ? inr(atRisk) : "—", valueColor: atRisk ? "--warn" : null,
               foot: "from open actions", href: "#incidents" }),
    linkTile({ label: "Top failure", value: top ? nf(top.count) : "0", valueColor: top ? "--err" : null,
               foot: top ? `${esc(svcShort((top.services || [])[0]))} · ${esc(top.topEvent || "")}` : "nothing repeating",
               href: top ? buildHash("logs", { q: (top.errorCodes || [])[0] || top.topEvent || "" }) : "#logs" }),
  ].join("");

  $("#anomTitle").textContent = `Anomalies`;
  $("#anomChip").innerHTML = anoms.length ? chip(`${nf(anoms.length)} beyond ${nf(an.sigma || 3, 1)}σ`, "--warn") : chip("All series normal", "--ok");
  $("#anomalyList").innerHTML = anoms.length === 0
    ? `<div class="card"><div class="empty">No series is departing from its own baseline.</div></div>`
    : anoms.map(a => {
        const tone = a.severity === "HIGH" ? "--err" : "--warn";
        const svc = serviceOf(a.metric) || serviceOf(a.label);
        const unit = a.unit ? " " + esc(a.unit) : "";
        return `<div class="anom-row" style="--tone:var(${tone})">
          <div class="anom-text">
            <div class="anom-title">${esc(a.label)} ${a.direction === "up" ? "rose" : "fell"} to ${nf(a.current, 1)}${unit}
              ${a.changePct == null ? "" : `<span class="faint">${a.changePct > 0 ? "+" : ""}${nf(a.changePct, 0)}%</span>`}</div>
            <div class="anom-sub"><span class="mono">median ${nf(a.baselineMedian, 1)}${unit}</span> · likely cause: ${causeHtml(svc)}</div>
          </div>
          <div class="anom-z"><div class="anom-zv">${Math.abs(a.zScore) >= 10 ? "≥10" : nf(Math.abs(a.zScore), 1)}σ</div><div class="faint">from baseline</div></div>
        </div>`;
      }).join("");

  $("#incChip").innerHTML = inc.stats.breachingNow ? chip(`${nf(inc.stats.breachingNow)} breaching`, "--err") : chip("Nothing breaching", "--ok");
  $("#findTable").innerHTML = groups.length === 0
    ? `<tbody><tr><td class="empty">No threshold was crossed in this window.</td></tr></tbody>`
    : `<thead><tr><th>Incident</th><th>Severity</th><th>Likely cause</th><th>Impact</th><th>Recommended action</th></tr></thead><tbody>` +
      groups.map(g => {
        const a = actionFor(g);
        const sev = { CRITICAL: "crit", HIGH: "err", MEDIUM: "warn" }[g.severity] || "muted";
        const root = g.correlation && g.correlation.suspectedRootCauseService;
        return `<tr class="clickable" data-inc="${esc(g.primaryIncidentId || "")}">
          <td><div class="find-title">${esc(g.title)}</div>
              <div class="faint pat-sub">${g.status === "BREACHING" ? `<span style="color:var(--err)">● breaching</span>` : "resolved " + ago(g.resolvedAt)} · ${esc(g.summary || "")}</div></td>
          <td><span class="pill ${sev}">${esc(g.severity)}</span></td>
          <td>${root && root !== g.service ? `<span class="mono">${esc(svcShort(root))}</span> <span class="faint">upstream</span><br>` : ""}${causeHtml(g.service)}</td>
          <td>${impactOf(a) || `<span class="faint">—</span>`}</td>
          <td class="find-action">${a ? esc(a.firstStep) : `<span class="faint">Open logs for ${esc(svcShort(g.service))}</span>`}</td>
        </tr>`;
      }).join("") + `</tbody>`;
  $$("#findTable [data-inc]").forEach(tr => tr.addEventListener("click", () => tr.dataset.inc ? showIncident(tr.dataset.inc) : go("incidents")));

  $("#patNote").textContent = `${nf(failing.length)} of ${nf(pat.distinctPatterns)} patterns`;
  const max = Math.max(...failing.map(p => p.sharePct || 0), 0.0001);
  $("#failPatTable").innerHTML = !failing.length
    ? `<tbody><tr><td class="empty">No failure patterns in this window.</td></tr></tbody>`
    : `<thead><tr><th>Pattern</th><th>Severity</th><th>Service</th><th class="right">Count</th><th>Share</th><th class="right">P95</th><th class="right">Last seen</th></tr></thead><tbody>` +
      failing.slice(0, 12).map(p => {
        const sev = (p.severity || "").toUpperCase();
        return `<tr class="clickable" data-q="${esc((p.errorCodes || [])[0] || p.topEvent || "")}" data-svc="${esc((p.services || [])[0] || "")}">
          <td class="pat-cell"><div class="mono pat-name">${esc(p.pattern)}</div><div class="mono faint pat-sub">${esc(p.topEvent || "")}</div></td>
          <td><span class="sev-tag" style="--c:var(${sevVar(sev)})"><span class="dot"></span>${esc(sev)}</span></td>
          <td class="mono">${esc((p.services || []).map(svcShort).join(", "))}</td>
          <td class="num pat-count">${nf(p.count)}</td>
          <td><div class="share"><span class="share-bar"><i style="width:${Math.max(3, (p.sharePct || 0) / max * 100).toFixed(1)}%;background:var(${sev === "ERROR" || sev === "CRITICAL" ? "--err" : "--accent"})"></i></span><span class="faint">${nf(p.sharePct, 1)}%</span></div></td>
          <td class="num">${p.p95LatencyMs == null ? "—" : nf(p.p95LatencyMs) + "ms"}</td>
          <td class="num faint">${ago(p.lastSeen)}</td></tr>`;
      }).join("") + `</tbody>`;
  $$("#failPatTable [data-q]").forEach(tr => tr.addEventListener("click", () =>
    go("logs", { q: tr.dataset.q, service: tr.dataset.svc, severity: "" })));
}

/* ---------- performance ---------- */
async function loadPerformance() {
  const [pts, routes] = await Promise.all([
    api(`/api/v1/metrics/series?window=${state.window}`),
    api(`/api/v1/routes?window=${state.window}&limit=50`).catch(() => null),
  ]);
  const nowMin = Math.floor(Date.now() / 60000);
  const points = complete(pts.points).filter(p => Math.floor(p.ts / 60) < nowMin);
  const vals = k => points.map(p => p[k]).filter(v => v != null);
  const median = a => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
  const p50 = median(vals("p50LatencyMs")), p95 = median(vals("p95LatencyMs"));
  const p99max = vals("p99LatencyMs").length ? Math.max(...vals("p99LatencyMs")) : null;
  const ms = v => v == null ? "—" : nf(v) + "<span class='kunit'>ms</span>";

  $("#perfTiles").innerHTML = [
    linkTile({ label: "P50 latency", value: ms(p50), spark: vals("p50LatencyMs"), sparkColor: "--s1", foot: "median minute", href: "#performance" }),
    linkTile({ label: "P95 latency", value: ms(p95), valueColor: p95 > 500 ? "--warn" : null, spark: vals("p95LatencyMs"), sparkColor: "--s2", foot: "median minute", href: "#performance" }),
    linkTile({ label: "P99 latency", value: ms(p99max), valueColor: p99max > 2000 ? "--err" : null, spark: vals("p99LatencyMs"), sparkColor: "--s3", foot: "worst minute", href: "#performance" }),
    linkTile({ label: "Slow requests", value: routes ? nf(routes.slowRequests) : "—", foot: routes ? `over ${nf(routes.slowThresholdMs)}ms` : "route data unavailable", href: "#logs" }),
  ].join("");

  const labels = points.map(p => hhmm(p.ts));
  const series = [["p50", "p50LatencyMs", "--s1"], ["p95", "p95LatencyMs", "--s2"], ["p99", "p99LatencyMs", "--s3"]];
  upsert("perfLat", "chPerfLat", "line", {
    labels, datasets: series.map(([n, k, c]) => ds(n, points.map(p => p[k]), css(c), false)),
  }, { plugins: { legend: { display: false } }, scales: { y: { grid: { color: css("--border-soft") }, border: { display: false },
       ticks: { color: css("--text-faint"), callback: v => v + "ms", maxTicksLimit: 6 }, beginAtZero: true } } });
  $("#perfLatLegend").innerHTML = series.map(([n, , c]) => `<span><i style="background:var(${c})"></i>${n}</span>`).join("");

  upsert("perfTp", "chPerfTp", "line", {
    labels, datasets: [
      ds("Requests", points.map(p => p.requests || 0), css("--s1"), true),
      ds("Failures (4xx + 5xx)", points.map(p => (p.errors5xx || 0) + (p.errors4xx || 0)), css("--err"), false),
    ],
  }, { plugins: { legend: { display: false } } });
  $("#perfTpLegend").innerHTML = `<span><i style="background:var(--s1)"></i>Requests</span><span><i style="background:var(--err)"></i>Failures</span>`;

  const rows = (routes && routes.routes) || [];
  $("#perfRoutesTitle").textContent = `All routes (${nf(routes ? routes.routeCount : 0)})`;
  const max = Math.max(...rows.map(r => r.p95LatencyMs || 0), 1);
  $("#perfRoutes").innerHTML = !rows.length
    ? `<tbody><tr><td class="empty">No requests in this window.</td></tr></tbody>`
    : `<thead><tr><th>Route</th><th>Service</th><th class="right">Requests</th><th class="right">Error rate</th><th>P95</th></tr></thead><tbody>` +
      rows.map(r => `<tr class="clickable" data-q="${esc(r.route)}">
        <td class="mono">${esc(r.route)}</td>
        <td class="mono faint">${esc(svcShort(r.service))}</td>
        <td class="num">${nf(r.count)}</td>
        <td class="num" style="color:${r.errorRate > 0.02 ? "var(--err)" : "inherit"}">${pct(r.errorRate * 100, 1)}</td>
        <td><div class="share"><span class="share-bar wide"><i style="width:${Math.max(3, (r.p95LatencyMs || 0) / max * 100).toFixed(1)}%;background:var(${(r.p95LatencyMs || 0) > 500 ? "--warn" : "--accent"})"></i></span>
            <span class="num">${r.p95LatencyMs == null ? "—" : nf(r.p95LatencyMs) + "ms"}</span></div></td>
      </tr>`).join("") + `</tbody>`;
  $$("#perfRoutes [data-q]").forEach(tr => tr.addEventListener("click", () => go("logs", { q: tr.dataset.q })));
}

/* ---------- services ---------- */
async function loadServices() {
  const [s, routes, errs] = await Promise.all([
    api(`/api/v1/services?window=${state.window}`),
    api(`/api/v1/routes?window=${state.window}&limit=200`).catch(() => null),
    api(`/api/v1/errors?window=${state.window}&limit=100`).catch(() => ({ groups: [] })),
  ]);
  const list = s.services || [];
  const grade = x => gradeOf({ errorRate: x.errorRate5xx || 0, p95LatencyMs: x.p95LatencyMs });
  const order = { err: 0, warn: 1, ok: 2 };
  list.sort((a, b) => order[grade(a).cls] - order[grade(b).cls] || b.requests - a.requests);
  const attention = list.filter(x => grade(x).cls !== "ok");
  const totReq = list.reduce((a, x) => a + (x.requests || 0), 0);
  const totErr = list.reduce((a, x) => a + (x.errors5xx || 0) + (x.errors4xx || 0), 0);

  $("#svcTiles").innerHTML = [
    linkTile({ label: "Services", value: nf(list.length), foot: "emitting telemetry in this window", href: "#services" }),
    linkTile({ label: "Needing attention", value: nf(attention.length), valueColor: attention.length ? "--warn" : "--ok",
               foot: attention.length ? attention.map(x => svcShort(x.service)).join(", ") : "all healthy", href: "#services" }),
    linkTile({ label: "Total requests", value: nf(totReq), foot: `${nf(totReq / state.window, 1)}/min`, href: "#logs" }),
    linkTile({ label: "Total errors", value: nf(totErr), valueColor: totErr ? "--err" : null, foot: "5xx and 4xx", href: "#logs?severity=WARNING" }),
  ].join("");

  if (!list.length) { $("#serviceCards").innerHTML = `<div class="empty">No services reporting yet.</div>`; return; }
  const byService = {};
  ((routes && routes.routes) || []).forEach(r => (byService[r.service] = byService[r.service] || []).push(r.route));
  const fig = (label, value, color) =>
    `<div class="svc-fig"><div class="svc-fig-k">${label}</div><div class="svc-fig-v"${color ? ` style="color:var(${color})"` : ""}>${value}</div></div>`;

  $("#serviceCards").innerHTML = list.map((x, i) => {
    const g = grade(x);
    const top = (errs.groups || []).filter(e => e.service === x.service).sort((a, b) => b.count - a.count)[0];
    const avail = x.requests ? 100 - (x.errorRate5xx || 0) * 100 : null;
    const rts = (byService[x.service] || []).sort();
    const dot = `var(--s${(i % 4) + 1})`;
    return `<div class="card svc-card">
      <div class="svc-head">
        <span class="svc-dot" style="background:${dot}"></span>
        <div class="svc-title"><div class="mono svc-name2">${esc(svcShort(x.service))}</div>
          <div class="faint">${nf(rts.length)} route${rts.length === 1 ? "" : "s"} · ${nf(x.requestsPerMin, 1)}/min</div></div>
        <span class="pill ${g.cls}"><span class="dot"></span>${g.word}</span>
        <span class="svc-actions">
          <a class="btn" href="${buildHash("logs", { service: x.service })}">View logs →</a>
          ${x.errors5xx ? `<a class="btn danger" href="${buildHash("logs", { service: x.service, severity: "ERROR" })}">View errors →</a>` : ""}
        </span>
      </div>
      <div class="svc-figs">
        ${fig("Requests", nf(x.requests))}
        ${fig("Error rate", pct((x.errorRate5xx || 0) * 100, 1), x.errorRate5xx > 0.01 ? "--err" : null)}
        ${fig("Availability", avail == null ? "—" : pct(avail, 2), avail != null && avail < 99 ? "--err" : null)}
        ${fig("CPU", x.cpuPctMax == null ? "—" : pct(x.cpuPctMax, 0), x.cpuPctMax > 80 ? "--err" : x.cpuPctMax > 60 ? "--warn" : null)}
        ${fig("P50", x.p50LatencyMs == null ? "—" : nf(x.p50LatencyMs) + "ms")}
        ${fig("P95", x.p95LatencyMs == null ? "—" : nf(x.p95LatencyMs) + "ms", x.p95LatencyMs > 500 ? "--warn" : null)}
        ${fig("P99", x.p99LatencyMs == null ? "—" : nf(x.p99LatencyMs) + "ms")}
      </div>
      <div class="svc-foot">
        <span><span class="faint">5xx</span> <b>${nf(x.errors5xx)}</b></span>
        <span><span class="faint">4xx</span> <b>${nf(x.errors4xx)}</b></span>
        ${top ? `<a class="mono faint svc-top" href="${buildHash("logs", { q: top.errorCode, service: x.service })}">top failure: ${esc(top.errorCode)} ×${nf(top.count)} →</a>` : ""}
      </div>
      ${rts.length ? `<div class="svc-routes"><div class="svc-fig-k">Routes</div>${rts.map(r => `<a class="route-chip mono" href="${buildHash("logs", { service: x.service, route: r })}">${esc(r)}</a>`).join("")}</div>` : ""}
    </div>`;
  }).join("");
}

/* ---------- setup ---------- */
/* Firestore does two jobs: user accounts (on whenever the portal runs on
   Google Cloud) and daily history (HISTORY_ENABLED). The row names whichever
   are on, and says so plainly when history cannot reach the database. */
function firestoreRow(c, hist) {
  const accounts = c.accountsBackend === "firestore";
  const history = !!hist.enabled;
  const jobs = [accounts && "Accounts", history && "History"].filter(Boolean);
  const what = "User accounts and daily history";
  if (history && hist.clientError) return ["grid", "info", "Firestore", what, false, "Unreachable", []];
  if (!jobs.length) return ["grid", "info", "Firestore", what, false, c.dataSource === "gcp" ? "Off" : "Local mode", []];
  return ["grid", "info", "Firestore", what, true, jobs.join(" · "), []];
}

async function loadSetup() {
  const m = state.meta || await api("/api/v1/meta");
  const c = m.config, col = m.collectors || {};
  const gcp = c.dataSource === "gcp";
  const logsOn = col.logs && col.logs.running;
  const pill = (ok, on, off) => `<span class="pill ${ok ? "ok" : "muted"}"><span class="dot"></span>${ok ? on : off}</span>`;
  const tile = (icon, tint, label, value, sub) => `<div class="card setup-tile">
      <div class="tile-head">${ico(icon, tint)}<div class="label">${label}</div></div>
      <div class="setup-value">${value}</div><div class="setup-sub">${sub}</div></div>`;

  $("#setupConn").innerHTML = [
    tile("cloud", "accent", "Platform", gcp ? "Google Cloud" : "Local ingest", pill(logsOn, "Connected", "Not connected")),
    tile("tag", "info", "Project", esc(c.activeProject || c.projectId || "local"), `<span class="faint">${gcp ? "Cloud Logging" : "direct ingest"}</span>`),
    tile("pin", "warn", "Region", esc(c.region || "—"), `<span class="faint">application region</span>`),
    tile("layers", "ok", "Application", esc(m.observes || "—"), `<span class="faint">${nf((c.watchedServices || []).length)} services watched</span>`),
  ].join("");

  // What each Google Cloud service does for OpsMind, and whether it is on.
  // Each in-use service names the cost drivers it is charged on, so clicking
  // it can show what that service costs in the modeled spend.
  const svc = [
    ["scroll", "accent", "Cloud Logging", "Logs and errors", gcp ? logsOn : false, gcp ? "Live" : "Local mode", ["logging"]],
    ["cpu", "info", "Cloud Monitoring", "CPU, memory, instances", gcp, gcp ? "Live" : "Local mode", []],
    ["layers", "ok", "Cloud Run", "Hosts the application", true, "In use", ["cpu", "memory", "requests"]],
    ["sparkles", "cost", "Vertex AI · Gemini", "Incident explanations", !!c.aiEnabled, c.aiEnabled ? (c.aiModel || "On") : "Off", []],
    ["coins", "money", "Cloud Billing pricing", "Modeled spend", true, c.pricingVerifiedOn ? "Prices " + c.pricingVerifiedOn : "In use", []],
    ["nodes", "warn", "Resource Manager", "Project discovery", gcp, gcp ? "In use" : "Local mode", []],
    firestoreRow(c, col.history || {}),
  ];
  $("#setupServices").innerHTML = svc.map(([icon, tint, name, what, on, st], i) => `
    <button class="card setup-svc is-click" data-svc="${i}" title="Show what ${esc(name)} costs">
      ${ico(icon, tint)}
      <div class="setup-svc-text"><div class="setup-svc-name">${esc(name)}</div><div class="faint">${esc(what)}</div></div>
      <span class="pill ${on ? "ok" : "muted"}"><span class="dot"></span>${esc(st)}</span>
    </button>`).join("");
  $$("#setupServices [data-svc]").forEach(b =>
    b.addEventListener("click", () => showServiceCost(svc[+b.dataset.svc])));

  const platforms = [
    ["cloud", "accent", "Google Cloud", gcp ? "Connected" : "Local mode", gcp ? "ok" : "info"],
    ["cloud", "info", "Microsoft Azure", "Coming soon", "muted"],
    ["cloud", "warn", "Amazon Web Services", "Coming soon", "muted"],
  ];
  $("#setupPlatforms").innerHTML = platforms.map(([icon, tint, name, state, cls]) => `
    <div class="card setup-svc${cls === "muted" ? " is-off" : ""}">${ico(icon, tint)}
      <div class="setup-svc-text"><div class="setup-svc-name">${name}</div></div>
      <span class="pill ${cls}"><span class="dot"></span>${state}</span></div>`).join("");

  // Google Cloud services OpsMind can use but this deployment does not yet.
  const available = [
    ["scroll", "info", "Cloud Trace", "Request traces across services"],
    ["alert", "err", "Error Reporting", "Grouped exceptions with stack traces"],
    ["coins", "money", "BigQuery billing export", "Billed cost, not modeled"],
    ["bolt", "warn", "Pub/Sub", "Alert delivery to chat and paging"],
    ["layers", "ok", "Cloud SQL", "Database CPU, connections, slow queries"],
    ["nodes", "accent", "Cloud Load Balancing", "Edge latency and status codes"],
  ];
  $("#setupAvailable").innerHTML = available.map(([icon, tint, name, what]) => `
    <div class="card setup-svc is-off">${ico(icon, tint)}
      <div class="setup-svc-text"><div class="setup-svc-name">${name}</div><div class="faint">${what}</div></div>
      <span class="pill muted">Available</span></div>`).join("");
  const used = svc.filter(x => x[4]).length;
  $("#svcUsedChip").innerHTML = chip(`${used} of ${svc.length} active`, "--ok");
  $("#svcAvailChip").innerHTML = chip(`${available.length} not connected`, "--text-faint");
}

/* What one Google Cloud service costs inside the modeled spend. Only the
   drivers OpsMind actually prices are charged to a service; everything else
   is read-only or unmetered here, and says so rather than showing a zero. */
const DRIVER_LABEL = { cpu: "vCPU-seconds", memory: "GiB-seconds", requests: "Requests", logging: "Log ingestion" };
async function showServiceCost([icon, tint, name, what, on, st, drivers]) {
  openDrawer(name, `<span class="pill ${on ? "ok" : "muted"}"><span class="dot"></span>${esc(st)}</span>
    <span class="faint">${esc(what)}</span>`, `<div class="empty"><span class="spin"></span> pricing…</div>`);
  let c;
  try { c = await api(`/api/v1/cost?window=${state.window}`); }
  catch (e) { $("#drawerBody").innerHTML = `<div class="empty">Could not load cost: ${esc(e.message)}</div>`; return; }

  const total = c.usdPerHour || 0;
  const rate = (drivers || []).reduce((s, k) => s + (c.byDriver[k] || 0), 0);
  const share = total ? 100 * rate / total : 0;
  const body = !drivers || !drivers.length
    ? `<div class="ev">
         <div class="ev-head">Modeled cost</div>
         <div class="faint">${esc(name)} is not charged in the modeled spend: it is either free to read or not metered by OpsMind. The modeled rate below belongs entirely to the services that are.</div>
         <div class="fact" style="margin-top:10px"><span class="fk">Project modeled rate</span><span class="fv num">${usd(total)}/hr</span></div>
       </div>`
    : `<div class="ev">
         <div class="ev-head">Modeled cost</div>
         <div class="grid g2">
           <div class="card"><div class="ev-k">THIS SERVICE</div>
             <div class="ev-big">${usd(rate)}<span class="faint">/hr</span></div>
             <div class="faint">${pct(share)} of the project's modeled rate</div></div>
           <div class="card"><div class="ev-k">PROJECTED</div>
             <div class="ev-big">${usd(rate * 24, 3)}<span class="faint">/day</span></div>
             <div class="faint">${usd(rate * 730, 2)}/month at this rate</div></div>
         </div>
       </div>
       <div class="ev">
         <div class="ev-head">Charged on</div>
         <div class="rec-facts">${drivers.map(k =>
           fact(DRIVER_LABEL[k] || k, `<span class="num">${usd(c.byDriver[k] || 0)}/hr</span>`)).join("")}</div>
       </div>
       ${name === "Cloud Run" && (c.byService || []).length ? `<div class="ev">
         <div class="ev-head">By application service</div>
         <div class="rec-facts">${c.byService.map(s =>
           fact(svcShort(s.service), `<span class="num">${usd(s.usdPerHour.total)}/hr</span>`)).join("")}</div>
       </div>` : ""}`;
  $("#drawerBody").innerHTML = body +
    `<div class="faint ev-foot">Modeled: measured usage × published list prices${c.pricingVerifiedOn ? ", verified " + esc(c.pricingVerifiedOn) : ""}. Not an invoice.</div>`;
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
                 backgroundColor: [css("--s1"), css("--s2"), css("--s3"), css("--s4")],
                 borderColor: css("--bg-elev"), borderWidth: 2 }],
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

  // Three states, each shown as itself: not configured, configured but not
  // yet populated, populated. None of them invents a number.
  if (billed.available) {
    const rows = (billed.byService || []).slice(0, 8).map(r =>
      `<tr><td>${esc(r.service)}</td><td class="num">${esc(billed.currency)} ${nf(r.cost, 4)}</td></tr>`).join("");
    $("#billed").innerHTML = `
      <div class="tier auth">authoritative · billed</div>
      <div style="font-size:21px;font-weight:600;margin:6px 0 2px">${esc(billed.currency)} ${nf(billed.totalCost, 4)}</div>
      <div style="font-size:12.5px" class="muted">Google's own billing export, last ${esc(billed.windowDays)} days · ${nf(billed.rowsSeen)} rows</div>
      ${rows ? `<table class="pat-table" style="margin-top:10px"><thead><tr><th>Service</th><th class="right">Billed</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
      <div style="font-size:12px;margin-top:8px" class="faint">${esc(billed.latencyCharacteristics)}</div>`;
    loadReconcile();
    return;
  }
  $("#billed").innerHTML = `
    <div class="pill muted" style="margin-bottom:9px"><span class="dot"></span>not configured</div>
    <div style="font-size:12.5px" class="muted">${esc(billed.reason)}</div>
    <h3 style="margin:14px 0 6px;font-size:12px;letter-spacing:.5px;color:var(--text-dim)">LATENCY</h3>
    <div style="font-size:12.5px" class="muted">${esc(billed.latencyCharacteristics)}</div>
    <h3 style="margin:14px 0 6px;font-size:12px;letter-spacing:.5px;color:var(--text-dim)">RECONCILIATION PLAN</h3>
    <div style="font-size:12.5px" class="muted">${esc(billed.reconciliationPlan)}</div>`;

  renderOptimization(recs);
}

/* The Optimization Center: three numbers that frame the list, a written
   summary, then one card per recommendation. */
function renderOptimization(recs) {
  const list = recs.recommendations || [];
  const high = list.filter(r => r.severity === "HIGH").length;
  const priced = list.filter(r => r.estimatedSavingUsdPerMonth !== null).length;

  $("#optChip").innerHTML = list.length
    ? chip(`${nf(list.length)} open`, high ? "--warn" : "--text-faint")
    : chip("Nothing to change", "--ok");
  $("#optTiles").innerHTML = [
    tile("Calculated savings", usd(recs.totalCalculatedSavingUsdPerMonth, 4) + "<span class='faint' style='font-size:14px'>/mo</span>",
         `${priced} of ${list.length} priced from list prices`, "ok", "coins", "money"),
    tile("Recommendations", nf(list.length), "evidence-backed, in this window", "", "sparkles", "accent"),
    tile("High severity", nf(high), high ? "act on these first" : "nothing urgent", high ? "err" : "", "alert", high ? "err" : "ok"),
  ].join("");

  $("#recHint").textContent = `${recs.note} Google Recommender: ${recs.googleRecommender.reason}`;

  // Live refresh re-renders the list; remember which cards the reader opened.
  const opened = new Set($$("#recs details[open]").map(d => d.dataset.id));
  $("#recs").innerHTML = list.length === 0
    ? `<div class="card"><div class="empty">No recommendation applies to this window.</div></div>`
    // Closed, a card is title, severity and saving; the evidence opens on click.
    : list.map(r => `<details class="card rec-card ${esc(r.severity)}" data-id="${esc(r.id)}"${opened.has(r.id) ? " open" : ""}>
        <summary>
          <div class="rec-head">
            <div class="rec-title">${esc(r.title)}</div>
            <span class="pill ${r.severity === "HIGH" ? "err" : r.severity === "MEDIUM" ? "warn" : "muted"}">${esc(r.severity)}</span>
          </div>
          <div class="rec-save">${r.estimatedSavingUsdPerMonth !== null
            ? `<span class="saving">${usd(r.estimatedSavingUsdPerMonth, 4)}<span class="faint">/mo</span></span>`
            : `<span class="saving none">${esc(r.savingStatus)}</span>`}
            <span class="rec-more faint">Details</span></div>
        </summary>
        <div class="rec-body">${esc(r.recommendation)}</div>
        <div class="rec-body faint">${esc(r.rationale)}</div>
        <div class="rec-facts">
          ${fact(r.observedMetric, `<span class="num">${esc(r.observedValue)}${esc(r.unit)}</span>`)}
          ${fact("Window", esc(r.timeWindow))}
          ${fact("Confidence", esc(r.confidence))}
          ${fact("Source", esc(r.provenance))}
          ${r.savingBasis ? fact("Saving basis", esc(r.savingBasis)) : ""}
        </div>
        <code>${esc(r.suggestedAction)}</code></details>`).join("");

  // The summary is written last and separately: a slow or absent model must
  // never hold up the numbers above it.
  const box = $("#optSummary");
  box.innerHTML = `<div class="opt-sum-head"><h3>Summary</h3><span class="faint"><span class="spin"></span> writing…</span></div>`;
  api(`/api/v1/cost/summary?window=${Math.max(state.window, 15)}`).then(s => {
    box.innerHTML = `<div class="opt-sum-head"><h3>Summary</h3>${s.ai ? AI_TAG : `<span class="pill muted">calculated</span>`}</div>
      <p class="opt-sum-text">${esc(s.summary)}</p>
      ${s.ai ? `<div class="faint opt-sum-foot">Written by ${esc(s.model || "Gemini")} from the modeled spend and recommendations on this page. Numbers above are measured, not generated.</div>`
             : `<div class="faint opt-sum-foot">Gemini is off (${esc(s.reason || "AI disabled")}), so this is calculated directly from the same numbers.</div>`}`;
  }).catch(e => { box.innerHTML = `<div class="opt-sum-head"><h3>Summary</h3></div><div class="faint">Summary unavailable: ${esc(e.message)}</div>`; });
}

/* ---------- alerts ---------- */
async function loadAlerts() {
  const a = await api("/api/v1/alerts");
  const rules = a.rules;
  const breaching = rules.filter(r => r.breachCount).length;
  const off = rules.filter(r => !r.enabled).length;

  $("#alertTiles").innerHTML = [
    tile("Rules", nf(rules.length), "evaluated against the live stream", "", "alert", "accent"),
    tile("Breaching now", nf(breaching), breaching ? "thresholds crossed" : "all within threshold",
         breaching ? "err" : "ok", "bolt", breaching ? "err" : "ok"),
    tile("Disabled", nf(off), off ? "not evaluated" : "every rule is on", off ? "warn" : "", "cpu", off ? "warn" : "ok"),
    tile("Detection latency", `${nf(rules[0] && rules[0].expectedDetectionLatencyS)}s`, "fast path, after the breach", "", "activity", "info"),
  ].join("");
  $("#alertChip").innerHTML = breaching ? chip(`${nf(breaching)} breaching`, "--err") : chip("All within threshold", "--ok");

  $("#alertSources").innerHTML = a.sources.map(s => `
    <div class="card src-card">
      <div class="src-head"><span class="pill ${s.source === "fast-path" ? "info" : "ok"}">${esc(s.source)}</span>
        <span class="faint">latency ${esc(s.latency)}</span></div>
      <div class="muted src-body">${esc(s.description)}</div>
    </div>`).join("") || `<div class="card"><div class="empty">No alert source configured.</div></div>`;

  // One rule per row: what it watches on the left, the editable threshold on
  // the right, breaches underneath where they cannot be missed.
  const catTone = { cost: "cost", business: "warn" };
  $("#alertList").innerHTML = rules.map(r => `
    <div class="card rule${r.breachCount ? " breaching" : ""}${r.enabled ? "" : " is-off"}">
      <div class="rule-main">
        <div class="rule-id">
          <div class="rule-name">${esc(r.name)}</div>
          <div class="rule-tags">
            <span class="pill ${catTone[r.category] || "info"}">${esc(r.category)}</span>
            <span class="pill muted">${esc(r.source)}</span>
            ${r.breachCount
              ? `<span class="pill err"><span class="dot"></span>${nf(r.breachCount)} breaching</span>`
              : r.enabled ? `<span class="pill ok"><span class="dot"></span>within threshold</span>`
                          : `<span class="pill muted"><span class="dot"></span>disabled</span>`}
          </div>
          <div class="muted rule-desc">${esc(r.description)}</div>
          <div class="faint rule-why"><strong>Why this rule:</strong> ${esc(r.rationale)}</div>
        </div>
        <div class="rule-set">
          <div class="rule-facts">
            ${fact("Metric", `<span class="mono">${esc(r.metric)}</span>`)}
            ${fact("Window", `${nf(r.windowMinutes)} min`)}
            ${fact("Severity", esc(r.severity))}
          </div>
          <label class="rule-th">
            <span class="faint">Alert when ${esc(r.comparator === "lt" ? "below" : "above")}</span>
            <span class="rule-input"><input type="number" step="any" value="${r.threshold}" data-th="${esc(r.id)}"><span class="faint">${esc(r.unit)}</span></span>
          </label>
          <div class="rule-btns">
            <button class="btn primary" data-save="${esc(r.id)}">Save</button>
            <button class="btn" data-tog="${esc(r.id)}" data-on="${r.enabled}">${r.enabled ? "Disable" : "Enable"}</button>
          </div>
        </div>
      </div>
      ${r.currentlyBreaching.length ? `<div class="rule-breaches">${r.currentlyBreaching.map(b => `
        <div class="rule-breach"><span class="mono">${esc(b.scope)}</span>
          <span>observed <strong>${nf(b.observed, 2)}${esc(b.unit)}</strong> against ${nf(b.threshold, 2)}${esc(b.unit)}</span></div>`).join("")}</div>` : ""}
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
    linkTile({ label: "Breaching now", value: nf(st.breachingNow), valueColor: st.breachingNow ? "--err" : "--ok",
               foot: "thresholds currently crossed" }),
    linkTile({ label: "Resolved", value: nf(st.resolvedInWindow), foot: "stopped breaching in this window" }),
    linkTile({ label: "Episodes", value: nf(st.totalEpisodes),
               foot: `across ${nf(st.distinctIncidents)} distinct incident${st.distinctIncidents === 1 ? "" : "s"}` }),
    // The backend's "critical" count includes HIGH, so the label says so.
    linkTile({ label: "High or critical", value: nf(st.critical), valueColor: st.critical ? "--err" : null,
               foot: "top two severities" }),
  ].join("");
  $("#incBreachChip").innerHTML = st.breachingNow ? chip(`${nf(st.breachingNow)} need someone`, "--err") : chip("All clear", "--ok");
  $("#incResolvedChip").innerHTML = st.resolvedInWindow ? chip(`${nf(st.resolvedInWindow)} resolved`, "--text-faint") : "";

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
  const rowHtml = (g, i) => {
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
  };
  // Alerts that trace back to the same failing service are one problem: show
  // them under that cause instead of as unrelated rows.
  const byCause = new Map();
  groups.forEach((g, i) => {
    const k = g.rootCause || "__" + i;
    if (!byCause.has(k)) byCause.set(k, []);
    byCause.get(k).push([g, i]);
  });
  box.innerHTML = [...byCause.entries()].map(([cause, members]) => members.length < 2
    ? rowHtml(...members[0])
    : `<div class="cause-group">
        <div class="cause-head">
          <span class="pill err"><span class="dot"></span>Root cause</span>
          <strong>${esc(svcShort(cause))}</strong>
          <span class="faint">${members.length} alerts from one failure. Fix ${esc(svcShort(cause))} first; the rest follow from it.</span>
        </div>
        ${members.map(m => rowHtml(...m)).join("")}
      </div>`).join("");

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

  const up = (cost.deltaUsdPerHour || 0) > 0;
  return `
    <div class="ev-grid">
      <section class="ev ev-wide">
        <div class="ev-head">Impact</div>
        <div class="grid g2">
          <div class="card ev-metric">
            <div class="ev-k">Cloud cost <span class="faint">modeled</span></div>
            <div class="ev-big ${up ? "delta-up" : "delta-down"}">${up ? "+" : ""}${usd(cost.deltaUsdPerHour)}<span class="faint">/hr</span></div>
            <div class="rec-facts">
              ${fact("Baseline", `<span class="num">${usd(cost.baselineUsdPerHour)}/hr</span>`)}
              ${fact("Dominant driver", esc(cost.dominantDriver || "—"))}
              ${fact("Incurred so far", `<span class="num">${usd(cost.incurredUsdSoFar, 5)}</span>`)}
            </div>
          </div>
          <div class="card ev-metric">
            <div class="ev-k">Revenue at risk <span class="faint">measured</span></div>
            <div class="ev-big saving">${inr(biz.revenueAtRiskInr)}</div>
            <div class="rec-facts">
              ${fact("Failed checkouts", `<span class="num">${nf(biz.failedCheckouts)}</span>`)}
              ${fact("Checkout success", `<span class="num">${pct(biz.checkoutSuccessRatePct)}</span>`)}
            </div>
          </div>
        </div>
        <div class="faint ev-foot">${esc(cost.disclaimer)}</div>
      </section>

      <section class="ev">
        <div class="ev-head">Suspected root cause</div>
        ${c.suspectedRootCauseService ? `
          <div class="ev-cause">
            <span class="pill err"><span class="dot"></span>${esc(c.suspectedRootCauseService)}</span>
            ${c.isLikelyDownstream ? `<span class="faint">this service is downstream, not at fault</span>` : ""}
          </div>
          <div class="faint ev-basis">${esc(c.rootCauseBasis || "")}</div>
          ${citations.map(([k, v]) => `
            <div class="ev-cite"><span>${esc(k)}</span><span class="num">${nf(v)}</span></div>
            <div class="bar-track"><div class="bar-fill err" style="width:${100 * v / maxCite}%"></div></div>`).join("")}`
          : `<div class="faint">Not determinable from this window.</div>`}
      </section>

      <section class="ev">
        <div class="ev-head">Top errors</div>
        ${ev.topErrors.length ? `<table class="ev-table"><tbody>${ev.topErrors.map(e => `
          <tr><td><span class="mono ev-code">${esc(e.errorCode)}</span></td>
              <td class="faint">${esc(svcShort(e.service))} ${esc(e.route || "")}</td>
              <td class="num right">${nf(e.count)}</td></tr>`).join("")}</tbody></table>`
          : '<div class="faint">None grouped in this window.</div>'}
      </section>

      <section class="ev">
        <div class="ev-head">Before vs during</div>
        <table class="ev-table"><thead><tr><th>Metric</th><th class="right">Before</th><th class="right">During</th></tr></thead><tbody>
          ${deltaRow("Requests / min", md.requestsPerMin)}
          ${deltaRow("5xx / min", md.errors5xxPerMin)}
          ${deltaRow("p95 latency", md.p95LatencyMs, "ms", 0)}
          ${deltaRow("Retries / min", md.retriesPerMin)}
          ${deltaRow("Payment attempts / min", md.paymentAttemptsPerMin)}
          ${deltaRow("Log volume", md.logMibPerMin, " MiB", 3)}
          ${deltaRow("Instances (max)", md.instanceCountMax, "", 0)}
        </tbody></table>
      </section>

      <section class="ev">
        <div class="ev-head">Sample trace</div>
        ${ev.sampleTrace.length ? `<table class="ev-table ev-trace"><tbody>${ev.sampleTrace.map(e => `
          <tr><td class="faint">${esc(svcShort(e.service))}</td>
              <td class="mono">${esc(e.event)}</td>
              <td class="num right faint">${e.latencyMs != null ? nf(e.latencyMs) + "ms" : ""}</td>
              <td class="num right ${(e.httpStatus || 0) >= 500 ? "bad" : ""}">${e.httpStatus ?? ""}</td></tr>`).join("")}</tbody></table>`
          : '<div class="faint">No trace still buffered for this window.</div>'}
      </section>

      <section class="ev ev-wide">
        <div class="ev-head">Timeline</div>
        <div class="tl">${d.timeline.map(t => `<div class="tl-item ${esc(t.kind)}">
          <div class="t">${hms(t.ts)} · ${esc(t.kind)}</div>
          <div class="tl-text">${esc(t.text)}</div></div>`).join("")}</div>
      </section>

      <section class="ev ev-wide">
        <div class="ev-head">Explanation</div>
        <div class="ev-explain">
          <button class="btn primary" data-explain="${esc(id)}">Explain this incident</button>
          <span class="faint">Narrated from the evidence above — no new numbers.</span>
        </div>
        <div class="explain-out"></div>
      </section>
    </div>`;
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
        <div class="explain-tags">
          ${r.provider === "vertex-ai" ? AI_TAG : `<span class="pill muted">calculated · deterministic</span>`}
          ${r.model ? `<span class="pill muted">${esc(r.model)}</span>` : ""}
          <span class="pill ${g.grounded ? "ok" : "warn"}"><span class="dot"></span>${g.grounded ? "fully grounded" : g.unsupportedNumbers.length + " unverified number(s)"}</span>
          <span class="pill muted">${g.checkedNumbers} numbers checked</span>
          ${r.cached ? `<span class="pill muted">cached</span>` : ""}
        </div>
        ${r.fallbackUsed ? `<div class="faint" style="font-size:11.5px;margin-bottom:8px">Deterministic narrative (${esc(r.fallbackReason)}). Built from the same evidence bundle.</div>` : ""}
        ${narrativeHtml(r.narrative)}
        <div class="faint" style="font-size:11px">${esc(g.method)}</div>`;
    } catch (e) {
      out.innerHTML = `<div class="faint">Explanation failed: ${esc(e.message)}</div>`;
    }
    btn.disabled = false;
  });
}

/* The narrative arrives as plain text under fixed upper-case headings
   (WHAT HAPPENED, EVIDENCE, ...). Render each as a labelled block in body
   type instead of one monospace slab. */
function narrativeHtml(text) {
  const blocks = [];
  String(text || "").split(/\r?\n/).forEach(line => {
    const t = line.trim().replace(/^#+\s*|\*\*/g, "");
    if (!t) return;
    if (/^[A-Z][A-Z \/&-]{2,}:?$/.test(t)) { blocks.push({ head: t.replace(/:$/, ""), body: [] }); return; }
    if (!blocks.length) blocks.push({ head: "", body: [] });
    blocks[blocks.length - 1].body.push(t);
  });
  return `<div class="narr">${blocks.map(b => `<div class="narr-blk">
    ${b.head ? `<div class="ev-k">${esc(b.head.toLowerCase())}</div>` : ""}
    <p>${esc(b.body.join(" "))}</p></div>`).join("")}</div>`;
}

/* ---------- dropdowns ----------
   Every dropdown is a pill (a <label>) around a native <select>. The select
   stays the source of truth -- code sets .value, rebuilds options and listens
   for "change" exactly as before -- but its OS-drawn list is replaced by a
   themed menu: same fonts and sizing in both themes, full option text, and it
   is built when opened, so a live refresh rebuilding the options cannot close
   it under the reader. */
const dd = { open: null };
function ddLabel(sel) {
  const o = sel.options[sel.selectedIndex];
  const v = sel.closest(".dd").querySelector(".dd-value");
  v.textContent = o ? o.text : "";
  v.title = v.textContent;
}
function ddClose() {
  if (!dd.open) return;
  dd.open.pill.setAttribute("aria-expanded", "false");
  dd.open.menu.remove();
  dd.open = null;
}
function ddRender(sel, menu) {
  menu.innerHTML = [...sel.options].map((o, i) => /^─+$/.test(o.text)
    ? `<div class="dd-sep" role="separator"></div>`
    : `<button type="button" role="option" class="dd-opt${i === sel.selectedIndex ? " on" : ""}" data-i="${i}"
         aria-selected="${i === sel.selectedIndex}"${o.disabled ? " disabled" : ""}>${esc(o.text)}</button>`).join("");
}
function ddOpen(pill, sel) {
  ddClose();
  const menu = document.createElement("div");
  menu.className = "dd-menu"; menu.setAttribute("role", "listbox");
  ddRender(sel, menu);
  document.body.appendChild(menu);
  // Fixed to the viewport so no scrolling or clipping container hides it.
  const r = pill.getBoundingClientRect();
  menu.style.minWidth = r.width + "px";
  menu.style.top = (r.bottom + 6) + "px";
  const left = Math.min(r.left, innerWidth - menu.offsetWidth - 8);
  menu.style.left = Math.max(8, left) + "px";
  menu.addEventListener("click", e => {
    const b = e.target.closest(".dd-opt");
    if (!b || b.disabled) return;
    const i = +b.dataset.i;
    ddClose(); pill.focus();
    if (i !== sel.selectedIndex) {
      sel.selectedIndex = i;
      ddLabel(sel);
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
  pill.setAttribute("aria-expanded", "true");
  dd.open = { pill, sel, menu };
  (menu.querySelector(".dd-opt.on") || menu.querySelector(".dd-opt:not([disabled])"))?.focus();
}
function enhanceSelect(sel) {
  const pill = sel.closest(".tb-project, .tb-window, .fsel");
  if (!pill || pill.classList.contains("dd")) return;
  pill.classList.add("dd");
  pill.tabIndex = 0;
  pill.setAttribute("role", "combobox");
  pill.setAttribute("aria-haspopup", "listbox");
  pill.setAttribute("aria-expanded", "false");
  if (sel.getAttribute("aria-label")) pill.setAttribute("aria-label", sel.getAttribute("aria-label"));
  const v = document.createElement("span");
  v.className = "dd-value";
  sel.after(v);
  sel.tabIndex = -1;
  // Keep the visible text in step with every way the value can change:
  // assignment, rebuilt options, or a user choice.
  const proto = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  Object.defineProperty(sel, "value", {
    get() { return proto.get.call(sel); },
    set(x) { proto.set.call(sel, x); ddLabel(sel); },
  });
  new MutationObserver(() => {
    ddLabel(sel);
    if (dd.open && dd.open.sel === sel) ddRender(sel, dd.open.menu);
  }).observe(sel, { childList: true, subtree: true, attributes: true });
  sel.addEventListener("change", () => ddLabel(sel));
  pill.addEventListener("click", e => {
    e.preventDefault();
    if (sel.disabled) return;
    if (dd.open && dd.open.pill === pill) ddClose(); else ddOpen(pill, sel);
  });
  pill.addEventListener("keydown", e => {
    if (["Enter", " ", "ArrowDown"].includes(e.key) && !dd.open) { e.preventDefault(); ddOpen(pill, sel); }
  });
  ddLabel(sel);
}
document.addEventListener("click", e => {
  if (dd.open && !dd.open.pill.contains(e.target) && !dd.open.menu.contains(e.target)) ddClose();
});
document.addEventListener("keydown", e => {
  if (!dd.open) return;
  const opts = [...dd.open.menu.querySelectorAll(".dd-opt:not([disabled])")];
  const i = opts.indexOf(document.activeElement);
  if (e.key === "Escape") { e.stopPropagation(); const p = dd.open.pill; ddClose(); p.focus(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); opts[Math.min(opts.length - 1, i + 1)]?.focus(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); opts[Math.max(0, i - 1)]?.focus(); }
  else if (e.key === "Tab") ddClose();
}, true);
addEventListener("resize", ddClose);
document.addEventListener("scroll", e => { if (dd.open && !dd.open.menu.contains(e.target)) ddClose(); }, true);
$$(".tb-project select, .tb-window select, .fsel select").forEach(enhanceSelect);

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
  if (!notifOpen) return;
  renderNotifList();
  state.notifSeen = Math.max(state.notifSeen, ...state.notifs.map(x => x.ts), Date.now() / 1000);
  store_.set(NOTIF_SEEN, String(state.notifSeen));
  paintBell();
});
document.addEventListener("click", e => {
  if (!notifOpen) return;
  if (e.target.closest("#notifPanel") || e.target.closest("#bellBtn")) return;
  notifOpen = false; $("#notifPanel").hidden = true;
});

/* ---------- notifications ----------
   "Unread" means an incident opened after you last opened the bell, kept per
   browser. Pop-ups are deliberately quiet: only for incidents opening, never
   for resolutions; one card at a time, several at once become one line; the
   same problem pops up at most once in ten minutes; no sound; gone after ten
   seconds; and they can be switched off in the bell panel. */
const NOTIF_SEEN = "opsmind.notifSeen", NOTIF_POPUPS = "opsmind.notifPopups";
const store_ = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
state.notifs = [];
state.notifSeen = +store_.get(NOTIF_SEEN, String(Date.now() / 1000));
const popupsOn = () => store_.get(NOTIF_POPUPS, "1") === "1";

function setNotifs(items) {
  const byKey = new Map(state.notifs.concat(items).map(x => [x.kind + x.incidentId, x]));
  state.notifs = [...byKey.values()].sort((a, b) => b.ts - a.ts).slice(0, 40);
  paintBell();
  if (notifOpen) renderNotifList();
}
function paintBell() {
  const unread = state.notifs.filter(x => x.kind === "opened" && x.ts > state.notifSeen).length;
  const b = $("#bellBadge");
  b.hidden = !unread;
  b.textContent = unread > 99 ? "99+" : unread;
  // A background tab shows the count in its title, which is visible without
  // switching to it.
  document.title = (unread ? `(${unread}) ` : "") + "OpsMind";
}
function renderNotifList() {
  $("#notifNote").textContent = "Incidents opening and resolving";
  $("#notifPopups").checked = popupsOn();
  $("#notifList").innerHTML = state.notifs.length === 0
    ? `<div class="empty">Nothing has opened or resolved recently.</div>`
    : state.notifs.map(x => `
      <div class="notif-item${x.kind === "opened" && x.ts > state.notifSeen ? " unread" : ""}" data-inc="${esc(x.incidentId)}">
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
    showIncident(el.dataset.inc);
  }));
}
async function loadNotifications() {
  const n = await api("/api/v1/notifications?limit=25");
  setNotifs(n.notifications);
}
$("#notifPopups").addEventListener("change", e => store_.set(NOTIF_POPUPS, e.target.checked ? "1" : "0"));

const popped = new Map();   // incident scope -> when it last popped up
let popTimer = null;
function onNotify(items) {
  setNotifs(items);
  const fresh = items.filter(x => x.kind === "opened"
    && Date.now() - (popped.get(x.service || x.title) || 0) > 10 * 60 * 1000);
  if (!fresh.length || !popupsOn()) return;
  fresh.forEach(x => popped.set(x.service || x.title, Date.now()));
  const top = fresh.slice().sort((a, b) => (SEV_RANK[b.severity] || 0) - (SEV_RANK[a.severity] || 0))[0];
  $("#alertPopTitle").textContent = fresh.length === 1 ? top.title : `${fresh.length} new incidents`;
  $("#alertPopSub").textContent = fresh.length === 1 ? top.summary : `Worst: ${top.title}`;
  const pop = $("#alertPop");
  pop.dataset.inc = fresh.length === 1 ? top.incidentId : "";
  pop.className = "alert-pop " + (SEV_RANK[top.severity] >= 3 ? "high" : "medium");
  pop.hidden = false;
  armPop();
}
const SEV_RANK = { LOW: 1, MEDIUM: 2, HIGH: 3, CRITICAL: 4 };
function armPop() { clearTimeout(popTimer); popTimer = setTimeout(() => { $("#alertPop").hidden = true; }, 10000); }
$("#alertPop").addEventListener("mouseenter", () => clearTimeout(popTimer));
$("#alertPop").addEventListener("mouseleave", armPop);
$("#alertPopClose").addEventListener("click", () => { $("#alertPop").hidden = true; });
$("#alertPopView").addEventListener("click", () => {
  const id = $("#alertPop").dataset.inc;
  $("#alertPop").hidden = true;
  id ? showIncident(id) : go("incidents");
});

/* ---------- orchestration ---------- */
async function refresh() {
  try {
    await loadMeta();
    if (!$("#logSvc").options.length || $("#logSvc").options.length === 1) {
      const s = await api("/api/v1/services?window=60");
      const cur = $("#logSvc").value;
      $("#logSvc").innerHTML = `<option value="">All</option>` +
        s.services.map(x => `<option value="${esc(x.service)}">${esc(svcShort(x.service))}</option>`).join("");
      setSel($("#logSvc"), cur);
    }
    if (state.view === "overview") await loadOverview();
    else if (state.view === "logs") { await loadLogsInitial(); await loadErrors(); }
    else if (state.view === "insights") await loadInsights();
    else if (state.view === "services") await loadServices();
    else if (state.view === "performance") await loadPerformance();
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
    await loadNotifications();
  } catch (e) { console.error("refresh failed", e); }
}

(async function init() {
  applyChartTheme();
  loadProjectPicker();
  decorateNav();
  decorateHeadings();
  startStream();
  applyRoute();          // reads the hash, sets the view, starts the timer
})();
})();



/* Show who is signed in, and give the dashboard a route to /account. The
   page is reachable only with a session, so a failure here means signed out
   and the link still works -- it just shows no initials. */
(async function accountChip() {
  const el = document.getElementById("acctInitials");
  if (!el) return;
  try {
    const me = await (await fetch("/api/v1/auth/me")).json();
    if (me && me.user) {
      el.textContent = me.user.initials || "";
      const btn = document.getElementById("acctBtn");
      if (btn && me.user.email) btn.title = me.user.email + " · account and sign out";
    }
  } catch (e) { /* the link stands on its own */ }
})();

/* ---------- product guide ----------
   Answers questions about OpsMind itself, from a manifest generated by the
   running application. It is not a general assistant and does not read your
   telemetry -- it describes the product, which is a question judges and new
   users ask far more often than anything else. */
(function guide() {
  const fab = document.getElementById("guideFab");
  const panel = document.getElementById("guidePanel");
  if (!fab || !panel) return;
  const body = document.getElementById("guideBody");
  const input = document.getElementById("guideInput");
  const sendBtn = document.getElementById("guideSend");
  let greeted = false;

  /* Ask is disabled while the box is empty. It used to accept the click and
     silently return, which reads as a broken button -- the placeholder is a
     real question, so an empty box looks like a filled one. */
  const syncSend = () => { sendBtn.disabled = !input.value.trim(); };

  const esc2 = (t) => String(t ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (t) => esc2(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

  function say(who, text) {
    const el = document.createElement("div");
    el.className = "g-msg " + who;
    el.innerHTML = who === "me" ? esc2(text) : md(text);
    body.appendChild(el);
    body.scrollTop = body.scrollHeight;
    return el;
  }

  async function greet() {
    if (greeted) return;
    greeted = true;
    try {
      const m = await (await fetch("/api/v1/guide/manifest")).json();
      say("them", "Ask me what OpsMind does, what any view shows, or which "
                + "Google Cloud services it uses. I answer from this instance, "
                + "so what I say reflects how it is configured right now.");
      const wrap = document.createElement("div");
      wrap.innerHTML = (m.suggestions || []).slice(0, 5)
        .map(q => `<span class="g-chip">${esc2(q)}</span>`).join("");
      wrap.querySelectorAll(".g-chip").forEach(c =>
        c.addEventListener("click", () => { input.value = c.textContent; syncSend(); send(); }));
      body.appendChild(wrap);
    } catch (e) { say("them", "Ask me about OpsMind."); }
  }

  async function send() {
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    syncSend();
    say("me", q);
    const pending = say("them", "…");
    try {
      const r = await fetch("/api/v1/guide", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const d = await r.json();
      pending.innerHTML = md(d.answer);
      const p = document.getElementById("guideProvider");
      if (p) p.textContent = d.provider === "vertex-ai"
        ? `${d.model} · grounded in this instance`
        : "answered from this instance's own manifest";
    } catch (e) { pending.textContent = "Could not reach the guide."; }
  }

  fab.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) { greet(); input.focus(); }
  });
  document.getElementById("guideClose").addEventListener("click", () => panel.hidden = true);
  sendBtn.addEventListener("click", send);
  input.addEventListener("input", syncSend);
  input.addEventListener("keydown", e => { if (e.key === "Enter") send(); });
  syncSend();
})();
