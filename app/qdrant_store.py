"""Qdrant vector store operations for commit and Jira ticket embeddings.

Collections managed:
  github_commits       – vectors for commit summaries
  jira_tickets         – vectors for ticket summaries + descriptions
  codebase_*           – model-specific vectors for source files

Gracefully skips upsert if qdrant-client is not installed or the server
is unreachable — embedding generation is best-effort.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)

GITHUB_COLLECTION = "github_commits"
JIRA_COLLECTION = "jira_tickets"
JIRA_HYBRID_COLLECTION = "jira_tickets_hybrid"  # dense + sparse, used for RRF hybrid search
TESTCASE_COLLECTION = "test_cases"  # vectors for generated test cases (regression flag)
_DEFAULT_VECTOR_SIZE = 1024  # qwen3:0.6b and BGE-M3 both output 1024-dim vectors


def _get_client(url: str, api_key: Optional[str] = None):
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise RuntimeError(
            "qdrant-client not installed. Add it to requirements.txt and reinstall."
        ) from exc

    candidates = [url]
    for c in ["http://jira-ai-qdrant:6333", "http://host.docker.internal:6333", "http://localhost:6333"]:
        if c not in candidates:
            candidates.append(c)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            client = QdrantClient(url=candidate, api_key=api_key or None, timeout=2.0)
            client.get_collections()
            return client
        except Exception:
            continue

    return QdrantClient(url=url, api_key=api_key or None)


def _infer_vector_size(embeddings: list[Optional[list[float]]]) -> int:
    """Return the dimension of the first non-None embedding, or the default."""
    for emb in embeddings:
        if emb is not None:
            return len(emb)
    return _DEFAULT_VECTOR_SIZE


def _ensure_collection(client, name: str, vector_size: int = _DEFAULT_VECTOR_SIZE) -> None:
    try:
        from qdrant_client.models import Distance, VectorParams
    except ImportError:
        return
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        log.info("Created Qdrant collection '%s' (dim=%d)", name, vector_size)


def _stable_id(seed: str) -> str:
    """Derive a deterministic UUID from a string so upserts are idempotent."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


# ─── Jira tickets ────────────────────────────────────────────────────────────

def upsert_jira_embeddings(
    qdrant_url: str,
    tickets: list[dict[str, Any]],
    embeddings: list[Optional[list[float]]],
    api_key: Optional[str] = None,
) -> int:
    """Store Jira ticket embeddings in Qdrant. Returns the number of points written."""
    try:
        from qdrant_client.models import PointStruct
    except ImportError:
        log.warning("qdrant-client not installed; skipping Jira embedding storage")
        return 0

    try:
        client = _get_client(qdrant_url, api_key)
        _ensure_collection(client, JIRA_COLLECTION, _infer_vector_size(embeddings))

        points: list[PointStruct] = []
        for ticket, emb in zip(tickets, embeddings):
            if emb is None:
                continue
            fields = ticket.get("fields", {}) or {}
            key = ticket.get("key", "")
            points.append(
                PointStruct(
                    id=_stable_id(key or str(uuid.uuid4())),
                    vector=emb,
                    payload={
                        "key": key,
                        "summary": (fields.get("summary") or "")[:300],
                        "status": (fields.get("status") or {}).get("name", ""),
                        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
                        "project_key": key.rsplit("-", 1)[0] if "-" in key else key,
                    },
                )
            )

        if points:
            client.upsert(collection_name=JIRA_COLLECTION, points=points)
            log.info("Stored %d Jira ticket embeddings in Qdrant", len(points))

        return len(points)

    except Exception as exc:
        log.warning("Qdrant Jira upsert failed: %s", exc)
        return 0


# ─── Test cases ──────────────────────────────────────────────────────────────

