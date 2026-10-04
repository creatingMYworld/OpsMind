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

Confirm CogniKart is running, since OpsMind has nothing to watch otherwise:

```bash
export GATEWAY_URL=$(gcloud run services describe cognikart-gateway --region $REGION --format 'value(status.url)') && echo $GATEWAY_URL
```

If that prints nothing, CogniKart is not deployed — go back to
`DEPLOY_COGNIKART.md`. OpsMind itself never calls that URL; it reads
everything through the Cloud Logging and Cloud Monitoring APIs.

### Enable the APIs OpsMind needs

Logging is already on from CogniKart. Two more are not:

```bash
gcloud services enable monitoring.googleapis.com cloudresourcemanager.googleapis.com
```

Free to enable, as always. You pay for usage, and **reading Google Cloud system
metrics is not chargeable at all**.

`cloudresourcemanager` is what lets OpsMind list the projects you can see, so
the project picker shows real projects rather than a list you typed into a
config file.

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

### Multi-project: two permissions, deliberately separate

OpsMind's project picker lists the projects it can **see** and tells you, per
project, whether it can **read** them. Those are different permissions, and
keeping them apart is the point:

| Permission | Grants | Where |
|---|---|---|
| `resourcemanager.projects.get` | seeing a project exists | once, usually org-wide via `roles/browser` |
| `logging.viewer` | reading that project's logs | per project |

To let the picker enumerate every project in your organisation:

```bash
export ORG_ID=$(gcloud organizations list --format 'value(ID)' | head -1) && echo $ORG_ID
```

```bash
gcloud organizations add-iam-policy-binding $ORG_ID --member "serviceAccount:$SA_MIND" --role roles/browser --condition=None --quiet > /dev/null && echo "granted roles/browser on the org"
```

**This is optional.** Without it the picker still works — it simply lists only
the projects OpsMind already has a binding on, which on a single-project
hackathon is exactly the one that matters.

Log access stays per project on purpose. To connect a second project later:

```bash
gcloud projects add-iam-policy-binding OTHER_PROJECT_ID --member "serviceAccount:$SA_MIND" --role roles/logging.viewer --condition=None --quiet
```

The picker prints that exact command for any project it cannot read, so you do
not have to come back here for it.

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
gcloud run deploy opsmind-portal --source . --region $REGION --service-account $SA_MIND --allow-unauthenticated --memory 512Mi --cpu 1 --concurrency 40 --min-instances 0 --max-instances 2 --set-env-vars "DATA_SOURCE=gcp,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,DASHBOARD_TOKEN=$DASHBOARD_TOKEN,SERVICE_ACCOUNT_EMAIL=$SA_MIND,WATCHED_SERVICES=cognikart-gateway;cognikart-catalog;cognikart-orders;cognikart-payments;opsmind-portal"
```

The flags that matter here:

| Flag | Why |
|---|---|
| `--service-account $SA_MIND` | The new identity, not CogniKart's |
| `DATA_SOURCE=gcp` | Read Cloud Logging. `local` would wait for direct ingest and show nothing |
| `WATCHED_SERVICES` | Which Cloud Run services to observe. **Semicolons, not commas** — `--set-env-vars` already uses comma as its own delimiter, so commas here would be read as new variables |
| `--max-instances 2` | OpsMind keeps its working set in memory. Two instances would each poll and each hold a different view |
| `--allow-unauthenticated` | Public URL, gated by the token |

OpsMind has **no endpoint that changes anything**, anywhere. Read-only IAM
roles and a read-only API: it observes and advises, and cannot act on — or
break — the application it watches. Fault injection lives in CogniKart's own
Scenario Lab.

`SERVICE_ACCOUNT_EMAIL` is cosmetic: it is only used to print the exact
`gcloud` command in the picker's "grant access" hints. Nothing authenticates
with it.

```bash
export OPSMIND_URL=$(gcloud run services describe opsmind-portal --region $REGION --format 'value(status.url)') && echo "OPEN THIS: $OPSMIND_URL/?token=$DASHBOARD_TOKEN"
```

That URL is the **product page**. The dashboard itself is at `/app`, and the
Start button routes to `/app#projects` — the project picker.

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

Open **CogniKart's** Scenario Lab, pick `payment-timeout-cascade` and inject it,
with the load generator running. Within about a minute OpsMind's Incidents view
should show a suspected root cause of `cognikart-payments`, a cloud cost delta
and revenue at risk. Expand the incident for its top errors, a sample trace and
an explanation.

Press **Recover everything** in CogniKart's Scenario Lab when you are finished.

> Injection deliberately lives in the application, not the monitoring tool. A
> platform that can break what it watches is a harder thing to trust.

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
| 401 from every endpoint | Missing token | Append `?token=...` to the URL. It is exchanged for a cookie, so you only need it once |
| Page loads as plain unstyled text | Stale build where the token gate also blocked `/static` | Redeploy. Assets are deliberately not gated: they are program code, not data |
| Dashboard loads, no logs | `DATA_SOURCE` not `gcp` | Check `/api/v1/meta`; redeploy with it set |
| `lastError` mentions permission | IAM not propagated, or role missing | Wait 60s; re-check Part 4 |
| Logs appear but no services listed | `WATCHED_SERVICES` used commas | Redeploy with **semicolons** |
| Dashboard shows an old layout after a redeploy | Browser cached the assets | Should not happen — assets are fingerprinted per deploy and the HTML is `no-store`. If it does, hard-reload |
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

## Part 10 — Optional: persistent history in Firestore

Everything else works without this. Cloud Logging is the durable store; the
portal's in-memory working set holds **minutes**, which is why it cannot answer
"is today worse than yesterday?". Firestore is added for that one question.

