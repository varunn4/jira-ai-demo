"""Phase H — persistence for RCA runs (`rca_runs`), JobStore-style.

Durable run state in Postgres plus an in-memory mirror for live progress while a
run is investigating. Mirrors the existing per-module Postgres store pattern
(see app/conversation_store.py).
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import Settings

log = logging.getLogger(__name__)

# Lifecycle states for a run.
STATUS_QUEUED = "queued"
STATUS_INVESTIGATING = "investigating"
STATUS_SYNTHESIZING = "synthesizing"
STATUS_DELIVERED = "delivered"
STATUS_LOW_CONFIDENCE = "low_confidence"
STATUS_FAILED = "failed"


@dataclass
class RCARun:
    run_id: str
    jira_key: str
    status: str = STATUS_QUEUED
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    localized_repos: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: Optional[dict[str, Any]] = None
    confidence: Optional[float] = None
    agent_trace: list[dict[str, Any]] = field(default_factory=list)
    document: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d


class RCARunStore:
    """Postgres-backed run store with a thread-safe in-memory live mirror."""

    def __init__(self, settings: Settings, max_live: int = 50) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._live: dict[str, RCARun] = {}
        self._max_live = max_live

    # ── schema ────────────────────────────────────────────────────────────────

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.settings.database_url, row_factory=dict_row)

    def init_schema(self) -> None:
        if not self.settings.database_url:
            return
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rca_runs (
                    run_id          TEXT PRIMARY KEY,
                    user_id         INTEGER,
                    user_email      TEXT,
                    jira_key        TEXT,
                    status          TEXT NOT NULL,
                    localized_repos JSONB NOT NULL DEFAULT '[]'::jsonb,
                    candidates      JSONB NOT NULL DEFAULT '[]'::jsonb,
                    diagnosis       JSONB,
                    confidence      REAL,
                    agent_trace     JSONB NOT NULL DEFAULT '[]'::jsonb,
                    document        JSONB,
                    error           TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS user_id INTEGER;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS user_email TEXT;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS jira_key TEXT;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS localized_repos JSONB NOT NULL DEFAULT '[]'::jsonb;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS candidates JSONB NOT NULL DEFAULT '[]'::jsonb;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS diagnosis JSONB;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS confidence REAL;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS agent_trace JSONB NOT NULL DEFAULT '[]'::jsonb;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS document JSONB;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS error TEXT;")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
            conn.execute("ALTER TABLE rca_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
            try:
                conn.execute("ALTER TABLE rca_runs ALTER COLUMN ticket_key DROP NOT NULL;")
                conn.execute("UPDATE rca_runs SET jira_key = ticket_key WHERE jira_key IS NULL;")
            except Exception:
                pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rca_runs_key ON rca_runs (jira_key)"
            )
            conn.commit()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def create(self, jira_key: str, user_id: Optional[int] = None, user_email: Optional[str] = None) -> RCARun:
        self.init_schema()
        run = RCARun(
            run_id=str(uuid.uuid4()),
            jira_key=jira_key,
            status=STATUS_QUEUED,
            user_id=user_id,
            user_email=user_email,
        )
        with self._lock:
            self._live[run.run_id] = run
            if len(self._live) > self._max_live:
                # Evict oldest live entry from in-memory dictionary to bound memory
                oldest_key = min(self._live.keys(), key=lambda k: self._live[k].created_at)
                self._live.pop(oldest_key, None)
        self._persist(run)
        log.info("RCA run %s created for %s (user_id=%s)", run.run_id, jira_key, user_id)
        return run

    def touch(self, run: RCARun, *, status: Optional[str] = None) -> None:
        if status:
            run.status = status
        run.updated_at = datetime.now(timezone.utc)
        with self._lock:
            self._live[run.run_id] = run
        self._persist(run)

    def add_trace_event(self, run: RCARun, event: dict[str, Any]) -> None:
        run.agent_trace.append(event)
        run.updated_at = datetime.now(timezone.utc)
        with self._lock:
            self._live[run.run_id] = run

    def get(self, run_id: str) -> Optional[RCARun]:
        with self._lock:
            if run_id in self._live:
                return self._live[run_id]
        return self._load(run_id)

    def list_recent(self, limit: int = 25, user_id: Optional[int] = None, is_admin: bool = False) -> list[dict[str, Any]]:
        if not self.settings.database_url:
            with self._lock:
                runs = sorted(self._live.values(), key=lambda r: r.created_at, reverse=True)
                if not is_admin and user_id is not None:
                    runs = [r for r in runs if r.user_id == user_id]
            return [self._summary(r) for r in runs[:limit]]
        with self._connect() as conn:
            if is_admin or user_id is None:
                rows = conn.execute(
                    "SELECT run_id, jira_key, status, confidence, created_at, updated_at "
                    "FROM rca_runs ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, jira_key, status, confidence, created_at, updated_at "
                    "FROM rca_runs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    # ── persistence ───────────────────────────────────────────────────────────

    def _persist(self, run: RCARun) -> None:
        if not self.settings.database_url:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rca_runs (run_id, user_id, user_email, jira_key, status, localized_repos,
                    candidates, diagnosis, confidence, agent_trace, document, error,
                    created_at, updated_at)
                VALUES (%(run_id)s, %(user_id)s, %(user_email)s, %(jira_key)s, %(status)s, %(localized_repos)s::jsonb,
                    %(candidates)s::jsonb, %(diagnosis)s::jsonb, %(confidence)s,
                    %(agent_trace)s::jsonb, %(document)s::jsonb, %(error)s, %(created_at)s,
                    %(updated_at)s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    localized_repos = EXCLUDED.localized_repos,
                    candidates = EXCLUDED.candidates,
                    diagnosis = EXCLUDED.diagnosis,
                    confidence = EXCLUDED.confidence,
                    agent_trace = EXCLUDED.agent_trace,
                    document = EXCLUDED.document,
                    error = EXCLUDED.error,
                    updated_at = EXCLUDED.updated_at
                """,
                {
                    "run_id": run.run_id, "user_id": run.user_id, "user_email": run.user_email,
                    "jira_key": run.jira_key, "status": run.status,
                    "localized_repos": json.dumps(run.localized_repos),
                    "candidates": json.dumps(run.candidates),
                    "diagnosis": json.dumps(run.diagnosis) if run.diagnosis else None,
                    "confidence": run.confidence,
                    "agent_trace": json.dumps(run.agent_trace),
                    "document": json.dumps(run.document) if run.document else None,
                    "error": run.error,
                    "created_at": run.created_at, "updated_at": run.updated_at,
                },
            )

    def _load(self, run_id: str) -> Optional[RCARun]:
        if not self.settings.database_url:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rca_runs WHERE run_id = %s", (run_id,)).fetchone()
        if not row:
            return None
        return RCARun(
            run_id=row["run_id"], jira_key=row["jira_key"], status=row["status"],
            localized_repos=row["localized_repos"] or [], candidates=row["candidates"] or [],
            diagnosis=row["diagnosis"], confidence=row["confidence"],
            agent_trace=row["agent_trace"] or [], document=row["document"],
            error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _summary(run: RCARun) -> dict[str, Any]:
        return {
            "run_id": run.run_id, "jira_key": run.jira_key, "status": run.status,
            "confidence": run.confidence, "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }
