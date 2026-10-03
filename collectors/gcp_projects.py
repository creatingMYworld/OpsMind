"""Discovering which Google Cloud projects this installation can actually see.

Two different permissions are involved, and conflating them produces a picker
that lists projects it cannot read:

  resourcemanager.projects.get  -- lets the service account SEE a project.
      Granted in practice by roles/browser, usually at the organization level
      so one binding covers every project.
  logging.viewer                -- lets it READ that project's logs. Granted
      per project, because log access is exactly the thing you would not want
      to hand out organization-wide by default.

So every project is listed with its real connection state. A project without
logging access is not hidden and is not an error: it is shown with the binding
needed to connect it, which is more useful than pretending it is not there.

Probe results are cached, because asking Cloud Logging about every project on
every page load is a lot of API calls to learn something that changes rarely.
"""
import threading
import time
from typing import Any, Dict, List, Optional

from ..config import settings

_PROBE_TTL_S = 300.0
_LIST_TTL_S = 120.0


class ProjectDirectory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client = None
        self._projects: List[Dict[str, Any]] = []
        self._listed_at: float = 0.0
        self._probe: Dict[str, Dict[str, Any]] = {}
        self.last_error: Optional[str] = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from google.cloud import resourcemanager_v3
            self._client = resourcemanager_v3.ProjectsClient()
        return self._client

    # --- listing -----------------------------------------------------------
    def list_projects(self, refresh: bool = False) -> Dict[str, Any]:
        if settings.data_source != "gcp":
            return {
                "projects": [{
                    "projectId": settings.project_id or "local",
                    "displayName": "Local ingest",
                    "state": "ACTIVE",
                    "connected": True,
                    "active": True,
                    "source": "local",
                    "note": "CogniKart is posting its log lines directly to this "
                            "instance. Listing real Google Cloud projects needs "
                            "DATA_SOURCE=gcp.",
                }],
                "mode": "local",
                "canList": False,
                "note": "Running in local mode. Deploy with DATA_SOURCE=gcp to "
                        "discover the projects this service account can see.",
            }

        with self._lock:
            fresh = (time.time() - self._listed_at) < _LIST_TTL_S
            if self._projects and fresh and not refresh:
                return self._decorate(self._projects)

        try:
            client = self._ensure_client()
            found = []
            # An empty query returns every project the caller may view, which
            # is precisely the set worth offering.
            for p in client.search_projects(query="state:ACTIVE"):
                found.append({
                    "projectId": p.project_id,
                    "displayName": p.display_name or p.project_id,
                    "state": p.state.name if hasattr(p.state, "name") else str(p.state),
                    "parent": p.parent or None,
                    "labels": dict(p.labels or {}),
                    "createdAt": p.create_time.timestamp() if p.create_time else None,
                })
            found.sort(key=lambda x: x["displayName"].lower())
            with self._lock:
                self._projects = found
                self._listed_at = time.time()
                self.last_error = None
            return self._decorate(found)
        except Exception as exc:  # noqa: BLE001 -- surfaced, never swallowed
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            with self._lock:
                existing = list(self._projects)
            data = self._decorate(existing)
            data["canList"] = False
            data["error"] = self.last_error
            data["howToFix"] = (
                "Listing projects needs resourcemanager.projects.get. Grant "
                "roles/browser to this service account, ideally at the "
                "organization level so one binding covers every project: "
                "gcloud organizations add-iam-policy-binding ORG_ID "
                "--member serviceAccount:%s --role roles/browser"
                % (settings.service_account_hint or "SERVICE_ACCOUNT_EMAIL")
            )
            return data

    def _decorate(self, projects: List[Dict[str, Any]]) -> Dict[str, Any]:
        out = []
        for p in projects:
            probe = self._probe.get(p["projectId"], {})
            out.append(dict(
                p,
                connected=probe.get("connected"),
                checkedAt=probe.get("at"),
                reason=probe.get("reason"),
                active=(p["projectId"] == settings.active_project),
            ))
        return {
            "projects": out,
            "mode": "gcp",
            "canList": True,
            "activeProject": settings.active_project,
            "note": "Listing a project and being able to read its logs are "
                    "different permissions. Use Check access to confirm.",
        }

    # --- access probe ------------------------------------------------------
    def check_access(self, project_id: str, force: bool = False) -> Dict[str, Any]:
        """Can we actually read this project's logs?

        A single, tiny Cloud Logging read. Cheap, conclusive, and far better
        than inferring access from an IAM policy we may not be allowed to read
        either.
        """
        cached = self._probe.get(project_id)
        if cached and not force and (time.time() - cached["at"]) < _PROBE_TTL_S:
            return cached

        result = {"projectId": project_id, "at": time.time()}
        try:
            from google.cloud import logging as gcl
            client = gcl.Client(project=project_id)
            entries = client.list_entries(
                resource_names=["projects/%s" % project_id],
                filter_='timestamp>="%s"' % time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)),
                max_results=1, page_size=1,
            )
            count = sum(1 for _ in entries)
            result.update({
                "connected": True,
                "hasRecentLogs": count > 0,
                "reason": "Readable." if count else
                          "Readable, but nothing logged in the last hour.",
            })
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            denied = "403" in msg or "permission" in msg.lower()
            result.update({
                "connected": False,
                "hasRecentLogs": False,
                "reason": ("OpsMind cannot read this project's logs."
                           if denied else "Could not reach Cloud Logging: %s"
                           % msg[:160]),
                "howToFix": (
                    "gcloud projects add-iam-policy-binding %s "
                    "--member serviceAccount:%s --role roles/logging.viewer"
                    % (project_id, settings.service_account_hint
                       or "SERVICE_ACCOUNT_EMAIL")) if denied else None,
            })
        self._probe[project_id] = result
        return result

    def status(self) -> Dict[str, Any]:
        return {
            "listedProjects": len(self._projects),
            "probed": len(self._probe),
            "lastError": self.last_error,
            "activeProject": settings.active_project,
        }


directory = ProjectDirectory()
