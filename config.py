"""OpsMind platform configuration.

DATA_SOURCE is the single most important switch:

  local  -- CogniKart services POST their log entries straight to
            /internal/ingest. No GCP project, no credentials, no gcloud.
            This is the development and fallback path.
  gcp    -- the platform reads the Cloud Logging API and the Cloud Monitoring
            API using Application Default Credentials from the Cloud Run
            runtime service account. This is the real submission architecture.

Both modes share the same normalizer, the same engines, the same API contract
and the same UI. Only the collector swaps. Every API response carries the
active source so the dashboard can label its provenance honestly.
"""
import json
import os
import re
from typing import Dict, List


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


_HERE = os.path.dirname(os.path.abspath(__file__))


class Settings:
    def __init__(self) -> None:
        self.data_source: str = _env("DATA_SOURCE", "local").lower()
        if self.data_source not in ("local", "gcp"):
            self.data_source = "local"

        self.project_id: str = (
            _env("GOOGLE_CLOUD_PROJECT") or _env("GCP_PROJECT") or ""
        )
        self.region: str = _env("GCP_REGION", "asia-south1")

        # The project currently being viewed. Starts as the deployment's own
        # project and can be switched at runtime from the project picker --
        # OpsMind watches one project at a time, and switching re-points the
        # collectors rather than keeping several working sets in memory.
        self.active_project: str = self.project_id
        # Shown in the "grant this role" hints on the picker. Purely cosmetic:
        # nothing authenticates with it.
        self.service_account_hint: str = _env("SERVICE_ACCOUNT_EMAIL", "")

        # Cloud Run service names the platform observes. CogniKart's four
        # services plus the platform itself -- the platform monitoring its own
        # log volume is both a demo beat and a billing safety net.
        # Accept either separator. gcloud's --set-env-vars uses comma as its
        # own delimiter, so a comma-separated list has to be escaped or passed
        # with semicolons; supporting both removes a sharp edge from deploys.
        self.watched_services: List[str] = [
            s.strip() for s in re.split(
                r"[,;]",
                _env(
                    "WATCHED_SERVICES",
                    "cognikart-gateway,cognikart-catalog,cognikart-orders,"
                    "cognikart-payments,opsmind-portal",
                ),
            ) if s.strip()
        ]

        # Retention in the platform's own memory. 5000 entries at ~600 bytes
        # is a few MB -- bounded, no database, survives nothing. Deliberate:
        # Cloud Logging is the durable store, this is a working set.
        self.log_buffer_size: int = _env_int("LOG_BUFFER_SIZE", 5000)
        self.minute_buckets: int = _env_int("MINUTE_BUCKETS", 180)

        # Poll cadences. See docs/ARCHITECTURE.md "three latency tiers".
        self.logs_poll_interval_s: float = _env_float("LOGS_POLL_INTERVAL_S", 4.0)
        self.metrics_poll_interval_s: float = _env_float("METRICS_POLL_INTERVAL_S", 60.0)
        self.rules_eval_interval_s: float = _env_float("RULES_EVAL_INTERVAL_S", 5.0)

        # Cloud Run per-service provisioning, used by the cost engine to turn
        # observed usage into money, and by the optimization engine to spot
        # over-provisioning. Must match what you actually deploy -- deploy.sh
        # and this table are kept in step on purpose.
        self.service_shape: Dict[str, Dict[str, float]] = {
            "cognikart-gateway": {"vcpu": 1.0, "memoryGib": 0.5, "maxInstances": 5},
            "cognikart-catalog": {"vcpu": 1.0, "memoryGib": 1.0, "maxInstances": 5},
            "cognikart-orders": {"vcpu": 1.0, "memoryGib": 0.5, "maxInstances": 5},
            "cognikart-payments": {"vcpu": 1.0, "memoryGib": 0.5, "maxInstances": 3},
            "opsmind-portal": {"vcpu": 1.0, "memoryGib": 0.5, "maxInstances": 2},
        }
        self.billing_model: str = _env("CLOUD_RUN_BILLING_MODEL", "requestBased")

        # Gemini incident explanation. Entirely optional: when disabled or
        # unavailable the deterministic narrative is used instead.
        self.ai_enabled: bool = _env("AI_ENABLED", "false").lower() in ("1", "true", "yes")
        self.ai_model: str = _env("AI_MODEL", "gemini-2.5-flash")
        self.ai_location: str = _env("AI_LOCATION", "global")
        self.ai_timeout_s: float = _env_float("AI_TIMEOUT_S", 12.0)

        # Persistent history in Firestore. Off by default: the live path has
        # never needed a database, and a portal that cannot reach Firestore
        # must keep working exactly as before. See opsmind/history.py.
        self.history_enabled: bool = _env("HISTORY_ENABLED", "false").lower() in (
            "1", "true", "yes")
        self.firestore_database: str = _env("FIRESTORE_DATABASE", "(default)")
        # Edited alert thresholds. A deployed portal keeps them in Firestore,
        # because Cloud Run forgets its memory and disk on every restart and
        # each instance would otherwise hold its own copy. Locally they go to
        # a small JSON file beside the code.
        self.alerts_backend: str = _env(
            "ALERTS_BACKEND", "firestore" if self.data_source == "gcp" else "file").lower()
        self.alerts_collection: str = _env("ALERTS_COLLECTION", "opsmind_settings")
        self.alerts_file: str = _env("ALERTS_FILE", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".alert_rules.json"))
        self.history_collection: str = _env("HISTORY_COLLECTION", "opsmind_daily")
        self.history_events_collection: str = _env(
            "HISTORY_EVENTS_COLLECTION", "opsmind_incidents")
        self.history_write_interval_s: float = _env_float(
            "HISTORY_WRITE_INTERVAL_S", 300.0)
        # Which clock decides where "today" ends. UTC would roll the day over
        # at 05:30 local for an asia-south1 deployment, so "yesterday" would
        # not mean what the person reading the dashboard means. Default is IST.
        self.history_tz_offset_minutes: int = _env_int(
            "HISTORY_TZ_OFFSET_MINUTES", 330)

        # The authoritative cost tier: Cloud Billing export in BigQuery.
        # Set the dataset once the export is actually writing; the table name
        # carries the billing account id and is discovered rather than typed.
        self.billing_export_dataset: str = _env("BILLING_EXPORT_DATASET", "")
        self.billing_export_table: str = _env("BILLING_EXPORT_TABLE", "")
        self.billing_query_timeout_s: float = _env_float(
            "BILLING_QUERY_TIMEOUT_S", 25.0)

        # Optional shared-secret gate for the dashboard. The platform holds
        # read access to your logs, so do not leave it open on a public URL
        # without this set. See docs/DEPLOY.md step 8.
        self.dashboard_token: str = _env("DASHBOARD_TOKEN", "")

        # User accounts. Cloud Run scales to zero and forgets everything held
        # in memory, so a deployed portal keeps accounts in Firestore; a local
        # one keeps them in memory and says so on the account page.
        self.accounts_backend: str = _env(
            "ACCOUNTS_BACKEND",
            "firestore" if self.data_source == "gcp" else "memory").lower()
        self.accounts_collection: str = _env("ACCOUNTS_COLLECTION", "opsmind_users")
        # Signs session cookies. When unset it is derived from DASHBOARD_TOKEN,
        # which every instance already shares, so sessions survive a restart
        # and work across instances without another variable to set.
        self.session_secret: str = _env("SESSION_SECRET", "")

        self.pricing: Dict = self._load_pricing()
        self.inr_per_usd: float = float(
            self.pricing.get("indicativeInrPerUsd", 83.0)
        )

    def _load_pricing(self) -> Dict:
        with open(os.path.join(_HERE, "pricing", "skus.json")) as fh:
            return json.load(fh)

    @property
    def run_prices(self) -> Dict:
        return self.pricing["cloudRun"][self.billing_model]

    def shape(self, service: str) -> Dict[str, float]:
        return self.service_shape.get(
            service, {"vcpu": 1.0, "memoryGib": 0.5, "maxInstances": 5}
        )

    def set_active_project(self, project_id: str) -> str:
        """Point OpsMind at a different project.

        Callers are responsible for clearing the working set and resetting the
        collectors' cursors: the buffered entries belong to the project we are
        leaving, and showing them under a different project's name would be a
        lie that is very hard to spot.
        """
        self.active_project = (project_id or "").strip() or self.project_id
        return self.active_project

    def as_dict(self) -> Dict[str, object]:
        return {
            "dataSource": self.data_source,
            "projectId": self.project_id or None,
            "activeProject": self.active_project or None,
            "region": self.region,
            "watchedServices": self.watched_services,
            "billingModel": self.billing_model,
            "aiEnabled": self.ai_enabled,
            "aiModel": self.ai_model if self.ai_enabled else None,
            "pricingVerifiedOn": self.pricing.get("verifiedOn"),
            "logsPollIntervalS": self.logs_poll_interval_s,
            "metricsPollIntervalS": self.metrics_poll_interval_s,
            "historyEnabled": self.history_enabled,
            "billingExportDataset": self.billing_export_dataset or None,
            "accountsBackend": self.accounts_backend,
        }


settings = Settings()
