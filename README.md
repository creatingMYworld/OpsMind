# OpsMind

A cloud log monitoring, resource visibility and cost-optimization portal for
Google Cloud. It reads application logs out of **Cloud Logging** and metrics out
of **Cloud Monitoring**, and turns them into incidents, error groups, resource
charts, modeled cost and optimization recommendations.

Built for Cognizant GCP Hackathon **Use Case 2**. This is the deliverable; the
application it observes (CogniKart) is deployed separately.

## One service, not two

The dashboard is plain HTML, CSS and JavaScript — about 1,200 lines, Chart.js
from a CDN, no npm and no build step — served by the same FastAPI process that
exposes the JSON API.

That is deliberate. The process holding the Cloud Run service account is the
same one serving the page, so **the browser never holds a Google Cloud
credential**. Splitting the UI into its own service would add a second
deployment, CORS between them, and a question about how the browser
authenticates to the API — for no benefit.

## What it shows

| View | Contents |
|---|---|
| Overview | Health score, traffic and errors, modeled spend, service table, checkout funnel |
| Live Logs | Streaming log view with filters, and a trace waterfall across services |
| Errors | Deterministic error grouping; server faults kept distinct from client mistakes |
| Resources | CPU, memory against provisioned, instance counts |
| Cost & Optimization | Modeled spend by driver, free-tier position, recommendations with calculated savings |
| Alerts | Editable thresholds |
| Incidents | Timeline, suspected root cause, cloud cost and revenue at risk |

## Three latency tiers, labelled

Cloud Monitoring samples Cloud Run about once a minute and takes a few more
minutes to expose it; billing export is a day behind. Pretending otherwise would
make the dashboard lie, so every panel says which tier it is on:

| Tier | Latency | Source |
|---|---|---|
| LIVE | 1–5 s | Structured logs and service heartbeats |
| NEAR REAL TIME | 1–5 min | Cloud Monitoring system metrics |
| AUTHORITATIVE | hours–1 day | Cloud Billing export |

## Honesty guarantees

- Live cost is **modeled** — measured usage times Google's published list prices
  — and labelled as such. Billed cost is a separate tier.
- A savings figure appears only when calculated from verified pricing;
  otherwise the recommendation reads "potential optimization opportunity".
- The optional AI explanation narrates evidence and never computes. Every
  number it emits is checked against the evidence bundle, and a deterministic
  fallback always exists.

## Historical data (optional)

Everything above works with no database: Cloud Logging is the durable store and
the portal keeps a bounded in-memory working set. That working set holds
minutes, so it can never answer "is today worse than yesterday?".

Firestore is added for that one job, and nothing else. **Raw logs are never
written to it.** One small document per project per day holds counters and
averages — requests, 4xx/5xx, latency, CPU, memory, instances, log volume, a
cost snapshot and per-service totals — plus one document per incident. A busy
day is a few kilobytes.

It is **off by default** and failure-tolerant by design: the writer runs on its
own thread, every call is wrapped, and if Firestore is unreachable the
dashboard behaves exactly as it did before. With fewer than two days of data
the panel reads "Historical data is being collected" — no day is ever
estimated to fill a gap.

### Turning it on

```bash
gcloud services enable firestore.googleapis.com
```

```bash
gcloud firestore databases create --location=asia-south1 --type=firestore-native
```

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID --member "serviceAccount:$SA_MIND" --role roles/datastore.user --condition=None
```

Then redeploy with `HISTORY_ENABLED=true`. `roles/datastore.user` is the
Firestore role — the product was formerly Cloud Datastore and the role name
never changed.

Read it at `/api/v1/history/compare` and `/api/v1/history/days`, or look at
**Today vs yesterday** on the Overview page.

## Accounts

People sign up, sign in, and manage their profile at `/account`. The access
token is still the root of access: **creating an account needs the token**,
either from the `?token=` link the person arrived on or typed in as an access
code. Open sign-up would hand the project's logs to anyone who found the URL.
After that, signing in is how they come back — no token in a bookmark.

Opening the portal without access lands on `/signin` instead of an error page.
The `?token=` link still opens the product pages, but the dashboard itself
(`/app`, where every Start button goes) needs a signed-in account — with or
without a token. Not signed in, Start goes to sign in first and then on to the
dashboard. The API keeps accepting the token, so scripts need no account.

Accounts live in **Firestore** on Cloud Run, because Cloud Run scales to zero
and forgets memory. Locally they live in memory and the account page says so.
Passwords are hashed with PBKDF2-HMAC-SHA256 (600,000 iterations, per-password
salt); sessions are signed cookies, checked without a database read. Setup is
Steps 1–3 of DEPLOY.md Part 10 — no new environment variable is needed.

## Deploy

See **[DEPLOY.md](DEPLOY.md)**. Unlike the application it watches, OpsMind
**does** need IAM roles — read-only access to logging and monitoring.

## Run locally

The code uses relative imports, so clone into a lowercase directory:

```bash
git clone <this repo> opsmind && cd opsmind
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

Then from the directory *above* `opsmind`:

```bash
DATA_SOURCE=local PORT=8000 opsmind/.venv/bin/python -m uvicorn opsmind.main:app --port 8000
```

`DATA_SOURCE=local` accepts log entries POSTed to `/internal/ingest` instead of
reading Cloud Logging, so the whole portal works with no GCP project at all.

## Environment variables

| Variable | Purpose |
|---|---|
| `DATA_SOURCE` | `gcp` reads Cloud Logging and Monitoring; `local` accepts direct ingest |
| `GOOGLE_CLOUD_PROJECT` | Project to read logs and metrics from |
| `GCP_REGION` | Region label |
| `WATCHED_SERVICES` | Cloud Run services to observe. Comma **or semicolon** separated |
| `COGNIKART_GATEWAY_URL` | Only for the Scenario Lab buttons; carries no telemetry |
| `DASHBOARD_TOKEN` | Shared secret gating the dashboard. **Set this** — it can read your logs |
| `AI_ENABLED` | `true` turns on the Gemini explanation. Off by default |
| `LOGS_POLL_INTERVAL_S` | Cloud Logging poll cadence, default 4s |
| `METRICS_POLL_INTERVAL_S` | Cloud Monitoring poll cadence, default 60s |
| `HISTORY_ENABLED` | `true` writes daily rollups to Firestore. Off by default |
| `FIRESTORE_DATABASE` | Firestore database id, default `(default)` |
| `HISTORY_COLLECTION` | Daily rollups collection, default `opsmind_daily` |
| `HISTORY_EVENTS_COLLECTION` | Incident history collection, default `opsmind_incidents` |
| `HISTORY_WRITE_INTERVAL_S` | How often a rollup is written, default 300s |
| `HISTORY_TZ_OFFSET_MINUTES` | Which clock ends the day, default 330 (IST). UTC would roll over at 05:30 local |
| `ACCOUNTS_BACKEND` | Where accounts live: `firestore` (default when `DATA_SOURCE=gcp`) or `memory` (default locally; lost on restart) |
| `ACCOUNTS_COLLECTION` | Firestore collection for accounts, default `opsmind_users` |
| `SESSION_SECRET` | Signs sign-in cookies. Defaults to a value derived from `DASHBOARD_TOKEN`, so rotating the token signs everyone out |