**Raw logs are never written to Firestore.** One document per project per day
holds counters and averages — requests, 4xx/5xx, latency, CPU, memory,
instances, log volume, a cost snapshot and per-service totals — plus one
document per incident. A busy day is a few kilobytes.

**Step 1 — restore the shell variables** (§0 of `DECISIONS.md`), then:

```bash
gcloud services enable firestore.googleapis.com
```

**Step 2 — create the database.** One per project; `firestore-native` is the
mode you want, and the location cannot be changed afterwards.

```bash
gcloud firestore databases create --location=asia-south1 --type=firestore-native
```

If it reports the database already exists, that is a success — skip on.

**Step 3 — grant the role.** `roles/datastore.user` *is* the Firestore role:
the product was formerly Cloud Datastore and the role name never changed.
It allows reading and writing documents, not administering the database.

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID --member "serviceAccount:$SA_MIND" --role roles/datastore.user --condition=None --quiet
```

**Step 4 — turn it on.** `--update-env-vars` adds to the environment rather
than replacing it, unlike `--set-env-vars`:

```bash
gcloud run services update opsmind-portal --region $REGION --update-env-vars HISTORY_ENABLED=true
```

**Step 5 — verify.** The first rollup is written about a minute after boot and
then every five minutes.

```bash
curl -s "$OPSMIND_URL/api/v1/meta?token=$DASHBOARD_TOKEN" | python3 -c "import json,sys; h=json.load(sys.stdin)['collectors']['history']; print('enabled  :', h['enabled']); print('connected:', h['connected']); print('error    :', h['clientError'] or 'none'); print('lastWrite:', h['lastWrite'])"
```

Want `enabled: True`, `connected: True`, `error: none`. After a few minutes
`lastWrite` should show `ok: True` and a non-zero `minutesWritten`.

```bash
curl -s "$OPSMIND_URL/api/v1/history/compare?token=$DASHBOARD_TOKEN" | python3 -m json.tool | head -20
```

**Expect `available: false` with "Historical data is being collected" today.**
That is correct, not a fault: a comparison needs a full day on both sides, so
it starts working tomorrow. Nothing is estimated to fill the gap.

### What it costs

| Item | Charge |
|---|---|
| Firestore storage | 1 GiB free. A year of daily documents is well under a megabyte |
| Document reads | 50,000/day free. The dashboard reads 2 per Overview load |
| Document writes | 20,000/day free. One write per five minutes is 288/day |

Comfortably inside the free tier at this scale.

### If Firestore breaks

Nothing else does. The writer runs on its own thread, every call is wrapped,
and failures are reported through `/api/v1/meta` rather than raised. Live
monitoring, alerts, incidents, cost and the AI explanation are all unaffected —
there is a test for exactly this. To turn it off again:

```bash
gcloud run services update opsmind-portal --region $REGION --update-env-vars HISTORY_ENABLED=false
```

---

## Part 11 — Redeploy after an update

For a service that already exists. Nothing here creates anything, and the
existing environment variables, service account and IAM are all preserved.

**Step 1 — restore the shell variables.** Cloud Shell forgets every `export`
when the session drops, and the commands below then fail with errors that name
an argument rather than the real cause:

```bash
export PROJECT_ID=$(gcloud config get-value project) && export REGION=asia-south1 && export SA_MIND="sa-opsmind@${PROJECT_ID}.iam.gserviceaccount.com" && echo "$PROJECT_ID / $REGION / $SA_MIND"
```

**Step 2 — pull the new code.**

```bash
cd ~/opsmind && git pull origin main && git log --oneline -1
```

If `~/opsmind` is not there, clone it as in Part 3.

If `git pull` reports a conflict or refuses because of local changes, you
edited files in Cloud Shell. The repository is the source of truth, so discard
them:

```bash
cd ~/opsmind && git fetch origin && git reset --hard origin/main
```

**Step 3 — redeploy.** Deliberately no `--set-env-vars`: on an existing
service that flag *replaces* the whole environment, which would wipe
`DASHBOARD_TOKEN` and `AI_ENABLED` and lock you out of your own dashboard.
Omitting it keeps what is already there.

```bash
cd ~/opsmind && gcloud run deploy opsmind-portal --source . --region $REGION
```

Takes two to four minutes; it rebuilds the image with Cloud Build.

**Step 4 — verify the new revision is serving.**

```bash
export OPSMIND_URL=$(gcloud run services describe opsmind-portal --region $REGION --format 'value(status.url)') && export DASHBOARD_TOKEN=$(gcloud run services describe opsmind-portal --region $REGION --format=json | python3 -c "import json,sys; e=json.load(sys.stdin)['spec']['template']['spec']['containers'][0]['env']; print([x['value'] for x in e if x['name']=='DASHBOARD_TOKEN'][0])") && echo "OPEN: $OPSMIND_URL/?token=$DASHBOARD_TOKEN"
```

```bash
curl -s "$OPSMIND_URL/api/v1/meta?token=$DASHBOARD_TOKEN" | python3 -c "import json,sys; d=json.load(sys.stdin); c=d['config']; print('source   :', c['dataSource']); print('project  :', c['project']); print('aiEnabled:', c['aiEnabled']); print('views    :', d.get('routes') and 'ok')"
```

Then confirm the parts that are new in this revision actually respond:

```bash
for p in /api/v1/cost/summary /api/v1/routes /api/v1/guide/manifest /about /how-it-works; do printf "%-26s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$OPSMIND_URL$p?token=$DASHBOARD_TOKEN")"; done
```

All five should print `200`.

The dashboard's own assets are fingerprinted and the HTML is served
`Cache-Control: no-store`, so the browser picks up new JavaScript and CSS on an
ordinary reload. A hard refresh should not be necessary; if the page still
looks stale, that is worth investigating rather than working around.

---

## Part 12 — Tear down

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
