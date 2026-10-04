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
