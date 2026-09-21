"""Finds historically similar Jira tickets — three-tier search pipeline.

Priority
--------
1. Hybrid  — BGE-M3 dense + sparse → Qdrant RRF fusion (best accuracy)
             Requires FlagEmbedding + jira_tickets_hybrid collection populated.
2. Dense   — Ollama bge-m3 → Qdrant cosine sim on jira_tickets collection.
             Falls back to this when FlagEmbedding is not installed.
3. Keyword — PostgreSQL ILIKE on summary + description.
             Last resort when Qdrant / Ollama are both unavailable.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.config import Settings
from app.flag_embedder import BGEM3Embedder
from app.ollama_embedder import OllamaEmbedder
from app.qdrant_store import JIRA_COLLECTION, JIRA_HYBRID_COLLECTION

log = logging.getLogger(__name__)

_STOP_WORDS = {
    "about", "after", "again", "also", "before", "description",
    "from", "have", "into", "jira", "missing", "please",
    "reply", "should", "that", "them", "then", "there",
    "they", "this", "than", "ticket", "update", "when",
    "where", "which", "with",
}

_MAX_RETURNED_TICKETS = 5
_SEARCH_DEPTH = 10

# ── shared Qdrant helpers ─────────────────────────────────────────────────────

def _qdrant_client(url: str, api_key: Optional[str] = None):
    from qdrant_client import QdrantClient
    return QdrantClient(url=url, api_key=api_key or None)


def _build_filter(project_key: Optional[str]) -> Any:
    """Return a Qdrant Filter (or None) for project_key."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    must = []
    if project_key:
        must.append(FieldCondition(key="project_key", match=MatchValue(value=project_key)))
    return Filter(must=must) if must else None


def _point_to_hit(r: Any) -> Dict[str, Any]:
    """Convert a Qdrant scored point to a hit dict."""
    payload = r.payload or {}
    status = payload.get("status", "")
    return {
        "ticket_key":  payload.get("key", ""),
        "project_key": payload.get("project_key", ""),
        "summary":     payload.get("summary", ""),
        "status":      status,
        "issue_type":  payload.get("issue_type", ""),
        "similarity_score": round(r.score, 4),
        # filled later by _enrich_from_db
        "description": None, "priority": None,
        "assignee_name": None, "reporter_name": None,
        "labels": [], "created_at": None, "updated_at": None,
    }


# ── main class ────────────────────────────────────────────────────────────────

