"""In-process job store for async API endpoints.

Jobs run in a ThreadPoolExecutor; their state is persisted to a JSON file so
that status survives a service restart (the job itself won't resume, but the
caller can still see what happened to it). Adequate for a single-worker
internal service. If you ever need multi-worker durability, swap the storage
backend — the JobStore interface stays the same.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional

log = logging.getLogger(__name__)


JobStatus = Literal["pending", "running", "succeeded", "failed"]


@dataclass
class Job:
    id: str
    kind: str                              # "initial_scan" | "nightly_sync"
    status: JobStatus = "pending"
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"))
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None     # structured summary on success
    error: Optional[str] = None
    progress: Optional[str] = None              # human-readable progress line


class JobStore:
    """Thread-safe job registry. Persists to disk after every state change.

    `submit()` queues work on an internal ThreadPoolExecutor. The store keeps
    a reference to the Future so callers can cancel later if needed (not yet
    exposed — add only if you find you need it).
    """

    def __init__(self, path: Path, max_workers: int = 2):
        self.path = path
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._futures: Dict[str, Future] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except Exception as e:
            log.warning("Job store at %s unreadable (%s) — starting fresh", self.path, e)
            return
        for jid, data in raw.items():
            self._jobs[jid] = Job(**data)
        # On startup, mark any jobs that were "running" as failed — they
        # didn't survive the restart and we can't resume them.
        for job in self._jobs.values():
            if job.status == "running":
                job.status = "failed"
                job.error = "service restarted while job was running"
                job.finished_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump({jid: asdict(j) for jid, j in self._jobs.items()}, f, indent=2)
        tmp.replace(self.path)

    def submit(self, kind: str, target: Callable[["JobHandle"], Dict[str, Any]]) -> Job:
        """Create a job and start it on the executor.

        `target` receives a JobHandle that lets it update progress and is
        expected to return a dict (the result payload). Exceptions are caught
        and recorded as job failures.
        """
        job_id = str(uuid.uuid4())
        with self._lock:
            job = Job(id=job_id, kind=kind)
            self._jobs[job_id] = job
            self._flush()

        handle = JobHandle(self, job_id)
        future = self._executor.submit(self._run, handle, target)
        self._futures[job_id] = future
        return job

    def _run(self, handle: "JobHandle", target: Callable[["JobHandle"], Dict[str, Any]]) -> None:
        """Internal wrapper: mark running, call target, capture result/error."""
        handle._set_running()
        try:
            result = target(handle)
            handle._set_succeeded(result)
        except Exception as e:
            log.exception("Job %s (%s) failed", handle.job_id, handle._job().kind)
            handle._set_failed(str(e))

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            j = self._jobs.get(job_id)
            # Return a copy so callers can't mutate our internal state
            return Job(**asdict(j)) if j else None

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [Job(**asdict(j)) for j in jobs[:limit]]

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for k, v in fields.items():
                setattr(job, k, v)
            self._flush()


@dataclass
class JobHandle:
    """Passed to the target function so it can report progress."""
    store: JobStore
    job_id: str

    def _job(self) -> Job:
        return self.store._jobs[self.job_id]

    def progress(self, message: str) -> None:
        log.info("[job %s] %s", self.job_id[:8], message)
        self.store._update(self.job_id, progress=message)

    def _set_running(self) -> None:
        self.store._update(
            self.job_id,
            status="running",
            started_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def _set_succeeded(self, result: Dict[str, Any]) -> None:
        self.store._update(
            self.job_id,
            status="succeeded",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            result=result,
            progress=None,
        )

    def _set_failed(self, error: str) -> None:
        self.store._update(
            self.job_id,
            status="failed",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            error=error,
            progress=None,
        )