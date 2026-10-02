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
