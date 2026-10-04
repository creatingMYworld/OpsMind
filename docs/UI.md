# OpsMind UI

How the interface is put together, what each page is for, and the decisions
behind it. Read this before changing the UI.

Work happens on the `opsmind-staging` branch. `main` is untouched by the
redesign.

## Pages

| Page | Route | Built from |
|---|---|---|
| Landing | `/` | `static/landing.html`, `landing.css`, `landing.js` |
| How it works | `/how-it-works` | `static/how-it-works.html` (four steps, video slot) |
| About | `/about` | `static/about.html` (team cards are placeholders) |
| Dashboard | `/app#<view>` | `static/index.html`, `app.js`, `styles.css` |

Dashboard views: `overview`, `incidents`, `insights`, `services`,
`performance`, `logs`, `resources`, `cost`, `alerts`, `setup`.
`#errors` resolves to Logs and `#projects` to Setup, so old links still work.

The landing page's Start button goes to `/app#setup`, where the project is
chosen.

## Shell

- **Left rail**: logo block, one icon per page, red count badges on
  Incidents and Insights.
- **Top bar**: page title and subtitle (read from the active view's own
  heading, which is hidden), project picker, time window, live cadence,
  Refresh (spinning, then "Refreshed"), notifications, theme.
- **Project picker** lists every project OpsMind can see; ones without log
  access are disabled. It switches with `POST /api/v1/projects/select`, the
  same call as Setup's Open button.

## Overview, top to bottom

1. Status strip: project identity and refreshed time; Overall health,
   Services, Fix now, Availability, P95.
2. Things needing attention: each item closed is two lines; open, it shows
   Why it matters, First step, Fixed when, Impact, Conditions.
3. Key numbers: Modeled spend, Active incidents, Requests, Error rate. Each
   tile has a sparkline and links to the page that explains it.
4. What is failing: 5xx, 4xx, slow requests, CPU utilization (each with a
   sparkline); severity volume chart beside a severity split.
5. Health by service: one row per service, worst first.
6. Checkout funnel beside modeled cloud spend.
7. Failures and slow paths: ranked bars.
8. Top cost-saving actions as a two-line summary.

## Other pages

- **Services**: summary tiles (services, needing attention, total requests,
  total errors), then one card per service, worst first: grade, View logs /
  View errors, Requests, Error rate, Availability, CPU, P50, P95, P99, 5xx and
  4xx counts, top failure, route chips. OpsMind has no Apdex, so CPU takes
  that slot.
- **Logs**: one filter row (Search, Severity, Service, Status, Route, Event,
  match count, pause), a matching-volume chart, then the table. Severity,
  service, event and search are filtered by the API; status and route on the
  client. Choosing ERROR/CRITICAL or 5xx shows **Error details** (tiles and
  error groups for the selected service) above the matching error logs. Rows
  are newest first; clicking one opens the entry and its trace.
- **Insights**: anomalies (each with a likely cause: the most frequent error
  on that service), incidents as a table of likely cause, impact and
  recommended action, then failure patterns. Impact and action come from the
  open actions; a resolved incident with no open action shows a dash.
- **Incidents**: tiles, then Breaching and Resolved. The "High or critical"
  tile counts both severities, as the API does.
- **Performance**: p50/p95/p99 tiles, percentile chart, throughput against
  failures, every route.
- **Setup**: connection tiles, project tiles, cloud platforms (Azure and AWS
  coming soon), Google Cloud services **in use** and **available to
  connect**.

## Visual system

- Neutral near-black theme shared with the landing page; light theme
  re-stepped from the same tokens. Accent `#6c9cff` (dark) / `#3b6fe0`
  (light).
- Geist and Geist Mono; tabular figures wherever numbers are compared.
- Flat cards: crisp 1px border, no drop shadow.
- **Status colours** (`--ok`, `--warn`, `--err`, `--crit`) mean state only.
  They are never used as a series colour.
- **Series colours** `--s1`..`--s4` (blue, orange, aqua, yellow) are the
  dataviz reference slots, in fixed order. Validated with the dataviz
  palette checker: dark passes every check (worst adjacent CVD dE 8.4);
  light passes with a contrast note on two slots, which is why every
  multi-series chart has a visible legend.
