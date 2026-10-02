# Deploying OpsMind to Cloud Run

OpsMind only. This assumes **CogniKart is already deployed and running** — see
`DEPLOY_COGNIKART.md` if it is not.

You run every command. Nothing here touches your cloud by itself.

---

## Part 1 — What makes this different from CogniKart

You have deployed to Cloud Run once already, so the mechanics are familiar.
Three things genuinely differ, and they are the interesting parts.

### 1. It is ONE service, not four

```
CogniKart (4 services) ──stdout──▶ Cloud Logging ─┐
                                   Cloud Monitoring ├──▶ OpsMind ──▶ your browser
                                                   ─┘
```

The dashboard is plain HTML, CSS and JavaScript — no npm, no build step — served
by the same FastAPI process that exposes the JSON API.

**That is why there is no separate frontend repo or service.** The process
holding the service account is the same one serving the page, so the browser
never holds a Google Cloud credential. Split them and you would need a second
Cloud Run service, CORS between them, and a decision about how the browser
authenticates to the API — all cost, no benefit.

### 2. It needs IAM roles

CogniKart needed **none** — writing to stdout requires no permission. OpsMind
*reads* your project's telemetry, so it needs two read-only roles:

| Role | Why |
|---|---|
| `roles/logging.viewer` | Read log entries through the Cloud Logging API |
| `roles/monitoring.viewer` | Read Cloud Run system metrics |

Both are **read-only**. OpsMind observes and advises; it never changes
infrastructure. That is worth saying out loud in your presentation.

### 3. It must be protected

OpsMind can read your project's logs. On a public URL, so can anyone who finds
it. You will set a `DASHBOARD_TOKEN`.

---

## Part 2 — Prerequisites

Your Cloud Shell session may have dropped since last time, so re-establish the
variables:

```bash
export PROJECT_ID=$(gcloud config get-value project) && export REGION=asia-south1 && echo "$PROJECT_ID in $REGION"
```

OpsMind needs CogniKart's gateway URL for the Scenario Lab buttons. Fetch it
rather than retyping it:

```bash
export GATEWAY_URL=$(gcloud run services describe cognikart-gateway --region $REGION --format 'value(status.url)') && echo $GATEWAY_URL
```

If that prints nothing, CogniKart is not deployed — go back to
`DEPLOY_COGNIKART.md`.

### Enable the monitoring API

Logging is already on from CogniKart. Monitoring is not:

```bash
gcloud services enable monitoring.googleapis.com
```

Free to enable, as always. You pay for usage, and **reading Google Cloud system
metrics is not chargeable at all**.

---

## Part 3 — Clone the repository

```bash
cd ~ && git clone https://github.com/YOUR_USERNAME/YOUR_OPSMIND_REPO.git opsmind && cd opsmind
```

Check the Dockerfile is at the root:

```bash
ls Dockerfile main.py requirements.txt engine/ collectors/ static/
```

---

## Part 4 — A service account that can read telemetry

```bash
gcloud iam service-accounts create sa-opsmind --display-name "OpsMind portal"
```

```bash
export SA_MIND="sa-opsmind@${PROJECT_ID}.iam.gserviceaccount.com" && echo $SA_MIND
```

Now grant the two read-only roles. These are **project-level** bindings, which
is why the command shape differs from creating the account:

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID --member "serviceAccount:$SA_MIND" --role roles/logging.viewer --condition=None --quiet > /dev/null && echo "granted logging.viewer"
```

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID --member "serviceAccount:$SA_MIND" --role roles/monitoring.viewer --condition=None --quiet > /dev/null && echo "granted monitoring.viewer"
```

Confirm both landed:

```bash
gcloud projects get-iam-policy $PROJECT_ID --flatten="bindings[].members" --filter="bindings.members:sa-opsmind" --format="value(bindings.role)"
```

You should see exactly two roles. If you see more, something granted too much.

> **IAM takes up to a minute to propagate.** If the first deploy reports
> permission errors in its logs, wait and reload before changing anything.

---

## Part 5 — Deploy

Generate the dashboard token first and **write it down** — you need it in the
URL to get in:

```bash
export DASHBOARD_TOKEN=$(openssl rand -hex 16) && echo "SAVE THIS TOKEN: $DASHBOARD_TOKEN"
```

Then deploy:

```bash
gcloud run deploy opsmind-portal --source . --region $REGION --service-account $SA_MIND --allow-unauthenticated --memory 512Mi --cpu 1 --concurrency 40 --min-instances 0 --max-instances 2 --set-env-vars "DATA_SOURCE=gcp,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,COGNIKART_GATEWAY_URL=$GATEWAY_URL,DASHBOARD_TOKEN=$DASHBOARD_TOKEN,WATCHED_SERVICES=cognikart-gateway;cognikart-catalog;cognikart-orders;cognikart-payments;opsmind-portal"
```

The flags that matter here:

| Flag | Why |
|---|---|
| `--service-account $SA_MIND` | The new identity, not CogniKart's |
| `DATA_SOURCE=gcp` | Read Cloud Logging. `local` would wait for direct ingest and show nothing |
| `WATCHED_SERVICES` | Which Cloud Run services to observe. **Semicolons, not commas** — `--set-env-vars` already uses comma as its own delimiter, so commas here would be read as new variables |
| `--max-instances 2` | OpsMind keeps its working set in memory. Two instances would each poll and each hold a different view |
| `--allow-unauthenticated` | Public URL, gated by the token |

