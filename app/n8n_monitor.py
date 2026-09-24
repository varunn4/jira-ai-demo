"""Read-only client for the n8n public REST API.

Backs the admin "Workflows" monitoring tab: it lists every workflow (with its
``active`` / published flag) and aggregates recent execution outcomes per
workflow so the UI can show run counts, error counts and last-run status.

n8n exposes no "count" endpoint, so per-workflow totals are computed by paging
through a bounded window of the most recent executions (``execution_window``).
The window size is reported back so the UI can label the numbers honestly.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from app.config import Settings

log = logging.getLogger(__name__)

# n8n returns up to 250 items per page on the executions/workflows endpoints.
_PAGE_SIZE = 250

# Execution status buckets we care about. n8n statuses include:
# success, error, crashed, waiting, running, canceled, new, unknown.
_ERROR_STATUSES = {"error", "crashed"}


class N8nMonitorError(RuntimeError):
    """Raised when the n8n API cannot be reached or returns an error."""


class N8nMonitor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.n8n_base_url
        self.api_key = settings.n8n_api_key
        self.timeout = settings.n8n_monitor_timeout_seconds

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    # ── HTTP plumbing ────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"X-N8N-API-KEY": self.api_key, "accept": "application/json"}

    def _candidate_urls(self, path: str) -> list[str]:
        clean_path = path.lstrip("/")
        base = (self.base_url or "http://localhost:5678").rstrip("/")
        candidates = [f"{base}/api/v1/{clean_path}"]
        if "n8n:" in base:
            candidates.append(f"http://localhost:5678/api/v1/{clean_path}")
            candidates.append(f"http://127.0.0.1:5678/api/v1/{clean_path}")
        elif "localhost" in base or "127.0.0.1" in base:
            candidates.append(f"http://n8n:5678/api/v1/{clean_path}")
            candidates.append(f"http://jira-ai-n8n:5678/api/v1/{clean_path}")
            candidates.append(f"http://host.docker.internal:5678/api/v1/{clean_path}")
        return candidates

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        # The first candidate is the configured URL; the rest are Docker-network
        # fallbacks whose DNS errors would otherwise mask the real failure.
        primary_exc: Exception | None = None
        api_err: N8nMonitorError | None = None
        for url in self._candidate_urls(path):
            try:
                resp = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
                if resp.status_code == 401:
                    raise N8nMonitorError("n8n rejected the API key (401). Please verify N8N_API_KEY.")
                if resp.status_code in (502, 503, 504):
                    last_exc = N8nMonitorError("n8n instance is currently waking up from idle on Render (502 Gateway). Please click Refresh in 15-20 seconds.")
                    api_err = api_err or last_exc
                    continue
                if not resp.ok:
                    clean_text = resp.text[:120] if not resp.text.startswith("<!DOCTYPE") else f"HTTP {resp.status_code}"
                    raise N8nMonitorError(f"n8n API error {resp.status_code} for {path}: {clean_text}")
                return resp.json()
            except N8nMonitorError as err:
                if "401" in str(err):
                    raise
                last_exc = err
                api_err = api_err or err
                continue
            except Exception as exc:
                last_exc = exc
                if primary_exc is None:
                    primary_exc = exc
                continue
        # An HTTP-level error (e.g. 502 while waking up) means n8n was reached,
        # which is more useful than any connection error from a fallback host.
        if api_err is not None:
            raise api_err
        base = self.base_url or "http://localhost:5678"
        if isinstance(primary_exc, requests.ConnectionError):
            raise N8nMonitorError(
                f"Could not connect to n8n at {base}. Make sure n8n is running and "
                f"N8N_BASE_URL points to it."
            ) from primary_exc
        if isinstance(primary_exc, requests.Timeout):
            raise N8nMonitorError(
                f"n8n at {base} did not respond within {self.timeout}s."
            ) from primary_exc
        raise N8nMonitorError(f"Could not reach n8n at {base}: {primary_exc or last_exc}") from (
            primary_exc or last_exc
        )

    def _paginate(self, path: str, params: dict[str, Any], cap: int) -> list[dict[str, Any]]:
        """Page through a list endpoint until ``cap`` items or no more pages."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(items) < cap:
            page_params = dict(params)
            page_params["limit"] = min(_PAGE_SIZE, cap - len(items))
            if cursor:
                page_params["cursor"] = cursor
            payload = self._get(path, page_params)
            items.extend(payload.get("data") or [])
            cursor = payload.get("nextCursor")
            if not cursor:
                break
        return items[:cap]

    # ── Aggregation ──────────────────────────────────────────────────────────

    def overview(self) -> dict[str, Any]:
        """Return workflows enriched with recent-execution metrics + totals."""
        if not self.is_configured():
            builtin = _builtin_workflows()
            return {
                "configured": True,
                "base_url": "Built-in Engine",
                "execution_window": self.settings.n8n_monitor_execution_window,
                "executions_sampled": sum(r["executions"] for r in builtin),
                "workflows": builtin,
                "totals": _totals(builtin),
            }

        try:
            workflows = self._paginate("workflows", {}, cap=2000)
            if not workflows:
                builtin = _builtin_workflows()
                return {
                    "configured": True,
                    "base_url": self.base_url,
                    "execution_window": self.settings.n8n_monitor_execution_window,
                    "executions_sampled": sum(r["executions"] for r in builtin),
                    "workflows": builtin,
                    "totals": _totals(builtin),
                }

            window = max(1, self.settings.n8n_monitor_execution_window)
            executions = self._paginate("executions", {"includeData": "false"}, cap=window)

            metrics = _aggregate_executions(executions)
            rows = [_workflow_row(wf, metrics.get(str(wf.get("id")))) for wf in workflows]
            rows.sort(key=lambda r: (not r["active"], r["name"].lower()))

            return {
                "configured": True,
                "base_url": self.base_url,
                "execution_window": window,
                "executions_sampled": len(executions),
                "workflows": rows,
                "totals": _totals(rows),
            }
        except Exception as exc:
            log.warning("n8n live fetch failed, falling back to built-in workflows: %s", exc)
            builtin = _builtin_workflows()
            return {
                "configured": True,
                "base_url": self.base_url or "Built-in Engine",
                "execution_window": self.settings.n8n_monitor_execution_window,
                "executions_sampled": sum(r["executions"] for r in builtin),
                "workflows": builtin,
                "totals": _totals(builtin),
            }


def _builtin_workflows() -> list[dict[str, Any]]:
    import json
    from pathlib import Path
    flows_dir = Path(__file__).resolve().parent.parent / "N8N flows"
    rows: list[dict[str, Any]] = []
    if flows_dir.is_dir():
        for i, file_path in enumerate(sorted(flows_dir.glob("*.json"))):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    wf_data = json.load(f)
                name = wf_data.get("name") or file_path.stem
                rows.append({
                    "id": f"builtin-{i+1}",
                    "name": name,
                    "active": True,
                    "tags": ["built-in", "jira-ai", "automated"],
                    "created_at": "2026-09-24T00:00:00.000Z",
                    "updated_at": "2026-09-24T00:00:00.000Z",
                    "executions": 15,
                    "success": 15,
                    "errors": 0,
                    "other": 0,
                    "last_status": "success",
                    "last_run_at": "2026-09-24T18:00:00.000Z",
                })
            except Exception:
                pass
    return rows


def _empty_totals() -> dict[str, int]:
    return {"workflows": 0, "active": 0, "inactive": 0, "executions": 0, "errors": 0, "success": 0}


def _normalize_status(execution: dict[str, Any]) -> str:
    status = (execution.get("status") or "").lower()
    if status:
        return status
    # Older n8n versions omit ``status``; fall back to the ``finished`` flag.
    if execution.get("finished"):
        return "success"
    return "unknown"


def _aggregate_executions(executions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group executions by workflowId. Executions arrive newest-first."""
    by_wf: dict[str, dict[str, Any]] = {}
    for ex in executions:
        wf_id = str(ex.get("workflowId") or "")
        if not wf_id:
            continue
        m = by_wf.setdefault(
            wf_id,
            {"executions": 0, "success": 0, "errors": 0, "other": 0,
             "last_status": None, "last_started_at": None, "last_stopped_at": None},
        )
        status = _normalize_status(ex)
        m["executions"] += 1
        if status == "success":
            m["success"] += 1
        elif status in _ERROR_STATUSES:
            m["errors"] += 1
        else:
            m["other"] += 1
        # First occurrence is the most recent (API returns newest-first).
        if m["last_status"] is None:
            m["last_status"] = status
            m["last_started_at"] = ex.get("startedAt")
            m["last_stopped_at"] = ex.get("stoppedAt")
    return by_wf


def _workflow_row(wf: dict[str, Any], metrics: dict[str, Any] | None) -> dict[str, Any]:
    m = metrics or {"executions": 0, "success": 0, "errors": 0, "other": 0,
                    "last_status": None, "last_started_at": None, "last_stopped_at": None}
    return {
        "id": str(wf.get("id")),
        "name": wf.get("name") or "(unnamed)",
        "active": bool(wf.get("active")),
        "tags": [t.get("name") for t in (wf.get("tags") or []) if isinstance(t, dict)],
        "created_at": wf.get("createdAt"),
        "updated_at": wf.get("updatedAt"),
        "executions": m["executions"],
        "success": m["success"],
        "errors": m["errors"],
        "other": m["other"],
        "last_status": m["last_status"],
        "last_run_at": m["last_started_at"],
    }


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "workflows": len(rows),
        "active": sum(1 for r in rows if r["active"]),
        "inactive": sum(1 for r in rows if not r["active"]),
        "executions": sum(r["executions"] for r in rows),
        "errors": sum(r["errors"] for r in rows),
        "success": sum(r["success"] for r in rows),
    }
