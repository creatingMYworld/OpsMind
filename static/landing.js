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
  border: "#233049", borderSoft: "#1b2435", elev2: "#18202f",
  text: "#e6edf7", dim: "#93a4bf", faint: "#64748b",
  accent: "#38bdf8", ok: "#34d399", warn: "#fbbf24", err: "#f87171"
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
    const o = chartDefaults();
    o.scales = {
      x: { stacked: true, grid: { color: C.borderSoft }, ticks: { color: C.faint, font: { size: 10 }, autoSkipPadding: 18 } },
      y: { stacked: true, grid: { color: C.borderSoft }, ticks: { color: C.faint, font: { size: 10 } }, beginAtZero: true },
      y1: { position: "right", grid: { display: false }, ticks: { color: C.accent, font: { size: 10 } }, beginAtZero: true }
    };
    new Chart(hero.getContext("2d"), { type: "bar", options: o, data: {
      labels: hours,
      datasets: [
        { label: "2xx/3xx", data: ok5, backgroundColor: C.ok + "cc", stack: "s", borderRadius: 2 },
        { label: "4xx client", data: c4xx, backgroundColor: C.warn + "cc", stack: "s", borderRadius: 2 },
        { label: "5xx server", data: c5xx, backgroundColor: C.err + "dd", stack: "s", borderRadius: 2 },
        { label: "p95 latency (ms)", data: p95, type: "line", yAxisID: "y1",
          borderColor: C.accent, borderWidth: 2, pointRadius: 0, tension: .3, fill: false }
      ]
    }});
  }
  const ws = document.getElementById("chWorkspace");
  if (ws) {
    const labels = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0") + ":00");
    const req = labels.map((_, i) => Math.round(1800 + 420 * i + 900 * Math.sin(i / 3.2)));
    const err = labels.map((_, i) => (i >= 11 && i <= 13 ? [620, 1480, 540][i - 11] : 90 + (i % 4) * 22));
    new Chart(ws.getContext("2d"), { type: "line", options: chartDefaults(), data: {
      labels,
      datasets: [ds("Requests", req, C.accent, true), ds("Errors", err, C.err, false)]
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