class SimilarTicketFinder:
    """Three-tier Jira ticket similarity search."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def find_similar(
        self,
        summary: str,
        description: Optional[str] = None,
        *,
        project_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Match the text format used at ingest time (graph_job_runner._ticket_embed_texts)
        query = f"{summary[:200]}\n{(description or '')[:500]}".strip()

        # Tier 1 — hybrid (BGE-M3 dense + sparse, RRF)
        hits, method = self._hybrid_search(query, project_key=project_key,
                                            top_k=_SEARCH_DEPTH * 2)

        # Tier 2 — dense-only cosine (Ollama)
        if not hits:
            hits, method = self._dense_search(query, project_key=project_key,
                                               top_k=_SEARCH_DEPTH * 2)

        # Tier 3 — PostgreSQL keyword ILIKE
        if not hits:
            hits, method = self._keyword_search(query, project_key=project_key,
                                                 top_k=_SEARCH_DEPTH)
        else:
            hits = self._enrich_from_db(hits)

        match_threshold = self.settings.similar_ticket_match_threshold
        hits = self._top_match_above_threshold(hits, match_threshold)
        log.info(
            "SimilarTicketFinder method=%s returned=%d threshold>%s project=%s",
            method, len(hits), match_threshold, project_key,
        )
        return {"query_summary": summary, "total_found": len(hits),
                "search_method": method, "tickets": hits}

    # ── Tier 1: hybrid search (BGE-M3 dense + sparse → RRF) ─────────────────

    def _hybrid_search(
        self,
        query: str,
        *,
        project_key: Optional[str],
        top_k: int,
    ) -> tuple[List[Dict[str, Any]], str]:
        try:
            from qdrant_client.models import Prefetch, FusionQuery, Fusion, SparseVector
            client = _qdrant_client(self.settings.qdrant_url, self.settings.qdrant_api_key)

            # Check if Qdrant collection exists and has points BEFORE loading model
            existing = {c.name for c in client.get_collections().collections}
            if JIRA_HYBRID_COLLECTION not in existing:
                return [], "dense_fallback"

            info = client.get_collection(JIRA_HYBRID_COLLECTION)
            if not info.points_count or info.points_count == 0:
                return [], "dense_fallback"
        except Exception as exc:
            log.debug("Qdrant hybrid collection check skipped (%s), falling back", exc)
            return [], "dense_fallback"

        flag = BGEM3Embedder()
        if not flag.is_available():
            log.info("FlagEmbedding not installed — skipping hybrid search")
            return [], "dense_fallback"

        try:
            encoded = flag.encode_one(query)
        except Exception as exc:
            log.warning("BGE-M3 encode failed: %s", exc)
            return [], "dense_fallback"

        if not encoded or not encoded.get("dense"):
            return [], "dense_fallback"

        try:
            search_filter = _build_filter(project_key)

            sparse_vec = SparseVector(
                indices=encoded.get("sparse_indices", []),
                values=encoded.get("sparse_values", []),
            )

            response = client.query_points(
                collection_name=JIRA_HYBRID_COLLECTION,
                prefetch=[
                    Prefetch(query=encoded["dense"], using="dense", limit=top_k),
                    Prefetch(query=sparse_vec,        using="sparse", limit=top_k),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                limit=top_k,
                query_filter=search_filter,
                with_payload=True,
            )

            # Re-score with dense cosine similarity so the displayed score is
            # interpretable (0–1) instead of the raw RRF rank-fusion value.
            # RRF is used only for retrieval; cosine tells us actual closeness.
            try:
                dense_resp = client.query_points(
                    collection_name=JIRA_HYBRID_COLLECTION,
                    query=encoded["dense"],
                    using="dense",
                    limit=top_k,
                    query_filter=search_filter,
                    with_payload=False,
                )
                dense_scores = {str(r.id): r.score for r in dense_resp.points}
            except Exception:
                dense_scores = {}

            results = []
            for r in response.points:
                hit = _point_to_hit(r)
                cosine = dense_scores.get(str(r.id))
                if cosine is not None:
                    hit["similarity_score"] = round(cosine, 4)
                results.append(hit)

            # Sort by cosine similarity (best first); ties keep RRF order
            results.sort(key=lambda h: h["similarity_score"], reverse=True)

            log.info("Hybrid RRF search returned %d hits", len(results))
            return results, "hybrid_rrf"

        except Exception as exc:
            log.warning("Hybrid search failed: %s", exc)
            return [], "dense_fallback"

    # ── Tier 2: dense-only cosine search (Ollama) ────────────────────────────

    def _dense_search(
        self,
        query: str,
        *,
        project_key: Optional[str],
        top_k: int,
    ) -> tuple[List[Dict[str, Any]], str]:
        try:
            client = _qdrant_client(self.settings.qdrant_url, self.settings.qdrant_api_key)
            existing = {c.name for c in client.get_collections().collections}
            if JIRA_COLLECTION not in existing:
                return [], "keyword_fallback"
            info = client.get_collection(JIRA_COLLECTION)
            if not info.points_count or info.points_count == 0:
                return [], "keyword_fallback"
        except Exception:
            return [], "keyword_fallback"

        embedder = OllamaEmbedder(
            base_url=self.settings.ollama_url,
            model=self.settings.ollama_embed_model,
            timeout_seconds=min(self.settings.ollama_embed_timeout_seconds, 3),
        )
        if not embedder.is_available():
            log.warning("Ollama unavailable — skipping dense search")
            return [], "keyword_fallback"

        vector = embedder.embed(query)
        if not vector:
            return [], "keyword_fallback"

        try:
            search_filter = _build_filter(project_key)

            try:
                response = client.query_points(
                    collection_name=JIRA_COLLECTION,
                    query=vector,
                    limit=top_k,
                    query_filter=search_filter,
                    with_payload=True,
                )
                scored = response.points
            except AttributeError:
                scored = client.search(
                    collection_name=JIRA_COLLECTION,
                    query_vector=vector,
                    limit=top_k,
                    query_filter=search_filter,
                    with_payload=True,
                )

            results = [_point_to_hit(r) for r in scored]
            log.info("Dense cosine search returned %d hits", len(results))
            return results, "semantic"

        except Exception as exc:
            log.warning("Dense search failed: %s", exc)
            return [], "keyword_fallback"

    # ── Tier 3: PostgreSQL keyword fallback ──────────────────────────────────

    def _keyword_search(
        self,
        query: str,
        *,
        project_key: Optional[str],
        top_k: int,
    ) -> tuple[List[Dict[str, Any]], str]:
        if not self.settings.database_url:
            return [], "none"

        keywords = self._extract_keywords(query)[:6]
        if not keywords:
            return [], "none"

        try:
            import psycopg
            from psycopg.rows import dict_row

            where_parts = []
            params: list[Any] = []

            if project_key:
                where_parts.append("project_key = %s")
                params.append(project_key)

            kw_clause = " OR ".join(
                "(summary ILIKE %s OR description ILIKE %s)" for _ in keywords
            )
            where_parts.append(f"({kw_clause})")
            for kw in keywords:
                params.extend([f"%{kw}%", f"%{kw}%"])

            params.append(top_k)

            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    f"""
                    SELECT ticket_key, project_key, summary, description,
                           status, issue_type, priority,
                           assignee_name, reporter_name, labels,
                           created_at, updated_at
                    FROM jira_ticket_cache
                    WHERE {" AND ".join(where_parts)}
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    params,
                ).fetchall()

            results = []
            for r in rows:
                item = dict(r)
                text = f"{item.get('summary', '')} {item.get('description', '')}".lower()
                matches = sum(1 for kw in keywords if kw in text)
                ratio = matches / max(1, len(keywords))
                score = round(min(0.96, 0.72 + (ratio * 0.24)), 2)
                results.append({
                    **item,
                    "similarity_score": score,
                    "created_at": _fmt_dt(item.get("created_at")),
                    "updated_at": _fmt_dt(item.get("updated_at")),
                    "labels": item.get("labels") or [],
                })

            results.sort(key=lambda x: x["similarity_score"], reverse=True)
            log.info("PostgreSQL keyword fallback returned %d tickets", len(results))
            return results, "keyword_fallback"

        except Exception as exc:
            log.warning("PostgreSQL keyword search failed: %s", exc)
            return [], "none"

    # ── PostgreSQL enrichment ─────────────────────────────────────────────────

    def _enrich_from_db(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fetch full ticket rows from PostgreSQL and merge into Qdrant hits."""
        if not self.settings.database_url or not hits:
            return hits

        keys = [h["ticket_key"] for h in hits if h.get("ticket_key")]
        if not keys:
            return hits

        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    """
                    SELECT ticket_key, project_key, summary, description,
                           status, issue_type, priority,
                           assignee_name, reporter_name, labels,
                           created_at, updated_at
                    FROM jira_ticket_cache
                    WHERE ticket_key = ANY(%s)
                    """,
                    (keys,),
                ).fetchall()

            db_map = {r["ticket_key"]: dict(r) for r in rows}
            return [
                {
                    **hit,
                    "description":   db.get("description"),
                    "priority":      db.get("priority"),
                    "assignee_name": db.get("assignee_name"),
                    "reporter_name": db.get("reporter_name"),
                    "labels":        db.get("labels") or [],
                    "created_at":    _fmt_dt(db.get("created_at")),
                    "updated_at":    _fmt_dt(db.get("updated_at")),
                    "status":        db.get("status")      or hit.get("status", ""),
                    "issue_type":    db.get("issue_type")  or hit.get("issue_type", ""),
                    "project_key":   db.get("project_key") or hit.get("project_key", ""),
                    "summary":       db.get("summary")     or hit.get("summary", ""),
                }
                for hit in hits
                if (db := db_map.get(hit.get("ticket_key", ""), {})) is not None
            ]

        except Exception as exc:
            log.warning("PostgreSQL enrichment failed: %s", exc)
            return hits

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_keywords(text: str) -> List[str]:
        words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
        seen: set[str] = set()
        out: List[str] = []
        for w in words:
            if w not in _STOP_WORDS and w not in seen:
                seen.add(w)
                out.append(w)
        return out

    @staticmethod
    def _top_match_above_threshold(
        hits: List[Dict[str, Any]],
        threshold: float,
    ) -> List[Dict[str, Any]]:
        filtered = [
            h for h in hits
            if SimilarTicketFinder._similarity_score(h) >= (threshold * 0.8) or SimilarTicketFinder._similarity_score(h) == 0.0
        ]
        filtered.sort(key=SimilarTicketFinder._similarity_score, reverse=True)
        return filtered[:_MAX_RETURNED_TICKETS]

    @staticmethod
    def _similarity_score(hit: Dict[str, Any]) -> float:
        try:
            return float(hit.get("similarity_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0


def _fmt_dt(val: Any) -> Optional[str]:
    if val is None:
        return None
    try:
        return val.isoformat()
    except AttributeError:
        return str(val)
