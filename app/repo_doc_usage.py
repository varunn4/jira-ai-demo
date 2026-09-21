"""Persistence for repository-document generation: a reuse cache plus a per-user
token/cost usage log.

Two tables (created on startup if `DATABASE_URL` is set):

* ``doc_artifacts`` — the most recent generated markdown for each
  (repo, doc_type, context_hash). ``context_hash`` is a digest of the exact
  RepoTree context fed to the model (architecture map + packed source), so a new
  row is only produced when the code/context actually changed. This is what lets
  us skip a paid regeneration and reuse a previous document.
* ``doc_generation_usage`` — one row per generation event (including cache
  reuses, which cost 0 tokens), attributed to the requesting user, for later
  analysis of who consumed how many tokens / how much money / how many docs.

All functions degrade gracefully to no-ops when Postgres is not configured.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from app.config import settings

log = logging.getLogger(__name__)


# ─── Pricing ─────────────────────────────────────────────────────────────────
# USD per 1M tokens: (input, output, cache_read, cache_write). Non-default
# families are approximated here; the default family reads env-overridable
# settings so deployments can tune the active model's rates precisely.
_PRICING_FAMILIES: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 1.50, 18.75),
    "haiku": (0.80, 4.0, 0.08, 1.0),
}


def _rates(model: str) -> tuple[float, float, float, float]:
    m = (model or "").lower()
    for key, rates in _PRICING_FAMILIES.items():
        if key in m:
            return rates
    return (
        settings.doc_price_input_per_mtok,
        settings.doc_price_output_per_mtok,
        settings.doc_price_cache_read_per_mtok,
        settings.doc_price_cache_write_per_mtok,
    )


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Return the estimated USD cost for one model call."""
    in_rate, out_rate, cr_rate, cw_rate = _rates(model)
    cost = (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * cr_rate
        + cache_creation_tokens * cw_rate
    ) / 1_000_000
    return round(cost, 6)


def context_hash(context: str) -> str:
    """Stable digest of the model context — changes iff the code/context changes."""
    return hashlib.sha256(context.encode("utf-8", errors="ignore")).hexdigest()


# ─── DB ──────────────────────────────────────────────────────────────────────

def _connect():
    if not settings.database_url:
        return None
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.database_url, row_factory=dict_row)


