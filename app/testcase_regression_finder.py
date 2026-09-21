"""Flag a new Jira ticket as a possible regression against existing test cases.

When a ticket is created we already look for similar *tickets*
(app/similar_ticket_finder.py). This finder is the complementary check against
existing generated *test cases*: if the new ticket's summary/description is
semantically close to a test case we previously generated, the ticket likely
re-opens a scenario that already has a (baseline / expected-to-pass) test —
i.e. a regression — so it is surfaced the same way a similar ticket is.

Search tiers (mirrors SimilarTicketFinder, minus hybrid):
  1. Dense   — Ollama bge-m3 → Qdrant cosine on the `test_cases` collection.
  2. Keyword — PostgreSQL ILIKE on test_cases.title / expected. Last resort.

Only the single best match above `REGRESSION_MATCH_THRESHOLD` is returned.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.config import Settings
from app.ollama_embedder import OllamaEmbedder
from app.qdrant_store import TESTCASE_COLLECTION
from app.testcase_embeddings import _steps_to_list

log = logging.getLogger(__name__)

_STOP_WORDS = {
    "about", "after", "again", "also", "before", "description",
    "from", "have", "into", "jira", "missing", "please",
    "reply", "should", "that", "them", "then", "there",
    "they", "this", "than", "ticket", "update", "when",
    "where", "which", "with", "case", "test", "step", "steps",
    "expected", "verify", "check",
}

_MAX_RETURNED = 1
_SEARCH_DEPTH = 10


def _qdrant_client(url: str, api_key: Optional[str] = None):
    from qdrant_client import QdrantClient
    return QdrantClient(url=url, api_key=api_key or None)


def _build_filter(project_key: Optional[str]) -> Any:
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    must = []
    if project_key:
        must.append(FieldCondition(key="project_key", match=MatchValue(value=project_key)))
    return Filter(must=must) if must else None


def _point_to_hit(r: Any) -> Dict[str, Any]:
    payload = r.payload or {}
    return {
        "jira_ticket_id": payload.get("jira_ticket_id", ""),
        "project_key": payload.get("project_key", ""),
        "phase": payload.get("phase", "qa"),
        "tc_index": payload.get("tc_index"),
        "title": payload.get("title", ""),
        "status": payload.get("status", ""),
        "ticket_summary": payload.get("ticket_summary"),
        "similarity_score": round(r.score, 4),
        # filled by _enrich_from_db
        "steps": [], "expected": None, "ticket_status": None,
    }


class TestCaseRegressionFinder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def find_regressions(
        self,
        summary: str,
        description: Optional[str] = None,
        *,
        project_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Match the query-side format used for similar tickets so the two checks
        # embed comparable text.
        query = f"{summary[:200]}\n{(description or '')[:500]}".strip()

        hits, method = self._dense_search(query, project_key=project_key,
                                          top_k=_SEARCH_DEPTH)
        if hits:
            hits = self._enrich_from_db(hits)
        else:
            hits, method = self._keyword_search(query, project_key=project_key,
                                               top_k=_SEARCH_DEPTH)

        threshold = self.settings.regression_match_threshold
        hits = self._top_match_above_threshold(hits, threshold)
        log.info(
            "TestCaseRegressionFinder method=%s returned=%d threshold>%s project=%s",
            method, len(hits), threshold, project_key,
        )
        return {"query_summary": summary, "total_found": len(hits),
                "search_method": method, "matches": hits}

    # ── Tier 1: dense cosine (Ollama) ────────────────────────────────────────

    def _dense_search(
        self, query: str, *, project_key: Optional[str], top_k: int
    ) -> tuple[List[Dict[str, Any]], str]:
        embedder = OllamaEmbedder(
            base_url=self.settings.ollama_url,
            model=self.settings.ollama_embed_model,
            timeout_seconds=self.settings.ollama_embed_timeout_seconds,
        )
        if not embedder.is_available():
            log.warning("Ollama unavailable — skipping test-case dense search")
            return [], "keyword_fallback"

        vector = embedder.embed(query)
        if not vector:
            return [], "keyword_fallback"

        try:
            client = _qdrant_client(self.settings.qdrant_url, self.settings.qdrant_api_key)
            existing = {c.name for c in client.get_collections().collections}
            if TESTCASE_COLLECTION not in existing:
                log.info("Collection '%s' not found — run test-case embedding build",
                         TESTCASE_COLLECTION)
                return [], "keyword_fallback"

            search_filter = _build_filter(project_key)
            try:
                response = client.query_points(
                    collection_name=TESTCASE_COLLECTION,
                    query=vector,
                    limit=top_k,
                    query_filter=search_filter,
                    with_payload=True,
                )
                scored = response.points
            except AttributeError:
                scored = client.search(
                    collection_name=TESTCASE_COLLECTION,
                    query_vector=vector,
                    limit=top_k,
                    query_filter=search_filter,
                    with_payload=True,
                )

            results = [_point_to_hit(r) for r in scored]
            log.info("Test-case dense search returned %d hits", len(results))
            return results, "semantic"

        except Exception as exc:
            log.warning("Test-case dense search failed: %s", exc)
            return [], "keyword_fallback"

    # ── Tier 2: PostgreSQL keyword fallback ──────────────────────────────────

    def _keyword_search(
        self, query: str, *, project_key: Optional[str], top_k: int
    ) -> tuple[List[Dict[str, Any]], str]:
        if not self.settings.database_url:
            return [], "none"

        keywords = self._extract_keywords(query)[:6]
        if not keywords:
            return [], "none"

        try:
            import psycopg
            from psycopg.rows import dict_row

            where_parts: List[str] = []
            params: List[Any] = []
            if project_key:
                where_parts.append("tc.jira_ticket_id LIKE %s")
                params.append(f"{project_key}-%")

            kw_clause = " OR ".join(
                "(tc.title ILIKE %s OR tc.expected ILIKE %s)" for _ in keywords
            )
            where_parts.append(f"({kw_clause})")
            for kw in keywords:
                params.extend([f"%{kw}%", f"%{kw}%"])
            params.append(top_k)

            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    f"""
                    SELECT tc.jira_ticket_id, tc.phase, tc.tc_index, tc.title,
                           tc.steps, tc.expected, tc.status
                    FROM test_cases tc
                    WHERE {" AND ".join(where_parts)}
                    ORDER BY tc.updated_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    params,
                ).fetchall()

            results = []
            for r in rows:
                d = dict(r)
                jira_key = str(d.get("jira_ticket_id") or "")
                results.append({
                    "jira_ticket_id": jira_key,
                    "project_key": jira_key.rsplit("-", 1)[0] if "-" in jira_key else jira_key,
                    "phase": d.get("phase") or "qa",
                    "tc_index": d.get("tc_index"),
                    "title": d.get("title") or "",
                    "steps": _steps_to_list(d.get("steps")),
                    "expected": d.get("expected"),
                    "status": d.get("status") or "",
                    "ticket_summary": None,
                    "ticket_status": None,
                    "similarity_score": 0.0,
                })
            log.info("Test-case keyword fallback returned %d rows", len(results))
            return results, "keyword_fallback"

        except Exception as exc:
            log.warning("Test-case keyword search failed: %s", exc)
            return [], "none"

    # ── enrichment ───────────────────────────────────────────────────────────

    def _enrich_from_db(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fetch steps/expected + owning-ticket status for each Qdrant hit."""
        if not self.settings.database_url or not hits:
            return hits
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                for hit in hits:
                    row = conn.execute(
                        """
                        SELECT tc.steps, tc.expected, tc.status, tc.title,
                               t.status AS ticket_status
                        FROM test_cases tc
                        LEFT JOIN tickets t ON t.jira_ticket_id = tc.jira_ticket_id
                        WHERE tc.jira_ticket_id = %s AND tc.phase = %s AND tc.tc_index = %s
                        LIMIT 1
                        """,
                        (hit.get("jira_ticket_id"), hit.get("phase"), hit.get("tc_index")),
                    ).fetchone()
                    if row:
                        hit["steps"] = _steps_to_list(row.get("steps"))
                        hit["expected"] = row.get("expected")
                        hit["status"] = hit.get("status") or (row.get("status") or "")
                        hit["title"] = hit.get("title") or (row.get("title") or "")
                        hit["ticket_status"] = row.get("ticket_status")
            return hits
        except Exception as exc:
            log.warning("Test-case enrichment failed: %s", exc)
            return hits

    # ── helpers ──────────────────────────────────────────────────────────────

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
        hits: List[Dict[str, Any]], threshold: float
    ) -> List[Dict[str, Any]]:
        best = max(hits, key=TestCaseRegressionFinder._score, default=None)
        if best is None or TestCaseRegressionFinder._score(best) <= threshold:
            return []
        return [best][:_MAX_RETURNED]

    @staticmethod
    def _score(hit: Dict[str, Any]) -> float:
        try:
            return float(hit.get("similarity_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0
