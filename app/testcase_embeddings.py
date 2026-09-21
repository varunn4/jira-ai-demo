"""Embed generated test cases into Qdrant for the regression flag.

A new incoming Jira ticket is compared (in app/testcase_regression_finder.py)
against these vectors: if its summary/description is semantically close to an
existing test case, the ticket is flagged as a possible regression — it appears
to re-open a scenario we already have a (passing baseline) test case for.

Two entry points:
  * build_testcase_embeddings(settings)                 – backfill / rebuild all
  * embed_ticket_testcases(settings, jira_ticket_id)    – one ticket, on write

Both are best-effort: they degrade quietly when Ollama / Qdrant are unavailable
so the test-case write path is never blocked.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Optional

from app.config import Settings
from app.ollama_embedder import OllamaEmbedder
from app.qdrant_store import (
    TESTCASE_COLLECTION,
    delete_testcase_embeddings,
    upsert_testcase_embeddings,
)

log = logging.getLogger(__name__)


def _steps_to_list(value: Any) -> list[str]:
    """Normalise a `steps` column value (json list, json string, or text) to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [str(parsed)]
    return [str(value)]


def testcase_embed_text(title: str, steps: list[str], expected: str) -> str:
    """Canonical text used to embed a test case. Kept in one place so ingest and
    any future re-ingest stay consistent."""
    steps_text = "\n".join(steps)
    return f"{(title or '')[:200]}\n{steps_text[:600]}\n{(expected or '')[:300]}".strip()


def _load_rows(
    settings: Settings,
    jira_ticket_id: Optional[str] = None,
    phase: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not settings.database_url:
        return []
    import psycopg
    from psycopg.rows import dict_row

    where: list[str] = []
    params: list[Any] = []
    if jira_ticket_id:
        where.append("tc.jira_ticket_id = %s")
        params.append(jira_ticket_id)
    if phase:
        where.append("tc.phase = %s")
        params.append(phase)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT tc.jira_ticket_id, tc.phase, tc.tc_index, tc.title,
                   tc.steps, tc.expected, tc.status,
                   t.jira_payload
            FROM test_cases tc
            LEFT JOIN tickets t ON t.jira_ticket_id = tc.jira_ticket_id
            {where_sql}
            ORDER BY tc.jira_ticket_id, tc.phase, tc.tc_index
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def build_testcase_embeddings(
    settings: Settings,
    jira_ticket_id: Optional[str] = None,
    phase: Optional[str] = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Embed all test cases (or one ticket's / one phase's) into Qdrant.

    Returns a summary dict: {rows, embedded, stored, method}.
    """
    rows = _load_rows(settings, jira_ticket_id, phase)
    if not rows:
        return {"rows": 0, "embedded": 0, "stored": 0, "method": "none"}

    embedder = OllamaEmbedder(
        base_url=settings.ollama_url,
        model=settings.ollama_embed_model,
        timeout_seconds=settings.ollama_embed_timeout_seconds,
    )
    if not embedder.is_available():
        log.warning("Ollama unavailable — cannot embed test cases")
        return {"rows": len(rows), "embedded": 0, "stored": 0, "method": "unavailable"}

    texts = [
        testcase_embed_text(r.get("title") or "", _steps_to_list(r.get("steps")), r.get("expected") or "")
        for r in rows
    ]
    vectors = embedder.embed_batch(texts, progress_callback=progress_callback)

    payloads: list[dict[str, Any]] = []
    for r in rows:
        summary = ""
        payload = r.get("jira_payload") or {}
        if isinstance(payload, dict):
            summary = str(payload.get("summary") or "")
        payloads.append(
            {
                "jira_ticket_id": r.get("jira_ticket_id"),
                "phase": r.get("phase") or "qa",
                "tc_index": r.get("tc_index"),
                "title": r.get("title") or "",
                "status": r.get("status") or "",
                "ticket_summary": summary,
            }
        )

    stored = upsert_testcase_embeddings(
        settings.qdrant_url, payloads, vectors, settings.qdrant_api_key
    )
    embedded = sum(1 for v in vectors if v is not None)
    if stored:
        try:
            from app.embedding_status import record_embedding_update

            # Only a full rebuild knows the true total point count; a per-ticket
            # embed just refreshes the timestamp so it can't clobber the total.
            is_full_build = jira_ticket_id is None and phase is None
            record_embedding_update(
                settings, TESTCASE_COLLECTION, stored if is_full_build else None
            )
        except Exception as exc:  # pragma: no cover - bookkeeping only
            log.debug("record_embedding_update(test_cases) failed: %s", exc)
    log.info(
        "Test-case embeddings: rows=%d embedded=%d stored=%d ticket=%s phase=%s",
        len(rows), embedded, stored, jira_ticket_id, phase,
    )
    return {"rows": len(rows), "embedded": embedded, "stored": stored, "method": "semantic"}


def run_testcase_embedding_job(job: Any, settings: Settings) -> None:
    """Background runner for the admin "Build test-case embeddings" button.

    Drives GraphJob status + progress counters so the live Embeddings panel can
    show a progress bar. Never raises — records failure on the job instead.
    """
    from datetime import datetime, timezone

    def _log(level: str, message: str) -> None:
        getattr(log, level, log.info)("Testcase-embed job %s: %s", getattr(job, "job_id", "?"), message)
        try:
            job.logs.append(
                {"step": "testcase_embeddings", "level": level, "message": message,
                 "at": datetime.now(timezone.utc).isoformat()}
            )
        except Exception:  # pragma: no cover
            pass

    try:
        job.status = "running"
        _log("info", "Embedding all generated test cases…")

        def _cb(done: int, total: int) -> None:
            job.totals["testcase_embedding_documents"] = total
            job.progress["testcase_embedding_documents_done"] = done

        result = build_testcase_embeddings(settings, progress_callback=_cb)
        job.meta["result"] = result

        if result["method"] in {"none", "unavailable"}:
            job.mark_failed(
                f"Embeddings were not stored (method={result['method']}). "
                "Check that Ollama and Qdrant are reachable and DATABASE_URL is set."
            )
            return
        if result["rows"] == 0:
            _log("info", "No test cases found to embed.")
        _log(
            "info",
            f"Embedded {result['embedded']} of {result['rows']} test cases "
            f"(stored {result['stored']}).",
        )
        job.mark_done()
    except Exception as exc:  # pragma: no cover - defensive
        job.mark_failed(str(exc))


def embed_ticket_testcases(
    settings: Settings, jira_ticket_id: str, phase: Optional[str] = None
) -> None:
    """Best-effort: (re)embed a single ticket's test cases after they are written.

    Never raises — the test-case write path must not fail because embedding did.
    Also prunes stale vectors when the ticket's case count shrank.
    """
    try:
        rows = _load_rows(settings, jira_ticket_id, phase)
        result = build_testcase_embeddings(settings, jira_ticket_id, phase)
        # Drop vectors for indexes that no longer exist (case count decreased).
        by_phase: dict[str, list[int]] = {}
        for r in rows:
            idx = r.get("tc_index")
            if idx is not None:
                by_phase.setdefault(r.get("phase") or "qa", []).append(int(idx))
        for ph, keep in by_phase.items():
            delete_testcase_embeddings(
                settings.qdrant_url, jira_ticket_id, ph, keep, settings.qdrant_api_key
            )
        log.info("embed_ticket_testcases %s: %s", jira_ticket_id, result)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("embed_ticket_testcases failed for %s: %s", jira_ticket_id, exc)
