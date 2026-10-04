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
