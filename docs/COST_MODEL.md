# Modeled Cost

**Modeled cost = measured usage × Google's published list price.**

It is labelled *modeled* because it is calculated, not billed. Google's billing
export arrives about a day later and is shown separately.

## 1. Usage measured from the logs

Per service, over the selected time window:

| Quantity | Source |
|---|---|
| Requests | Count of `http.request.*` log entries |
| Request-seconds | Requests × average response time |
| Log bytes | Total size of the log entries |

Average response time is estimated from the latency percentiles:

```
average time ≈ 0.75 × p50 + 0.25 × p95
request-seconds = Σ per minute ( average time in seconds × requests )
```

## 2. Cost formulas

Prices are in `pricing/skus.json` (verified 2026-10-02).

| Part | Formula | Price |
|---|---|---|
| Cloud Run CPU | request-seconds × vCPU × price | $0.000024 per vCPU-second |
| Cloud Run memory | request-seconds × memory GiB × price | $0.0000025 per GiB-second |
| Cloud Run requests | (requests ÷ 1,000,000) × price | $0.40 per million |
| Cloud Logging | (log bytes ÷ 1 GiB) × price | $0.50 per GiB |
| Cloud Monitoring | — | $0 (system metrics are free to read) |

```
total = CPU + memory + requests + logging
```

vCPU and memory are each service's configured size (default 1 vCPU, 0.5 GiB).

## 3. Rates

```
cost per hour  = total ÷ window length in hours
cost per day   = cost per hour × 24
cost per month = cost per hour × 24 × 30
```

## 4. Incident cost

```
incident cost delta = cost per hour during the incident − cost per hour in the 15 minutes before it
```

## 5. Worked example

payments service, 15-minute window: 600 requests, p50 200 ms, p95 1,000 ms, 5 MiB of logs.

| Step | Calculation | Result |
|---|---|---|
| Average time | 0.75 × 0.2 + 0.25 × 1.0 | 0.40 s |
| Request-seconds | 600 × 0.40 | 240 s |
| CPU | 240 × 1 × $0.000024 | $0.00576 |
| Memory | 240 × 0.5 × $0.0000025 | $0.00030 |
| Requests | 600 ÷ 1,000,000 × $0.40 | $0.00024 |
| Logging | 5 ÷ 1024 × $0.50 | $0.00244 |
| Total (15 min) | | $0.00874 |
| Per hour | $0.00874 ÷ 0.25 | $0.035 |
| Per month | $0.035 × 24 × 30 | ≈ $25 |

## 6. Rules

- The free tier is reported separately. Monthly Cloud Run free tier: 180,000
  vCPU-seconds, 360,000 GiB-seconds, 2 million requests. Cloud Logging: 50 GiB.
- Every figure carries its usage and arithmetic in the API response (`basis`).
- Request-based billing is modeled. Instance-based billing is not.