def ensure_doc_usage_schema() -> None:
    if not settings.database_url:
        log.info("DATABASE_URL not set; document usage logging/cache disabled")
        return
    try:
        conn = _connect()
        if conn is None:
            return
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS doc_artifacts (
                    id              SERIAL PRIMARY KEY,
                    repo            TEXT NOT NULL,
                    doc_type        TEXT NOT NULL,
                    context_hash    TEXT NOT NULL,
                    filename        TEXT NOT NULL,
                    markdown        TEXT NOT NULL,
                    model           TEXT,
                    input_tokens    INTEGER NOT NULL DEFAULT 0,
                    output_tokens   INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0,
                    created_by      TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (repo, doc_type, context_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS doc_generation_usage (
                    id              SERIAL PRIMARY KEY,
                    user_email      TEXT,
                    repo            TEXT NOT NULL,
                    doc_type        TEXT NOT NULL,
                    reused          BOOLEAN NOT NULL DEFAULT FALSE,
                    model           TEXT,
                    input_tokens    INTEGER NOT NULL DEFAULT 0,
                    output_tokens   INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0,
                    context_hash    TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.commit()
        log.info("Document usage/cache schema ready")
    except Exception:  # noqa: BLE001
        log.exception("Failed to ensure document usage schema")


def get_cached_artifact(repo: str, doc_type: str, ctx_hash: str) -> Optional[dict[str, Any]]:
    """Return a previously generated doc for the same repo+type+context, or None."""
    try:
        conn = _connect()
        if conn is None:
            return None
        with conn:
            return conn.execute(
                "SELECT filename, markdown, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens, cost_usd, created_at "
                "FROM doc_artifacts WHERE repo = %s AND doc_type = %s AND context_hash = %s",
                (repo, doc_type, ctx_hash),
            ).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("doc_artifacts lookup failed")
        return None


def save_artifact(
    *,
    repo: str,
    doc_type: str,
    ctx_hash: str,
    filename: str,
    markdown: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    cost_usd: float,
    created_by: Optional[str],
) -> None:
    try:
        conn = _connect()
        if conn is None:
            return
        with conn:
            conn.execute(
                """
                INSERT INTO doc_artifacts
                    (repo, doc_type, context_hash, filename, markdown, model,
                     input_tokens, output_tokens, cache_read_tokens,
                     cache_creation_tokens, cost_usd, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (repo, doc_type, context_hash) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    markdown = EXCLUDED.markdown,
                    model = EXCLUDED.model,
                    input_tokens = EXCLUDED.input_tokens,
                    output_tokens = EXCLUDED.output_tokens,
                    cache_read_tokens = EXCLUDED.cache_read_tokens,
                    cache_creation_tokens = EXCLUDED.cache_creation_tokens,
                    cost_usd = EXCLUDED.cost_usd,
                    created_by = EXCLUDED.created_by,
                    created_at = now()
                """,
                (
                    repo, doc_type, ctx_hash, filename, markdown, model,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd, created_by,
                ),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("doc_artifacts save failed")


def record_usage(
    *,
    user_email: Optional[str],
    repo: str,
    doc_type: str,
    reused: bool,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    cost_usd: float,
    ctx_hash: str,
) -> None:
    try:
        conn = _connect()
        if conn is None:
            return
        with conn:
            conn.execute(
                """
                INSERT INTO doc_generation_usage
                    (user_email, repo, doc_type, reused, model, input_tokens,
                     output_tokens, cache_read_tokens, cache_creation_tokens,
                     cost_usd, context_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    user_email, repo, doc_type, reused, model, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    cost_usd, ctx_hash,
                ),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("doc_generation_usage record failed")


def usage_summary(user_email: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
    """Aggregate usage. If user_email is given, scope to that user only."""
    empty = {"by_user": [], "totals": {}, "recent": []}
    try:
        conn = _connect()
        if conn is None:
            return {**empty, "error": "DATABASE_URL not configured"}
        where = "WHERE user_email = %s" if user_email else ""
        params = (user_email,) if user_email else ()
        with conn:
            by_user = conn.execute(
                f"""
                SELECT user_email,
                       COUNT(*)                              AS generations,
                       COUNT(*) FILTER (WHERE reused)        AS reused_count,
                       COALESCE(SUM(input_tokens), 0)        AS input_tokens,
                       COALESCE(SUM(output_tokens), 0)       AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0)   AS cache_read_tokens,
                       COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                       COALESCE(SUM(cost_usd), 0)            AS cost_usd
                FROM doc_generation_usage
                {where}
                GROUP BY user_email
                ORDER BY cost_usd DESC
                """,
                params,
            ).fetchall()
            totals = conn.execute(
                f"""
                SELECT COUNT(*)                               AS generations,
                       COUNT(*) FILTER (WHERE reused)         AS reused_count,
                       COALESCE(SUM(input_tokens), 0)         AS input_tokens,
                       COALESCE(SUM(output_tokens), 0)        AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0)    AS cache_read_tokens,
                       COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                       COALESCE(SUM(cost_usd), 0)             AS cost_usd
                FROM doc_generation_usage
                {where}
                """,
                params,
            ).fetchone()
            recent = conn.execute(
                f"""
                SELECT user_email, repo, doc_type, reused, model,
                       input_tokens, output_tokens, cost_usd, created_at
                FROM doc_generation_usage
                {where}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (*params, limit),
            ).fetchall()
        return {
            "by_user": [dict(r) for r in by_user],
            "totals": dict(totals) if totals else {},
            "recent": [dict(r) for r in recent],
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("usage_summary failed")
        return {**empty, "error": str(exc)}