- One Chart.js theme for every chart (`applyChartTheme()` in `app.js`):
  no x gridlines, quiet y grid, theme fonts, one tooltip style, 2px lines,
  rounded bars, 2px surface gaps in stacks and doughnuts. Never two
  y-axes.

The landing page's product previews copy the dashboard's colour values
(`.dash` in `landing.css`, `C` in `landing.js`). If the dashboard tokens
change, change those too.

## AI

Incident explanations use Gemini through Vertex AI (`ai/explain.py`,
`vertexai=True`, model `AI_MODEL`, default `gemini-2.5-flash`). The UI says
"Gemini" only when `AI_ENABLED=true`; otherwise it says "Explain", because
the write-up is then built deterministically from the evidence.

## Backend additions made for the UI

| Change | Why |
|---|---|
| `GET /api/v1/routes` | Per-route p95, 5xx rate and slow-request count, read from the log buffer. Minute buckets only keep overall percentiles. Takes `window`, `slow_ms`, `limit`. |
| `GET /how-it-works`, `GET /about` | Marketing pages. |
| Local `Check access` | In local mode the `local` project reports its direct-ingest connection instead of probing Cloud Logging for a project that does not exist. |
| `GET /api/v1/cost/summary` | Optimization Center summary: Gemini when AI is on, deterministic otherwise, with `ai` saying which. |
| `toast()` in `app.js` | Setup's project switch called it before it existed, so a successful switch threw. |

## Running it locally

From the directory above the clone (it is a package with relative imports):

```bash
OpsMind/.venv/Scripts/python -m uvicorn OpsMind.main:app --port 8081
```

Local mode keeps everything in memory, so a restart empties it. To see the
UI with data, POST CogniKart-shaped entries to `/internal/ingest`. Requests
are only counted for events starting `http.request` with an `httpStatus`;
CPU comes from `service.heartbeat` events with `cpuPct`.

## Status and next work (2026-10-04)

Local branch `ui-updates` is ahead of `origin/opsmind-staging`. Not pushed.

Done in this pass:

1. **Resources**: a 2x2 grid of per-service line charts, one unit each -- CPU
   utilisation (%), Memory (MiB), In-flight requests, Instance count. Series
   use `--s1`..`--s4` (they previously borrowed status colours), each chart
   labels its own y ticks with the unit, and the instance chart is stepped to
   whole numbers so ticks cannot repeat.
2. **Cost, Optimization Center**: lifted out of its card into its own section
   -- three summary tiles (calculated savings, open recommendations, high
   severity), a written **Summary**, then one card per recommendation in a
   two-column grid with its saving, evidence and command. The summary comes
   from `GET /api/v1/cost/summary`, which asks Gemini when `AI_ENABLED=true`
   and otherwise returns the same numbers summarised deterministically. The
   response carries `ai: true|false`, and only `ai: true` text is tagged
   **AI - Gemini on Vertex AI** (`AI_TAG` in `app.js`, `.pill.ai`). The
   incident explanation uses the same tag.
3. **Alerts**: four tiles, the two delivery sources side by side, then one
   card per rule -- name, category, source and state on the left; metric,
   window, severity, the editable threshold and the buttons on the right;
   current breaches underneath.
4. **Setup**: each **In use** service is now a button. Clicking it opens a
   drawer with what that Google Cloud service costs in the modeled spend --
   its rate, share of the project, projections, the drivers it is charged on
   (Cloud Run = cpu + memory + requests, Cloud Logging = logging), and for
   Cloud Run the per-application-service split. Services OpsMind does not
   price say so rather than showing a zero.
5. **Incident evidence** (`incidentDetailHtml`, used by both the expanded
   incident card and the drawer): the stack of bare `<h5>` headings and inline
   styles became `.ev` sections in a two-column `.ev-grid` -- Impact (wide),
   Suspected root cause, Top errors, Before vs during, Sample trace, Timeline
   (wide), Explanation (wide). Everything reuses `fact()`, `.ev-table` and the
   existing pills; it collapses to one column under 1000px, which is how it
   renders in the drawer.

Backend added: `GET /api/v1/cost/summary` and `ai/explain.summarize_cost()`
(120s cache, deterministic fallback, never raises).