```bash
export OPSMIND_URL=$(gcloud run services describe opsmind-portal --region $REGION --format 'value(status.url)') && echo "OPEN THIS: $OPSMIND_URL/?token=$DASHBOARD_TOKEN"
```

---

## Part 6 — Verify

### Is it reading your logs?

```bash
curl -s "$OPSMIND_URL/api/v1/meta?token=$DASHBOARD_TOKEN" | python3 -m json.tool | head -40
```

Look for:

- `"dataSource": "gcp"` — reading Cloud Logging, not waiting for ingest
- `collectors.logs.lastError: null` — no permission problem
- `store.bufferedEntries` above zero — entries are arriving
- `ruleWarnings: []` — every alert rule resolves to a real metric

If `lastError` mentions permissions, the IAM binding has not propagated. Wait a
minute and retry before changing anything.

### Give it something to watch

Nothing interesting appears until CogniKart has traffic:

```bash
cd ~/CogniKart && python3 loadgen/loadgen.py --url $GATEWAY_URL --profile normal --duration 180 && cd ~/opsmind
```

### Open the dashboard

```bash
echo "$OPSMIND_URL/?token=$DASHBOARD_TOKEN"
```

Within a few seconds the Live Logs view should stream entries from all four
CogniKart services. Charts fill in over the next few minutes.

### The whole pipeline, in one gesture

1. Open **CogniKart** in one tab, **OpsMind** in another
2. On the storefront, sign in and place an order
3. Copy the trace id from the ribbon at the foot of the page
4. Paste it into OpsMind's Live Logs search

The same click, seen from the monitoring side — ten or so entries across four
services, one trace. Click a row for the waterfall.

### Raise an incident

The payment dropdown produces one failed order: a single log line, not an
incident. An incident needs a sustained failure rate, which is what the
Scenario Lab is for.

In OpsMind, pick `payment-timeout-cascade` and press **Inject**, with the load
generator running. Within about a minute the Incidents view should show a
suspected root cause of `cognikart-payments`, a cloud cost delta and revenue at
risk.

Press **Recover** when you are finished.

---

## Part 7 — Optional: the Gemini explanation

```bash
gcloud services enable aiplatform.googleapis.com
```

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID --member "serviceAccount:$SA_MIND" --role roles/aiplatform.user --condition=None --quiet > /dev/null && echo granted
```

```bash
gcloud run services update opsmind-portal --region $REGION --update-env-vars AI_ENABLED=true
```

**Cost:** Gemini 2.5 Flash is $0.30 per million input tokens and $2.50 per
million output. One explanation is roughly 4k in and 400 out — well under a
cent, and explanations are cached per incident.

Leaving this off is a perfectly good choice. Without it OpsMind returns a
deterministic narrative built from the same evidence bundle, so the demo never
depends on an API call succeeding.

---

## Part 8 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 from every endpoint | Missing token | Append `?token=...` to the URL |
| Dashboard loads, no logs | `DATA_SOURCE` not `gcp` | Check `/api/v1/meta`; redeploy with it set |
| `lastError` mentions permission | IAM not propagated, or role missing | Wait 60s; re-check Part 4 |
| Logs appear but no services listed | `WATCHED_SERVICES` used commas | Redeploy with **semicolons** |
| Scenario Lab buttons fail | `COGNIKART_GATEWAY_URL` wrong or empty | `gcloud run services update opsmind-portal --region $REGION --update-env-vars COGNIKART_GATEWAY_URL=$GATEWAY_URL` |
| Charts empty, logs fine | No traffic yet | Run the load generator |
| Cost panel shows zero | No completed minute buckets yet | Wait two minutes with traffic |

Inspect what a service actually received:

```bash
gcloud run services describe opsmind-portal --region $REGION --format 'value(spec.template.spec.containers[0].env)'
```

Read its own logs:

```bash
gcloud run services logs read opsmind-portal --region $REGION --limit 40
```

---

## Part 9 — What this costs

| Resource | Position |
|---|---|
| Cloud Run | One small service, scales to zero. Inside the free tier |
| Reading Cloud Logging | **No charge** for queries |
| Reading Cloud Monitoring | **No charge** — Google Cloud system metrics are non-chargeable |
| Vertex AI (optional) | Fractions of a cent per explanation |

OpsMind adds essentially nothing to your bill. It reads data you are already
generating, and reading is free.

---

## Part 10 — Tear down

> **Not until the hackathon is over.**

```bash
gcloud run services delete opsmind-portal --region $REGION --quiet
```

```bash
for r in roles/logging.viewer roles/monitoring.viewer roles/aiplatform.user; do gcloud projects remove-iam-policy-binding $PROJECT_ID --member "serviceAccount:$SA_MIND" --role $r --condition=None --quiet > /dev/null 2>&1; done && gcloud iam service-accounts delete $SA_MIND --quiet
```

---

## One honest note

OpsMind is gated by a shared token in the query string. That is adequate for a
hackathon demo and inadequate for anything longer-lived — the token appears in
browser history and in any link you paste.

Properly, this would sit behind Identity-Aware Proxy, or be deployed
`--no-allow-unauthenticated` and reached through
`gcloud run services proxy opsmind-portal --region $REGION`.

Worth naming before a judge asks.
