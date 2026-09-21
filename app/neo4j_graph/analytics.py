"""Read-only analytics queries over the Neo4j code graph for the admin UI."""

from __future__ import annotations

import logging
from typing import Any

from neo4j import GraphDatabase

from .config import GraphBuildConfig

log = logging.getLogger(__name__)


def _rows(session, query: str, **params) -> list[dict[str, Any]]:
    return [dict(record) for record in session.run(query, **params)]


def _scalar(session, query: str, default=0):
    rec = session.run(query).single()
    if not rec:
        return default
    return list(rec.values())[0]


def graph_analytics(cfg: GraphBuildConfig | None = None) -> dict[str, Any]:
    """Summarise the current graph. Never raises — returns connected=False on error."""
    cfg = cfg or GraphBuildConfig.from_settings()
    if not cfg.neo4j_password:
        return {"connected": False, "error": "NEO4J_PASSWORD not configured"}

    driver = None
    try:
        driver = GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))
        driver.verify_connectivity()
        with driver.session(database=cfg.neo4j_database) as s:
            node_total = _scalar(s, "MATCH (n) RETURN count(n) AS c")
            rel_total = _scalar(s, "MATCH ()-[r]->() RETURN count(r) AS c")
            nodes_by_label = _rows(
                s,
                "MATCH (n) UNWIND labels(n) AS l "
                "RETURN l AS label, count(*) AS count ORDER BY count DESC",
            )
            rels_by_type = _rows(
                s,
                "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count "
                "ORDER BY count DESC",
            )
            repositories = _rows(
                s,
                "MATCH (r:Repo) RETURN "
                "  r.name AS name, "
                "  r.activity_score AS activity_score, "
                "  r.last_commit_days AS last_commit_days, "
                "  r.commits_90d AS commits_90d, "
                "  COUNT { (f:File)-[:IN_REPO]->(r) } AS files, "
                "  COUNT { (c:Commit)-[:IN_REPO]->(r) } AS commits, "
                "  COUNT { (fn:Function {repo: r.name}) } AS functions, "
                "  COUNT { (cl:Class {repo: r.name}) } AS classes "
                "ORDER BY activity_score DESC",
            )
            top_modules = _rows(
                s,
                "MATCH (f:File)-[:IMPORTS]->(m:Module) "
                "RETURN m.name AS name, count(DISTINCT f) AS imported_by "
                "ORDER BY imported_by DESC LIMIT 15",
            )
            top_called = _rows(
                s,
                "MATCH (:Function)-[c:CALLS]->(fn:Function) "
                "RETURN fn.name AS name, fn.repo AS repo, count(c) AS calls "
                "ORDER BY calls DESC LIMIT 15",
            )
            languages = _rows(
                s,
                "MATCH (f:File) WHERE f.ext <> '' "
                "RETURN f.ext AS ext, count(*) AS files "
                "ORDER BY files DESC LIMIT 15",
            )
        return {
            "connected": True,
            "uri": cfg.neo4j_uri,
            "database": cfg.neo4j_database,
            "node_total": node_total,
            "relationship_total": rel_total,
            "nodes_by_label": nodes_by_label,
            "relationships_by_type": rels_by_type,
            "repositories": repositories,
            "top_modules": top_modules,
            "top_called_functions": top_called,
            "languages": languages,
        }
    except Exception as exc:  # noqa: BLE001 - surface as connected=False to the UI
        log.warning("graph_analytics failed: %s", exc)
        return {"connected": False, "error": str(exc), "uri": cfg.neo4j_uri}
    finally:
        if driver is not None:
            driver.close()
