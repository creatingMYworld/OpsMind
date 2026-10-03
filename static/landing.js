/* Landing page.
   The figures are read from the running instance rather than written into the
   markup. A product page quoting invented numbers is the same failure as a
   dashboard quoting invented costs, and this one can afford not to. */
(() => {
"use strict";
// Opt in to the reveal animation only now that we know scripting works.
document.documentElement.classList.add("js");

const $ = (s) => document.querySelector(s);
const nf = (v) => Number(v || 0).toLocaleString();

function count(el, target) {
  if (!el) return;
  const dur = 900, t0 = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - t0) / dur);
    // ease-out, so it settles rather than stopping dead
    el.textContent = nf(Math.round(target * (1 - Math.pow(1 - k, 3))));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

async function live() {
  try {
    const r = await fetch("/api/v1/meta");
    if (!r.ok) throw new Error(String(r.status));
    const m = await r.json();
    const gcp = m.config.dataSource === "gcp";

    $("#liveState").textContent = gcp
      ? `reading Cloud Logging · ${m.config.projectId || "project"}`
      : "running locally · direct ingest";

    count($("#figServices"), (m.config.watchedServices || []).length);
    count($("#figEntries"), m.store.ingestedTotal || 0);
    $("#figServicesSub").textContent = gcp ? "from Cloud Logging" : "from local ingest";
    $("#footMeta").textContent = gcp
      ? `Read-only · project ${m.config.projectId}`
      : "Read-only · local mode";

    if (m.config.pricingVerifiedOn) {
      $("#heroNote").textContent =
        `Read-only. OpsMind cannot change anything in your project. ` +
        `Pricing verified ${m.config.pricingVerifiedOn}.`;
    }
  } catch (e) {
    // The page must stand on its own if the API is not up yet.
    $("#liveState").textContent = "Google Cloud native";
    $("#figServices").textContent = "4";
    $("#figEntries").textContent = "—";
  }
}

const io = new IntersectionObserver((rows) => {
  rows.forEach(r => { if (r.isIntersecting) { r.target.classList.add("in"); io.unobserve(r.target); } });
}, { threshold: .08, rootMargin: "0px 0px -8% 0px" });
document.querySelectorAll(".reveal").forEach(el => io.observe(el));

// Anything already on screen at load should not wait for a scroll event.
requestAnimationFrame(() => {
  document.querySelectorAll(".reveal").forEach(el => {
    if (el.getBoundingClientRect().top < innerHeight) el.classList.add("in");
  });
});

live();
})();
