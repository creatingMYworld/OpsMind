/* Sign in, sign up and the account page. The server sets and reads the
   session cookie; this script only submits forms and fills in the page. */
(() => {
"use strict";

// Only a path on this site, never somewhere else. The server applies the same
// rule; this keeps a crafted ?next= from bouncing a person off-site.
const safeNext = (v) =>
  (v && v.startsWith("/") && !v.startsWith("//") && !v.startsWith("/\\")) ? v : "/";
const params = new URLSearchParams(location.search);
let next = safeNext(params.get("next"));
// Start links to /app#setup. A redirect keeps the #setup on this page's URL
// but the server never sees it, so carry it on from here.
if (location.hash && !next.includes("#")) next += location.hash;

// Switching between sign in and sign up keeps where the person was going.
document.querySelectorAll("a[data-keep-next]").forEach((a) => {
  if (next !== "/") a.href += "?next=" + encodeURIComponent(next);
});

/* ---- show / hide password ----------------------------------------------
   Every password field on these pages, including the access code, gets a
   toggle. A reset (after changing the password) hides them again. */
document.querySelectorAll('input[type="password"]').forEach((input) => {
  const wrap = document.createElement("span");
  wrap.className = "pw-wrap";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pw-toggle";
  const set = (show) => {
    input.type = show ? "text" : "password";
    btn.textContent = show ? "Hide" : "Show";
    btn.setAttribute("aria-pressed", String(show));
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
  };
  set(false);
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    set(input.type === "password");
    input.focus();
  });
  wrap.appendChild(btn);
  if (input.form) input.form.addEventListener("reset", () => set(false));
});

/* ---- background: a faint log stream either side of the card -------------
   Decoration only: aria-hidden, sample data, the same services and events
   the product page shows. Each column holds its lines twice so the scroll
   loops without a seam. Without this script the page is simply plain. */
const bg = document.querySelector(".auth-bg");
if (bg) {
  const EVENTS = [
    ["INFO", "api-gateway", "GET /api/products 200 · 34ms"],
    ["INFO", "catalog", "product_viewed sku=SKU-1042"],
    ["INFO", "api-gateway", "GET /api/cart 200 · 18ms"],
    ["INFO", "orders", "order_created id=ORD-58213 items=2"],
    ["INFO", "payments", "payment_authorized amount=₹2,499"],
    ["INFO", "catalog", "cache_hit ratio=0.94"],
    ["WARN", "payments", "retry_scheduled attempt=2 backoff=800ms"],
    ["INFO", "orders", "inventory_reserved sku=SKU-2210"],
    ["INFO", "api-gateway", "POST /api/checkout 201 · 412ms"],
    ["ERROR", "payments", "payment_timeout upstream=psp after 5000ms"],
    ["INFO", "catalog", "search q=\"headphones\" hits=38 · 22ms"],
    ["INFO", "orders", "order_confirmed id=ORD-58209"],
    ["WARN", "api-gateway", "slow_request GET /api/products · 1.8s"],
    ["INFO", "payments", "heartbeat cpu=23% mem=184MB"],
    ["ERROR", "api-gateway", "POST /api/checkout 502 · 5012ms"],
    ["INFO", "catalog", "heartbeat cpu=11% mem=142MB"]
  ];
  const pad = (n, w) => String(n).padStart(w, "0");
  const stamp = (t) => {
    const d = new Date(t);
    return `${pad(d.getHours(), 2)}:${pad(d.getMinutes(), 2)}:${pad(d.getSeconds(), 2)}.${pad(d.getMilliseconds(), 3)}`;
  };
  const column = (offset, count) => {
    const col = document.createElement("div");
    col.className = "auth-bg-col";
    const track = document.createElement("div");
    track.className = "auth-bg-track";
    let t = Date.now() - count * 1400;
    const lines = [];
    for (let i = 0; i < count; i++) {
      const [lvl, svc, msg] = EVENTS[(i * 7 + offset) % EVENTS.length];
      t += 300 + ((i * 577 + offset * 131) % 2100);
      const row = document.createElement("div");
      const parts = [
        ["t", stamp(t)],
        [lvl === "ERROR" ? "lv e" : lvl === "WARN" ? "lv w" : "lv", lvl.padEnd(5)],
        ["s", svc.padEnd(12)],
        ["", msg]
      ];
      parts.forEach(([cls, text]) => {
        const span = document.createElement("span");
        if (cls) span.className = cls;
        span.textContent = text;
        row.appendChild(span);
      });
      lines.push(row);
    }
    lines.forEach((r) => track.appendChild(r));
    lines.forEach((r) => track.appendChild(r.cloneNode(true)));
    col.appendChild(track);
    return col;
  };
  bg.appendChild(column(0, 36));
  bg.appendChild(column(5, 36));
}