def upsert_testcase_embeddings(
    qdrant_url: str,
    test_cases: list[dict[str, Any]],
    embeddings: list[Optional[list[float]]],
    api_key: Optional[str] = None,
) -> int:
    """Store generated test-case embeddings in Qdrant. Returns points written.

    Each dict must carry: jira_ticket_id, phase, tc_index, title, status.
    The point id is derived from (jira_ticket_id, phase, tc_index) so re-embedding
    a ticket's cases overwrites the old vectors instead of duplicating them.
    """
    try:
        from qdrant_client.models import PointStruct
    except ImportError:
        log.warning("qdrant-client not installed; skipping test-case embedding storage")
        return 0

    try:
        client = _get_client(qdrant_url, api_key)
        _ensure_collection(client, TESTCASE_COLLECTION, _infer_vector_size(embeddings))

        points: list[PointStruct] = []
        for tc, emb in zip(test_cases, embeddings):
            if emb is None:
                continue
            jira_key = str(tc.get("jira_ticket_id") or "")
            phase = str(tc.get("phase") or "qa")
            tc_index = tc.get("tc_index")
            seed = f"{jira_key}:{phase}:{tc_index}"
            points.append(
                PointStruct(
                    id=_stable_id(seed),
                    vector=emb,
                    payload={
                        "jira_ticket_id": jira_key,
                        "phase": phase,
                        "tc_index": tc_index,
                        "title": (tc.get("title") or "")[:300],
                        "status": tc.get("status") or "",
                        "project_key": jira_key.rsplit("-", 1)[0] if "-" in jira_key else jira_key,
                    },
                )
            )

        stored = 0
        batch_size = 500
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            client.upsert(collection_name=TESTCASE_COLLECTION, points=batch)
            stored += len(batch)

        if stored:
            log.info("Stored %d test-case embeddings in Qdrant", stored)
        return stored

    except Exception as exc:
        log.warning("Qdrant test-case upsert failed: %s", exc)
        return 0


def delete_testcase_embeddings(
    qdrant_url: str,
    jira_ticket_id: str,
    phase: str,
    keep_tc_indexes: list[int],
    api_key: Optional[str] = None,
) -> int:
    """Delete stale test-case points for a ticket/phase whose tc_index is no
    longer present (the ticket's case count shrank). Returns points deleted.

    ``keep_tc_indexes`` must list the indexes that still exist; it is required so
    this can never wipe a ticket's live vectors (called right after upsert)."""
    if not jira_ticket_id or not keep_tc_indexes:
        return 0
    try:
        from qdrant_client.models import (
            Filter, FieldCondition, MatchValue, MatchExcept, FilterSelector,
        )
    except ImportError:
        return 0

    try:
        client = _get_client(qdrant_url, api_key)
        existing = {c.name for c in client.get_collections().collections}
        if TESTCASE_COLLECTION not in existing:
            return 0
        must = [
            FieldCondition(key="jira_ticket_id", match=MatchValue(value=jira_ticket_id)),
            FieldCondition(key="phase", match=MatchValue(value=phase)),
            FieldCondition(key="tc_index", match=MatchExcept(**{"except": keep_tc_indexes})),
        ]
        client.delete(
            collection_name=TESTCASE_COLLECTION,
            points_selector=FilterSelector(filter=Filter(must=must)),
        )
        return 1
    except Exception as exc:
        log.warning("Qdrant test-case delete failed: %s", exc)
        return 0


# ─── GitHub commits ──────────────────────────────────────────────────────────

def upsert_commit_embeddings(
    qdrant_url: str,
    commits: list[dict[str, Any]],
    embeddings: list[Optional[list[float]]],
    api_key: Optional[str] = None,
) -> int:
    """Store commit summary embeddings in Qdrant. Returns the number of points written."""
    try:
        from qdrant_client.models import PointStruct
    except ImportError:
        log.warning("qdrant-client not installed; skipping commit embedding storage")
        return 0

    try:
        client = _get_client(qdrant_url, api_key)
        _ensure_collection(client, GITHUB_COLLECTION, _infer_vector_size(embeddings))

        points: list[PointStruct] = []
        for commit, emb in zip(commits, embeddings):
            if emb is None:
                continue
            sha = commit.get("sha", "")
            points.append(
                PointStruct(
                    id=_stable_id(sha or str(uuid.uuid4())),
                    vector=emb,
                    payload={
                        "sha": sha,
                        "summary": (commit.get("summary") or "")[:300],
                        "repo": commit.get("repo_full_name", ""),
                        "author_email": commit.get("author_email", ""),
                    },
                )
            )

        if points:
            client.upsert(collection_name=GITHUB_COLLECTION, points=points)
            log.info("Stored %d commit embeddings in Qdrant", len(points))

        return len(points)

    except Exception as exc:
        log.warning("Qdrant commit upsert failed: %s", exc)
        return 0


# ─── Codebase files ──────────────────────────────────────────────────────────

