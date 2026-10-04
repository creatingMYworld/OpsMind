/* Marketing pages: reveal-on-scroll, plus the live project name in the hero
   note when the API is reachable. Every page works without this script. */
(() => {
"use strict";
document.documentElement.classList.add("js");

const items = document.querySelectorAll(".reveal");
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver((rows) => {
    rows.forEach((r) => {
      if (r.isIntersecting) { r.target.classList.add("in"); io.unobserve(r.target); }
    });
  }, { threshold: 0.08, rootMargin: "0px 0px -8% 0px" });
  items.forEach((el) => io.observe(el));
} else {
  items.forEach((el) => el.classList.add("in"));
}

/* ---- dashboard charts ----------------------------------------------------
   Same options as chartDefaults(), ds() and drawTraffic() in app.js, with the
   dashboard's colour tokens written in. The marketing page does not load
   styles.css, so the values cannot be read from CSS variables here. */
const C = {
  border: "#232833", borderSoft: "#1b1f28", elev2: "#171b23",
  text: "#e8ebf1", dim: "#a3abba", faint: "#7b8394",
  accent: "#6c9cff", ok: "#34d399", warn: "#fbbf24", err: "#f87171"
};
const chartDefaults = () => ({
  responsive: true, maintainAspectRatio: false, animation: { duration: 220 },
  interaction: { mode: "index", intersect: false },
  plugins: {
    legend: { labels: { color: C.dim, boxWidth: 10, boxHeight: 10, font: { size: 11 }, usePointStyle: true } },
    tooltip: { backgroundColor: C.elev2, borderColor: C.border, borderWidth: 1,
               titleColor: C.text, bodyColor: C.dim, padding: 9, displayColors: true }
  },
  scales: {
    x: { grid: { color: C.borderSoft, drawBorder: false }, ticks: { color: C.faint, font: { size: 10 }, maxRotation: 0, autoSkipPadding: 18 } },
    y: { grid: { color: C.borderSoft, drawBorder: false }, ticks: { color: C.faint, font: { size: 10 } }, beginAtZero: true }
  }
});
const ds = (label, data, color, fill) => ({
  label, data, borderColor: color, backgroundColor: fill ? color + "33" : color,
  fill: !!fill, tension: .3, borderWidth: 2, pointRadius: 0, pointHoverRadius: 3
});

// Sample series. Shaped like a real incident: a 5xx burst with latency
// climbing alongside it, then recovery.
const hours = ["10:00","10:30","11:00","11:30","12:00","12:30","13:00","13:30","14:00","14:30","15:00","15:30","16:00"];
const ok5  = [410,420,430,445,460,470,455,380,350,420,470,480,490];
const c4xx = [ 14, 16, 15, 17, 18, 16, 19, 22, 24, 20, 17, 16, 15];
const c5xx = [  3,  4,  3,  5,  6,  9, 22, 38, 41, 18,  6,  4,  3];
const p95  = [180,185,182,190,196,240,410,690,720,380,210,195,188];

function charts() {
  if (typeof Chart === "undefined") return;   // CDN blocked: cards still render
  const hero = document.getElementById("chHeroTraffic");
  if (hero) {
    // Error rate with the alert threshold: one line, one question. The line
    // turns red only where it crosses the threshold, so the incident is the
    // first thing the eye lands on.
    const rate = c5xx.map((v, i) => +(v / (ok5[i] + c4xx[i] + v) * 100).toFixed(1));
    const THRESHOLD = 5;
    const o = chartDefaults();
    o.plugins.legend = { display: false };
    o.scales.y.max = 12;
    o.scales.y.ticks.callback = (v) => v + "%";
    o.scales.y.ticks.stepSize = 4;
    const ctx = hero.getContext("2d");
    const grad = ctx.createLinearGradient(0, 0, 0, hero.parentElement.clientHeight);
    grad.addColorStop(0, C.accent + "40");
    grad.addColorStop(1, C.accent + "00");
    new Chart(ctx, { type: "line", options: o, data: {
      labels: hours,
      datasets: [
        { ...ds("5xx error rate", rate, C.accent, true), backgroundColor: grad,
          segment: { borderColor: (c) => (c.p0.parsed.y > THRESHOLD || c.p1.parsed.y > THRESHOLD) ? C.err : C.accent } },
        { label: "Threshold", data: hours.map(() => THRESHOLD), borderColor: C.err + "99",
          borderDash: [4, 4], borderWidth: 1, pointRadius: 0, fill: false }
      ]
    }});
  }
  const ws = document.getElementById("chWorkspace");
  if (ws) {
    // Where the errors come from, not when: a different question from the
    // hero's line, so a different shape. Sorted, and only the worst service is
    // red, so the answer is readable without the axis.
    const services = [
      ["checkout-api", 1480], ["payments", 612], ["auth", 214],
      ["catalog", 96], ["orders", 71], ["search", 38]
    ];
    const o = chartDefaults();
    o.indexAxis = "y";
    o.plugins.legend = { display: false };
    o.scales.x.ticks.callback = (v) => Number(v).toLocaleString();
    o.scales.y = { grid: { display: false }, ticks: { color: C.dim, font: { size: 11 } } };
    new Chart(ws.getContext("2d"), { type: "bar", options: o, data: {
      labels: services.map((r) => r[0]),
      datasets: [{
        label: "Errors (24h)",
        data: services.map((r) => r[1]),
        backgroundColor: services.map((_, i) => (i === 0 ? C.err + "dd" : C.accent + "99")),
        borderRadius: 3, barThickness: 16
      }]
    }});
  }
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", charts);
else charts();

const note = document.getElementById("liveState");
if (note) {
  fetch("/api/v1/meta")
    .then((r) => (r.ok ? r.json() : null))
    .then((m) => {
      const project = m && m.config && m.config.dataSource === "gcp" && m.config.projectId;
      if (project) note.textContent = `Read-only access · reading ${project}`;
    })
    .catch(() => { /* keep the static note */ });
}
})();
