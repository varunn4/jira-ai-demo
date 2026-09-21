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

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise N8nMonitorError(f"Could not reach n8n at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise N8nMonitorError("n8n rejected the API key (401). Check N8N_API_KEY.")
        if not resp.ok:
            raise N8nMonitorError(f"n8n API error {resp.status_code} for {path}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise N8nMonitorError(f"n8n returned a non-JSON response for {path}") from exc

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
            return {
                "configured": False,
                "base_url": self.base_url or None,
                "execution_window": self.settings.n8n_monitor_execution_window,
                "workflows": [],
                "totals": _empty_totals(),
            }

        workflows = self._paginate("workflows", {}, cap=2000)
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
