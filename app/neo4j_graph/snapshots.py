"""Persist graph-analytics snapshots so the UI can show up/down trends.

A snapshot is a compact summary of the graph (totals + per-label / per-rel /
per-language counts) written to Postgres at the end of each build. The analytics
endpoint compares the current live numbers against the most recent *different*
snapshot and returns a per-metric delta (▲/▼ N) for the dashboard.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.config import Settings

log = logging.getLogger(__name__)


def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.database_url, row_factory=dict_row)


def ensure_schema(settings: Settings) -> None:
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS neo4j_graph_snapshots (
                id                 BIGSERIAL PRIMARY KEY,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                node_total         BIGINT,
                relationship_total BIGINT,
                repository_count   INT,
                metrics            JSONB NOT NULL
            )
            """
        )
        conn.commit()


def _metrics_from_analytics(analytics: dict[str, Any]) -> dict[str, Any]:
    """Reduce a full analytics payload to the compact counters we trend on."""
    by_label = {r["label"]: r["count"] for r in analytics.get("nodes_by_label", [])}
    by_rel = {r["type"]: r["count"] for r in analytics.get("relationships_by_type", [])}
    by_lang = {r["ext"]: r["files"] for r in analytics.get("languages", [])}
    return {
        "node_total": analytics.get("node_total", 0),
        "relationship_total": analytics.get("relationship_total", 0),
        "repository_count": len(analytics.get("repositories", []) or []),
        "functions": by_label.get("Function", 0),
        "by_label": by_label,
        "by_rel": by_rel,
        "by_language": by_lang,
    }


def save_snapshot(settings: Settings, analytics: dict[str, Any]) -> None:
    """Record the current graph state. No-op without a database or connection."""
    if not settings.database_url or not analytics.get("connected"):
        return
    try:
        ensure_schema(settings)
        m = _metrics_from_analytics(analytics)
        with _connect(settings) as conn:
            conn.execute(
                """
                INSERT INTO neo4j_graph_snapshots
                    (node_total, relationship_total, repository_count, metrics)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (m["node_total"], m["relationship_total"], m["repository_count"],
                 json.dumps(m, ensure_ascii=False)),
            )
            conn.commit()
        log.info("Saved Neo4j graph snapshot (%s nodes, %s rels)",
                 m["node_total"], m["relationship_total"])
    except Exception as exc:  # noqa: BLE001
        log.debug("snapshot save skipped: %s", exc)


def _recent_snapshots(settings: Settings, limit: int = 12) -> list[dict[str, Any]]:
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT created_at, node_total, relationship_total, metrics "
            "FROM neo4j_graph_snapshots ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return rows


def _delta_map(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    keys = set(current) | set(previous)
    return {k: int(current.get(k, 0)) - int(previous.get(k, 0)) for k in keys}


def attach_trends(settings: Settings, analytics: dict[str, Any]) -> dict[str, Any]:
    """Augment a live analytics payload with `trends` vs the previous snapshot.

    Bootstraps a baseline row the first time it runs so a later build produces
    visible arrows. Never raises — on any failure trends are simply omitted.
    """
    analytics = dict(analytics)
    analytics["trends"] = {"available": False}
    if not settings.database_url or not analytics.get("connected"):
        return analytics

    try:
        ensure_schema(settings)
        current = _metrics_from_analytics(analytics)
        rows = _recent_snapshots(settings)

        if not rows:
            # First ever call: seed a baseline so the next build shows a delta.
            save_snapshot(settings, analytics)
            return analytics

        # Baseline = most recent snapshot whose totals differ from the live graph
        # (skips the snapshot the latest build just wrote, which equals current).
        baseline = None
        for row in rows:
            if (row["node_total"] != current["node_total"]
                    or row["relationship_total"] != current["relationship_total"]):
                baseline = row
                break
        if baseline is None:
            return analytics  # nothing changed since the last recorded state

        prev = baseline["metrics"] or {}
        analytics["trends"] = {
            "available": True,
            "baseline_at": baseline["created_at"].isoformat(),
            "totals": {
                "node_total": current["node_total"] - int(prev.get("node_total", 0)),
                "relationship_total": current["relationship_total"] - int(prev.get("relationship_total", 0)),
                "repository_count": current["repository_count"] - int(prev.get("repository_count", 0)),
                "functions": current["functions"] - int(prev.get("functions", 0)),
            },
            "by_label": _delta_map(current["by_label"], prev.get("by_label", {})),
            "by_rel": _delta_map(current["by_rel"], prev.get("by_rel", {})),
            "by_language": _delta_map(current["by_language"], prev.get("by_language", {})),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("trend computation skipped: %s", exc)
    return analytics