def upsert_codebase_embeddings(
    qdrant_url: str,
    collection_name: str,
    documents: list[dict[str, Any]],
    embeddings: list[Optional[list[float]]],
    model_key: str,
    model_name: str,
    api_key: Optional[str] = None,
) -> int:
    """Store source-file embeddings in a model-specific Qdrant collection."""
    try:
        from qdrant_client.models import PointStruct
    except ImportError:
        log.warning("qdrant-client not installed; skipping codebase embedding storage")
        return 0

    try:
        client = _get_client(qdrant_url, api_key)
        _ensure_collection(client, collection_name, _infer_vector_size(embeddings))

        points: list[PointStruct] = []
        for doc, emb in zip(documents, embeddings):
            if emb is None:
                continue
            seed = f"{collection_name}:{doc.get('id') or doc.get('path')}"
            points.append(
                PointStruct(
                    id=_stable_id(seed),
                    vector=emb,
                    payload={
                        "id": doc.get("id", ""),
                        "repo": doc.get("repo", ""),
                        "repo_name": doc.get("repo_name", ""),
                        "path": doc.get("path", ""),
                        "language": doc.get("language", ""),
                        "extension": doc.get("extension", ""),
                        "lines": doc.get("lines", 0),
                        "model_key": model_key,
                        "model_name": model_name,
                        "text": (doc.get("text") or "")[:2000],
                    },
                )
            )

        stored = 0
        batch_size = 500  # ~4MB per batch well under Qdrant's 32MB limit
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            client.upsert(collection_name=collection_name, points=batch)
            stored += len(batch)
            log.info(
                "Stored %d/%d codebase embeddings in Qdrant collection '%s'",
                stored, len(points), collection_name,
            )

        return stored

    except Exception as exc:
        log.warning("Qdrant codebase upsert failed: %s", exc)
        return 0


# ─── Jira tickets — hybrid (dense + sparse) ──────────────────────────────────

def _ensure_hybrid_collection(client, name: str, vector_size: int = _DEFAULT_VECTOR_SIZE) -> None:
    """Create or verify a Qdrant collection with named dense + sparse vectors."""
    try:
        from qdrant_client.models import Distance, VectorParams, SparseVectorParams
    except ImportError:
        return

    existing = {c.name for c in client.get_collections().collections}
    if name in existing:
        return

    client.create_collection(
        name,
        vectors_config={
            "dense": VectorParams(size=vector_size, distance=Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )
    log.info("Created hybrid Qdrant collection '%s' (dense=%d + sparse)", name, vector_size)


def upsert_jira_hybrid_embeddings(
    qdrant_url: str,
    tickets: list[dict[str, Any]],
    encoded: list[dict[str, Any]],   # list of {dense, sparse_indices, sparse_values}
    api_key: Optional[str] = None,
) -> int:
    """Store BGE-M3 hybrid (dense + sparse) Jira embeddings.  Returns points written."""
    try:
        from qdrant_client.models import PointStruct, SparseVector
    except ImportError:
        log.warning("qdrant-client not installed; skipping hybrid Jira embedding storage")
        return 0

    try:
        client = _get_client(qdrant_url, api_key)
        _ensure_hybrid_collection(client, JIRA_HYBRID_COLLECTION)

        points: list[PointStruct] = []
        for ticket, enc in zip(tickets, encoded):
            if not enc.get("dense"):
                continue
            fields = ticket.get("fields", {}) or {}
            key = ticket.get("key", "")
            points.append(
                PointStruct(
                    id=_stable_id(key or str(uuid.uuid4())),
                    vector={
                        "dense": enc["dense"],
                        "sparse": SparseVector(
                            indices=enc.get("sparse_indices", []),
                            values=enc.get("sparse_values", []),
                        ),
                    },
                    payload={
                        "key": key,
                        "summary": (fields.get("summary") or "")[:300],
                        "status": (fields.get("status") or {}).get("name", ""),
                        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
                        "project_key": key.rsplit("-", 1)[0] if "-" in key else key,
                    },
                )
            )

        if points:
            client.upsert(collection_name=JIRA_HYBRID_COLLECTION, points=points)
            log.info("Stored %d hybrid Jira embeddings in Qdrant", len(points))

        return len(points)

    except Exception as exc:
        log.warning("Qdrant hybrid Jira upsert failed: %s", exc)
        return 0
