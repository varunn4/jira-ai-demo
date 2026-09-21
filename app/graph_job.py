"""In-memory job state tracking for graph build operations.

Jobs are stored in a module-level JobStore (last 50 kept). Each job tracks
action, status, per-step totals, and live progress counters that the
background runner updates as work proceeds.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class GraphJob:
    job_id: str
    action: str
    status: str  # "pending" | "running" | "completed" | "failed"
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    totals: dict[str, int] = field(default_factory=lambda: {"repositories": 0, "jira_tickets": 0})
    progress: dict[str, int] = field(default_factory=lambda: {"repositories_done": 0, "jira_tickets_done": 0})
    logs: list[dict[str, Any]] = field(default_factory=list)
    # Free-form metadata (e.g. the codebase embedding model in use) that doesn't
    # fit the integer-valued totals/progress counters.
    meta: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "status": self.status,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "totals": dict(self.totals),
            "progress": dict(self.progress),
            "logs": list(self.logs),
            "meta": dict(self.meta),
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    def mark_done(self) -> None:
        self.status = "completed"
        self.completed_at = datetime.now(timezone.utc)
        log.info("Job %s completed", self.job_id)

    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.error = reason[:1000]
        self.completed_at = datetime.now(timezone.utc)
        log.error("Job %s failed: %s", self.job_id, reason[:200])


class JobStore:
    """Thread-safe in-memory store for recent graph jobs (capped at max_jobs)."""

    def __init__(self, max_jobs: int = 50) -> None:
        self._jobs: dict[str, GraphJob] = {}
        self._order: list[str] = []
        self._max = max_jobs

    def create(self, action: str, user_id: Optional[int] = None, user_email: Optional[str] = None) -> GraphJob:
        job_id = str(uuid.uuid4())
        job = GraphJob(job_id=job_id, action=action, status="pending", user_id=user_id, user_email=user_email)
        self._jobs[job_id] = job
        self._order.append(job_id)
        if len(self._order) > self._max:
            oldest = self._order.pop(0)
            self._jobs.pop(oldest, None)
        log.info("Created graph job %s (action=%s, user_id=%s)", job_id, action, user_id)
        return job

    def get(self, job_id: str) -> Optional[GraphJob]:
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 10, user_id: Optional[int] = None) -> list[GraphJob]:
        ids = self._order[-limit * 2:][::-1]
        results: list[GraphJob] = []
        for j in ids:
            job = self._jobs.get(j)
            if job is None:
                continue
            if user_id is not None and job.user_id is not None and job.user_id != user_id:
                continue
            results.append(job)
            if len(results) >= limit:
                break
        return results


# Module-level singleton used by api.py and graph_job_runner.py
job_store = JobStore()
