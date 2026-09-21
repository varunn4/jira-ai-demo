"""Ground a Jira ticket in the Neo4j code graph for effort estimation (WF7).

Given a ticket's text we derive keywords, find the code that most likely relates
to it (files / functions / modules), and surface estimation-relevant signals:

* file size      — functions & classes defined in a matched file (complexity)
* churn          — how many commits touched it (volatility / risk)
* blast radius    — how many functions CALL a matched function (reuse / coupling)

The reader holds one pooled driver and is reused across a whole WF7 batch, so a
daily run issues a handful of queries rather than one connection per ticket.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from neo4j import GraphDatabase

from .config import GraphBuildConfig

log = logging.getLogger(__name__)

_STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "your", "you",
    "are", "was", "were", "will", "should", "would", "could", "have", "has",
    "add", "added", "fix", "fixed", "issue", "ticket", "jira", "please", "update",
    "updated", "create", "created", "change", "changed", "support", "new", "page",
    "api", "user", "users", "data", "test", "testing", "feature", "bug", "task",
    "story", "implement", "implementation", "need", "needed", "required", "want",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def extract_keywords(ticket: dict[str, Any], limit: int = 8) -> list[str]:
    """Distinct, meaningful, lowercased tokens from the ticket text + names."""
    parts: list[str] = []
    for key in ("summary", "title", "description", "description_text"):
        val = ticket.get(key)
        if val:
            parts.append(str(val))
    for key in ("components", "labels"):
        val = ticket.get(key) or []
        if isinstance(val, (list, tuple)):
            parts.extend(str(v) for v in val)

    text = " ".join(parts)
    seen: set[str] = set()
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        # split camelCase / snake_case identifiers into their sub-words too
        for piece in [raw] + _CAMEL_RE.findall(raw) + raw.split("_"):
            tok = piece.lower()
            if len(tok) < 3 or tok in _STOP_WORDS or tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
            if len(out) >= limit:
                return out
    return out


class GraphTicketContext:
    def __init__(self, cfg: GraphBuildConfig):
        self.cfg = cfg
        self._driver = GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:  # noqa: BLE001
            pass

    def _query(self, cypher: str, **params) -> list[dict[str, Any]]:
        with self._driver.session(database=self.cfg.neo4j_database) as s:
            return [dict(r) for r in s.run(cypher, **params)]

    def context_for(self, ticket: dict[str, Any], *, limit: int = 6) -> dict[str, Any]:
        kws = extract_keywords(ticket)
        if not kws:
            return {"keywords": [], "files": [], "functions": [], "modules": [], "repos": []}

        files = self._query(
            "UNWIND $kws AS kw "
            "MATCH (f:File) "
            "WHERE toLower(f.name) CONTAINS kw OR toLower(f.path) CONTAINS kw "
            "WITH DISTINCT f "
            "WITH f, "
            "  COUNT { (fn:Function {repo: f.repo, file: f.path}) } AS functions, "
            "  COUNT { (cl:Class {repo: f.repo, file: f.path}) } AS classes, "
            "  COUNT { (:Commit)-[:TOUCHES]->(f) } AS commits "
            "RETURN f.repo AS repo, f.path AS path, functions, classes, commits "
            "ORDER BY commits DESC, functions DESC LIMIT $limit",
            kws=kws, limit=limit,
        )
        functions = self._query(
            "UNWIND $kws AS kw "
            "MATCH (fn:Function) WHERE toLower(fn.name) CONTAINS kw "
            "WITH DISTINCT fn "
            "WITH fn, COUNT { (:Function)-[:CALLS]->(fn) } AS callers "
            "RETURN fn.repo AS repo, fn.name AS name, fn.file AS file, callers "
            "ORDER BY callers DESC LIMIT $limit",
            kws=kws, limit=limit,
        )
        modules = self._query(
            "UNWIND $kws AS kw "
            "MATCH (m:Module) WHERE toLower(m.name) CONTAINS kw "
            "WITH DISTINCT m "
            "RETURN m.name AS name, COUNT { (:File)-[:IMPORTS]->(m) } AS imported_by "
            "ORDER BY imported_by DESC LIMIT $limit",
            kws=kws, limit=limit,
        )
        repos = sorted({r["repo"] for r in files} | {r["repo"] for r in functions})
        return {"keywords": kws, "files": files, "functions": functions,
                "modules": modules, "repos": repos}

    def text_for(self, ticket: dict[str, Any], *, max_chars: int = 1800) -> str:
        ctx = self.context_for(ticket)
        if not (ctx["files"] or ctx["functions"] or ctx["modules"]):
            return ""
        lines: list[str] = ["Relevant code from the Neo4j knowledge graph "
                            "(churn = commits touching the file; callers = how many functions call it):"]
        if ctx["repos"]:
            lines.append(f"Repositories: {', '.join(ctx['repos'])}")
        if ctx["files"]:
            lines.append("Files:")
            for f in ctx["files"]:
                lines.append(
                    f"  - {f['repo']}/{f['path']} "
                    f"({f['functions']} functions, {f['classes']} classes, {f['commits']} commits)"
                )
        if ctx["functions"]:
            lines.append("Functions:")
            for fn in ctx["functions"]:
                lines.append(f"  - {fn['name']} in {fn['repo']}/{fn['file']} ({fn['callers']} callers)")
        if ctx["modules"]:
            mods = ", ".join(f"{m['name']}({m['imported_by']})" for m in ctx["modules"])
            lines.append(f"Libraries/modules: {mods}")
        return "\n".join(lines)[:max_chars]


def open_reader(cfg: GraphBuildConfig | None = None) -> GraphTicketContext | None:
    """Return a connected reader, or None if Neo4j is unconfigured/unreachable."""
    cfg = cfg or GraphBuildConfig.from_settings()
    if not cfg.neo4j_password:
        return None
    try:
        reader = GraphTicketContext(cfg)
        reader._driver.verify_connectivity()
        return reader
    except Exception as exc:  # noqa: BLE001
        log.info("Neo4j ticket-context unavailable: %s", exc)
        return None
