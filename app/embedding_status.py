"""Live health + freshness status for every embedding collection.

Reports, for each Qdrant collection this app writes embeddings to: whether it
exists, how many vectors it holds, its vector dimension, when it was last
updated (persisted in ``app_settings`` so it survives restarts), and whether a
background job is currently writing to it.

Consumed by the admin dashboard's live status panel via
``GET /graph-admin/embeddings/status``. Every call is best-effort: a missing
DATABASE_URL, an unreachable Qdrant, or a partially-configured collection
degrades gracefully to a status entry rather than raising.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.app_settings import get_setting, set_setting
from app.codebase_graph import CODEBASE_EMBEDDING_MODELS
from app.config import Settings
from app.qdrant_store import (
    GITHUB_COLLECTION,
    JIRA_COLLECTION,
    JIRA_HYBRID_COLLECTION,
    TESTCASE_COLLECTION,
)

log = logging.getLogger(__name__)

_SETTING_PREFIX = "embed_last_updated:"


# ─── recording (write side) ──────────────────────────────────────────────────

def record_embedding_update(
    settings: Settings, collection: str, count: Optional[int] = None
) -> None:
    """Persist the last-updated timestamp (+ optional point count) for a collection.

    Best-effort: silently no-ops when DATABASE_URL is unset or the write fails,
    so it can be dropped into embedding pipelines without adding a failure mode.
    """
    if not settings.database_url:
        return
    try:
        payload = datetime.now(timezone.utc).isoformat()
        if count is not None:
            payload = f"{payload}|{count}"
        set_setting(settings, _SETTING_PREFIX + collection, payload)
    except Exception as exc:  # noqa: BLE001 - never let bookkeeping break a job
        log.debug("record_embedding_update(%s) failed: %s", collection, exc)


def _read_last_updated(settings: Settings, collection: str) -> tuple[Optional[str], Optional[int]]:
    raw = get_setting(settings, _SETTING_PREFIX + collection)
    if not raw:
        return None, None
    ts, _, cnt = raw.partition("|")
    last_count = int(cnt) if cnt.isdigit() else None
    return (ts or None), last_count


# ─── registry ────────────────────────────────────────────────────────────────

def _registry(settings: Settings) -> list[dict[str, str]]:
    """Ordered list of collections to report on, with display metadata."""
    cols: list[dict[str, str]] = [
        {"key": JIRA_COLLECTION, "label": "Jira tickets (dense)", "kind": "jira"},
        {"key": JIRA_HYBRID_COLLECTION, "label": "Jira tickets (hybrid)", "kind": "jira"},
        {"key": TESTCASE_COLLECTION, "label": "Test cases (regression)", "kind": "testcases"},
        {"key": GITHUB_COLLECTION, "label": "GitHub commits", "kind": "commits"},
    ]
    for model_key, meta in CODEBASE_EMBEDDING_MODELS.items():
        cols.append({"key": model_key, "label": f"Codebase · {meta['label']}", "kind": "codebase"})
    cols.append(
        {"key": settings.rca_code_chunks_collection, "label": "RCA code chunks", "kind": "rca"}
    )
    return cols


# ─── live "updating" detection from the in-memory job store ───────────────────

def _rca_progress_block(
    keys: list[str],
    totals: dict[str, Any],
    progress: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build progress for the RCA code-index build.

    build_index reports repo-level completion plus live chunk counts for the repo
    currently embedding. Total chunks across all repos is unknown up front, so the
    only honest ETA is *within the current repo* (repo_elapsed extrapolated over
    its known chunk count). The bar shows a smooth overall fraction that blends
    completed repos with the current repo's fractional progress.
    """
    repos_done = int(progress.get("repositories_done") or 0)
    total_repos = int(totals.get("repositories") or 0)
    cdone = int(progress.get("current_repo_chunks_done") or 0)
    ctotal = int(progress.get("current_repo_chunks_total") or 0)
    repo_elapsed = int(progress.get("current_repo_elapsed") or 0)
    repo_index = progress.get("current_repo_index")
    current_repo = meta.get("current_repo")

    repo_frac = (cdone / ctotal) if ctotal else 0.0
    percent = round(100 * (repos_done + repo_frac) / total_repos) if total_repos else 0

    eta = None
    if cdone and ctotal and repo_elapsed:
        eta = int(repo_elapsed * (ctotal - cdone) / cdone)

    detail = None
    if current_repo:
        loc = f"repo {repo_index}/{total_repos}" if repo_index and total_repos else "repo"
        chunks = f" · {cdone:,}/{ctotal:,} chunks" if ctotal else ""
        detail = f"{loc}: {current_repo}{chunks}"

    block = {
        "done": repos_done,
        "total": total_repos,
        "percent": percent,
        "eta_seconds": eta,
        "eta_scope": "repo",  # ETA is for the current repo, not the whole build
        "elapsed_seconds": None,
        "unit": "repos",
        "detail": detail,
    }
    return {k: block for k in keys}