async function send(method, url, body) {
  const r = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty or non-JSON body */ }
  if (!r.ok) {
    const msg = typeof data.detail === "string" ? data.detail
      : typeof data.error === "string" ? data.error
      : "Something went wrong. Try again.";
    throw new Error(msg);
  }
  return data;
}

// Native validation runs first (required, type=email, minlength), so the
// handler only sees a form the browser already accepts.
function bindForm(form, handler) {
  if (!form) return;
  const err = form.querySelector(".form-error");
  const ok = form.querySelector(".form-ok");
  const btn = form.querySelector("button[type=submit]");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    if (ok) ok.hidden = true;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = btn.dataset.busy || label;
    try {
      await handler(Object.fromEntries(new FormData(form)), form);
      if (ok) ok.hidden = false;
    } catch (x) {
      err.textContent = x.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
}

/* ---- sign in / sign up ------------------------------------------------- */
bindForm(document.getElementById("signinForm"), async (f) => {
  await send("POST", "/api/v1/auth/signin", { email: f.email, password: f.password });
  location.assign(next);
});

bindForm(document.getElementById("signupForm"), async (f) => {
  await send("POST", "/api/v1/auth/signup", {
    name: f.name, email: f.email, password: f.password, accessCode: f.accessCode
  });
  location.assign(next);
});

/* ---- account page ------------------------------------------------------ */
const page = document.getElementById("account");
if (!page) return;

const $ = (id) => document.getElementById(id);
const day = (ts) => ts ? new Date(ts * 1000).toLocaleDateString(undefined,
  { year: "numeric", month: "short", day: "numeric" }) : "—";
const moment = (ts) => ts ? new Date(ts * 1000).toLocaleString(undefined,
  { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

function render(user) {
  $("acctAvatar").textContent = user.initials;
  $("acctName").textContent = user.name || user.email;
  $("acctEmail").textContent = user.email;
  $("acctCreated").textContent = day(user.createdAt);
  $("acctLastSignIn").textContent = moment(user.lastSignInAt);
  $("acctPwChanged").textContent = day(user.passwordChangedAt);
  const form = $("profileForm");
  form.elements.name.value = user.name;
  form.elements.email.value = user.email;
  const navAvatar = document.querySelector(".nav-avatar");
  if (navAvatar) navAvatar.textContent = user.initials;
}

async function load() {
  let me;
  try {
    me = await send("GET", "/api/v1/auth/me");
  } catch (x) {
    $("acctError").textContent = x.message;
    $("acctError").hidden = false;
    return;
  }
  if (!me.user) { location.replace("/signin?next=/account"); return; }
  render(me.user);
  if (me.accounts && !me.accounts.persistent) {
    $("acctNotice").textContent = "This OpsMind keeps accounts in memory, so this one " +
      "is lost when the server restarts. A deployed portal keeps them in Firestore.";
    $("acctNotice").hidden = false;
  }
  // The workspace card is context, not the point of the page: if the API is
  // unreachable it keeps its dashes.
  send("GET", "/api/v1/meta").then((m) => {
    const c = (m && m.config) || {};
    $("wsProject").textContent = c.activeProject || c.projectId || "Not connected";
    $("wsRegion").textContent = c.region || "—";
    $("wsSource").textContent = c.dataSource === "gcp" ? "Google Cloud"
      : c.dataSource === "local" ? "Local ingest" : "—";
  }).catch(() => {});
}

bindForm($("profileForm"), async (f) => {
  const { user } = await send("PATCH", "/api/v1/auth/me", { name: f.name });
  render(user);
});

bindForm($("passwordForm"), async (f, form) => {
  if (f.newPassword !== f.confirmPassword) throw new Error("The new passwords do not match.");
  const { user } = await send("POST", "/api/v1/auth/password", {
    currentPassword: f.currentPassword, newPassword: f.newPassword
  });
  form.reset();
  render(user);
});

$("signOut").addEventListener("click", async () => {
  try { await send("POST", "/api/v1/auth/signout"); } catch (_) { /* leave anyway */ }
  location.assign("/signin");
});

load();
})();
