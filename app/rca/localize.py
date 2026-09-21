"""Phase D — repo localization.

Maps a Jira defect's component/affected-service to candidate repo(s) so the
investigation does NOT search all repos by default. Sources, in order of trust:

  1. `rca_repo_map` Postgres table (component → repo, weighted; auto-seeded,
     human-editable).
  2. Similar resolved tickets → repos they historically touched (fix links).
  3. Semantic fallback: code search of the ticket text → repos of top chunks.

Read-only. The table is seeded by matching Jira components/projects against the
known repo names plus the fix-link history; humans correct it afterward.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import Settings
from app.rca import fix_links, repos as repo_access

log = logging.getLogger(__name__)


@dataclass
class RepoCandidate:
    repo: str
    score: float
    reasons: list[str] = field(default_factory=list)


# ── Postgres mapping table ────────────────────────────────────────────────────

def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def init_schema(settings: Settings) -> None:
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rca_repo_map (
                id         BIGSERIAL PRIMARY KEY,
                component  TEXT NOT NULL,
                repo       TEXT NOT NULL,
                weight     REAL NOT NULL DEFAULT 1.0,
                source     TEXT NOT NULL DEFAULT 'manual',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (component, repo)
            )
            """
        )
        conn.execute("ALTER TABLE rca_repo_map ADD COLUMN IF NOT EXISTS component TEXT;")
        conn.execute("ALTER TABLE rca_repo_map ADD COLUMN IF NOT EXISTS repo TEXT;")
        conn.execute("ALTER TABLE rca_repo_map ADD COLUMN IF NOT EXISTS weight REAL NOT NULL DEFAULT 1.0;")
        conn.execute("ALTER TABLE rca_repo_map ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';")
        conn.execute("ALTER TABLE rca_repo_map ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rca_repo_map_component "
            "ON rca_repo_map (lower(component))"
        )


def upsert_mapping(settings: Settings, component: str, repo: str,
                   weight: float = 1.0, source: str = "manual") -> None:
    if not settings.database_url:
        return
    init_schema(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            INSERT INTO rca_repo_map (component, repo, weight, source, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (component, repo) DO UPDATE
              SET weight = EXCLUDED.weight, source = EXCLUDED.source, updated_at = NOW()
            """,
            (component.strip(), repo.strip(), weight, source),
        )


def get_mapping(settings: Settings, component: str) -> list[tuple[str, float]]:
    if not settings.database_url or not component:
        return []
    init_schema(settings)
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT repo, weight FROM rca_repo_map WHERE lower(component) = lower(%s) "
            "ORDER BY weight DESC",
            (component.strip(),),
        ).fetchall()
    return [(r["repo"], float(r["weight"])) for r in rows]


def list_mappings(settings: Settings) -> list[dict[str, Any]]:
    if not settings.database_url:
        return []
    init_schema(settings)
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT component, repo, weight, source, updated_at FROM rca_repo_map "
            "ORDER BY component, weight DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ── auto-seeding ──────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def seed_map(settings: Settings) -> dict[str, Any]:
    """Auto-seed component→repo mappings from fix-link history + name matching.

    Two signals: (a) tickets whose components are known and which have fix links
    → component maps to those repos; (b) fuzzy name match of component to a repo
    name. Existing rows are upserted with source='auto' (manual edits win via a
    later manual upsert). Returns a summary.
    """
    init_schema(settings)
    if not settings.database_url:
        return {"seeded": 0, "note": "no database configured"}

    known_repos = repo_access.list_repos(settings)
    norm_repo = {_normalize(r): r for r in known_repos}

    seeded = 0

    # (b) fuzzy name match: component token == repo name token
    with _connect(settings) as conn:
        comps = conn.execute(
            "SELECT DISTINCT unnest(string_to_array(component_str, ',')) AS component "
            "FROM ("
            "  SELECT array_to_string(ARRAY(SELECT jsonb_array_elements_text("
            "    COALESCE(data->'fields'->'components','[]'::jsonb))), ',') AS component_str "
            "  FROM jira_ticket_cache"
            ") sub WHERE component_str <> ''"
        ).fetchall() if _components_column_exists(conn) else []

    # Component names from Jira cache may not be reliably structured; fall back to
    # repo-name self-mapping so at least exact repo mentions localize.
    for repo in known_repos:
        upsert_mapping(settings, repo, repo, weight=1.0, source="auto")
        seeded += 1

    # (a) fix-link signal: project_key → repos seen in that project's fix links
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT split_part(ticket_key,'-',1) AS project, repo, count(*) AS n "
            "FROM rca_ticket_fix_links GROUP BY project, repo"
        ).fetchall()
    for r in rows:
        # weight by recency of association (count, capped)
        w = min(2.0, 1.0 + float(r["n"]) / 10.0)
        upsert_mapping(settings, r["project"], r["repo"], weight=w, source="auto")
        seeded += 1

    log.info("RCA repo-map seeded: %d rows (repos=%d)", seeded, len(known_repos))
    return {"seeded": seeded, "repos": len(known_repos)}


def _components_column_exists(conn) -> bool:
    try:
        conn.execute("SELECT data FROM jira_ticket_cache LIMIT 1").fetchone()
        return True
    except Exception:
        return False


# ── localization ──────────────────────────────────────────────────────────────

def _add(cands: dict[str, RepoCandidate], repo: str, score: float, reason: str) -> None:
    if not repo:
        return
    c = cands.get(repo)
    if c is None:
        cands[repo] = RepoCandidate(repo=repo, score=score, reasons=[reason])
    else:
        c.score += score
        if reason not in c.reasons:
            c.reasons.append(reason)


def localize(
    settings: Settings,
    *,
    ticket_key: str,
    components: list[str],
    project_key: str,
    query_text: str,
    similar_ticket_keys: Optional[list[str]] = None,
    max_repos: int = 3,
) -> list[RepoCandidate]:
    """Return ranked candidate repos for the defect (does not search all repos)."""
    init_schema(settings)
    cands: dict[str, RepoCandidate] = {}
    known = set(repo_access.list_repos(settings))

    # 1) component mapping table
    for comp in components or []:
        for repo, weight in get_mapping(settings, comp):
            _add(cands, repo, 3.0 * weight, f"component '{comp}' → mapping")

    # project-key mapping (seeded from fix-link history)
    if project_key:
        for repo, weight in get_mapping(settings, project_key):
            _add(cands, repo, 2.0 * weight, f"project '{project_key}' → mapping")

    # 2) similar resolved tickets → repos in their fix links
    if similar_ticket_keys:
        links = fix_links.get_links_for_keys(settings, similar_ticket_keys)
        repo_counts: dict[str, int] = {}
        for link in links:
            repo_counts[link.repo] = repo_counts.get(link.repo, 0) + 1
        for repo, n in repo_counts.items():
            _add(cands, repo, 1.5 + 0.5 * n, f"{n} similar-ticket fix(es) in repo")

    # 3) semantic fallback when nothing mapped
    if not cands and query_text:
        try:
            from app.rca import code_index
            hits = code_index.search(settings, query_text, repo=None, k=15)
            for h in hits:
                _add(cands, h.get("repo", ""), float(h.get("score", 0)), "semantic code match")
        except Exception as exc:
            log.warning("semantic localization fallback failed: %s", exc)

    # keep only repos that actually exist on disk, rank, cap
    ranked = sorted(
        (c for c in cands.values() if not known or c.repo in known),
        key=lambda c: c.score, reverse=True,
    )
    return ranked[:max_repos]