def _progress_by_collection(
    job_store: Any, registry: list[dict[str, str]]
) -> dict[str, dict[str, Any]]:
    """Map each collection a running job is writing to → its live progress.

    Each value is ``{done, total, eta_seconds, elapsed_seconds, unit}``. ETA is
    taken from the job's own estimate where it keeps one (Jira/codebase graph
    jobs), else extrapolated from elapsed time and fraction complete (the RCA
    code-index build only reports repo-level progress).
    """
    out: dict[str, dict[str, Any]] = {}
    codebase_keys = {c["key"] for c in registry if c["kind"] == "codebase"}
    rca_keys = [c["key"] for c in registry if c["kind"] == "rca"]
    try:
        jobs = job_store.list_recent(limit=10)
    except Exception:  # noqa: BLE001
        return out
    now = datetime.now(timezone.utc)
    for job in jobs:
        if getattr(job, "status", None) not in ("pending", "running"):
            continue
        action = getattr(job, "action", "") or ""
        totals = getattr(job, "totals", {}) or {}
        progress = getattr(job, "progress", {}) or {}
        meta = getattr(job, "meta", {}) or {}
        started = getattr(job, "started_at", None)
        elapsed = (now - started).total_seconds() if started else None

        def _assign(
            keys: list[str], done: Any, total: Any, eta: Any, unit: str,
            *, detail: Optional[str] = None,
        ) -> None:
            done_i, total_i = int(done or 0), int(total or 0)
            # Derive an ETA when the job doesn't report one but we know progress.
            if (not eta or eta <= 0) and done_i and total_i and elapsed:
                eta = elapsed * (total_i - done_i) / done_i
            block = {
                "done": done_i,
                "total": total_i,
                "percent": round(100 * done_i / total_i) if total_i else 0,
                "eta_seconds": int(eta) if eta and eta > 0 else None,
                "eta_scope": "overall",
                "elapsed_seconds": int(elapsed) if elapsed else None,
                "unit": unit,
                "detail": detail,
            }
            for k in keys:
                out[k] = block

        if action == "rca_code_index_build":
            out.update(_rca_progress_block(rca_keys, totals, progress, meta))
        if action == "testcase_embeddings" or totals.get("testcase_embedding_documents"):
            _assign(
                [TESTCASE_COLLECTION],
                progress.get("testcase_embedding_documents_done"),
                totals.get("testcase_embedding_documents"),
                None,
                "test cases",
            )
        if action == "jira_tickets_only" or totals.get("jira_embedding_documents"):
            _assign(
                [JIRA_COLLECTION, JIRA_HYBRID_COLLECTION],
                progress.get("jira_embedding_documents_done"),
                totals.get("jira_embedding_documents"),
                progress.get("jira_embedding_eta_seconds"),
                "tickets",
            )
        if totals.get("codebase_embedding_documents") or action in ("update", "regenerate", "create_new"):
            model = meta.get("embedding_model")
            keys = [model] if model in codebase_keys else list(codebase_keys)
            _assign(
                keys,
                progress.get("codebase_embedding_documents_done"),
                totals.get("codebase_embedding_documents"),
                progress.get("codebase_embedding_eta_seconds"),
                "files",
            )
    return out


# ─── qdrant probe ────────────────────────────────────────────────────────────

def _vector_dim(info: Any) -> Optional[int]:
    """Best-effort extraction of the dense vector size from a collection info."""
    try:
        vectors = info.config.params.vectors
    except Exception:  # noqa: BLE001
        return None
    size = getattr(vectors, "size", None)
    if isinstance(size, int):
        return size
    # Named-vector config: a dict of {name: VectorParams}.
    if isinstance(vectors, dict):
        for params in vectors.values():
            s = getattr(params, "size", None)
            if isinstance(s, int):
                return s
    return None


def get_embeddings_status(settings: Settings, job_store: Any) -> dict[str, Any]:
    """Assemble the full status payload for the live embeddings panel."""
    registry = _registry(settings)
    progress_map = _progress_by_collection(job_store, registry)

    reachable = True
    reach_error: Optional[str] = None
    existing: set[str] = set()
    client = None
    if not settings.qdrant_url:
        reachable = False
        reach_error = "QDRANT_URL is not configured"
    else:
        try:
            from app.qdrant_store import _get_client

            client = _get_client(settings.qdrant_url, settings.qdrant_api_key or None)
            existing = {c.name for c in client.get_collections().collections}
        except Exception as exc:  # noqa: BLE001
            reachable = False
            reach_error = str(exc)

    collections: list[dict[str, Any]] = []
    for entry in registry:
        key = entry["key"]
        last_updated, last_count = _read_last_updated(settings, key)
        item: dict[str, Any] = {
            "key": key,
            "label": entry["label"],
            "kind": entry["kind"],
            "exists": False,
            "points": None,
            "vector_dim": None,
            "last_updated": last_updated,
            "updating": key in progress_map,
            "progress": progress_map.get(key),
            "health": "unknown",
        }
        if not reachable:
            item["health"] = "unreachable"
        elif key not in existing:
            item["health"] = "missing"
        else:
            item["exists"] = True
            try:
                item["points"] = client.count(key).count
            except Exception as exc:  # noqa: BLE001
                item["points"] = last_count
                log.debug("count(%s) failed: %s", key, exc)
            try:
                item["vector_dim"] = _vector_dim(client.get_collection(key))
            except Exception as exc:  # noqa: BLE001
                log.debug("get_collection(%s) failed: %s", key, exc)
            item["health"] = "ok" if (item["points"] or 0) > 0 else "empty"
        collections.append(item)

    if not reachable:
        overall = "unreachable"
    elif any(c["health"] in ("missing", "empty") for c in collections):
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "reachable": reachable,
        "error": reach_error,
        "overall": overall,
        "updating": bool(progress_map),
        "qdrant_url": settings.qdrant_url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "collections": collections,
    }
