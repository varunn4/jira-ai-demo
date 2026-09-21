"""Phase B — ticket → fixing-commit/files linkage (read-only signal).

For each ticket we resolve the commit(s) that reference its key (smart commits /
"RFT-123 fix ...") and the files those commits changed. This is used ONLY as a
retrieval signal in localization ("a similar resolved ticket historically
touched these files") and as ground-truth labels in the offline eval — never to
copy or generate a fix.

Primary source is the Neo4j commit graph (already loaded: Commit.message +
Commit-[:TOUCHES]->File). A `git log --grep` fallback covers repos not yet in
the graph. Output is persisted to Postgres `rca_ticket_fix_links`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app.config import Settings
from app.rca import repos

log = logging.getLogger(__name__)

# Jira keys: PROJECT-123 with a word boundary so RFT-47 doesn't match RFT-475.
_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _key_in_message(key: str, message: str) -> bool:
    return any(found == key for found in _KEY_RE.findall(message or ""))


@dataclass(frozen=True)
class FixLink:
    ticket_key: str
    repo: str
    commit_sha: str
    changed_files: list[str]
    source: str  # "neo4j" | "git"


# ── Postgres persistence ──────────────────────────────────────────────────────

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
            CREATE TABLE IF NOT EXISTS rca_ticket_fix_links (
                id            BIGSERIAL PRIMARY KEY,
                ticket_key    TEXT NOT NULL,
                repo          TEXT NOT NULL,
                commit_sha    TEXT NOT NULL,
                changed_files JSONB NOT NULL DEFAULT '[]'::jsonb,
                source        TEXT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (ticket_key, repo, commit_sha)
            )
            """
        )
        conn.execute("ALTER TABLE rca_ticket_fix_links ADD COLUMN IF NOT EXISTS repo TEXT;")
        conn.execute("ALTER TABLE rca_ticket_fix_links ADD COLUMN IF NOT EXISTS changed_files JSONB NOT NULL DEFAULT '[]'::jsonb;")
        conn.execute("ALTER TABLE rca_ticket_fix_links ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'git';")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rca_fix_links_key "
            "ON rca_ticket_fix_links (ticket_key)"
        )


def _upsert_links(settings: Settings, links: Iterable[FixLink]) -> int:
    if not settings.database_url:
        return 0
    init_schema(settings)
    rows = list(links)
    if not rows:
        return 0
    with _connect(settings) as conn:
        for link in rows:
            conn.execute(
                """
                INSERT INTO rca_ticket_fix_links
                    (ticket_key, repo, commit_sha, changed_files, source)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (ticket_key, repo, commit_sha) DO UPDATE
                  SET changed_files = EXCLUDED.changed_files,
                      source = EXCLUDED.source
                """,
                (link.ticket_key, link.repo, link.commit_sha,
                 json.dumps(link.changed_files), link.source),
            )
    return len(rows)


def get_links(settings: Settings, ticket_key: str) -> list[FixLink]:
    if not settings.database_url:
        return []
    init_schema(settings)
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT ticket_key, repo, commit_sha, changed_files, source "
            "FROM rca_ticket_fix_links WHERE ticket_key = %s",
            (ticket_key,),
        ).fetchall()
    return [
        FixLink(r["ticket_key"], r["repo"], r["commit_sha"],
                list(r["changed_files"] or []), r["source"])
        for r in rows
    ]


def get_links_for_keys(settings: Settings, ticket_keys: list[str]) -> list[FixLink]:
    """All fix links for a set of ticket keys."""
    if not settings.database_url:
        return []
    init_schema(settings)
    if not settings.database_url or not ticket_keys:
        return []
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT ticket_key, repo, commit_sha, changed_files, source "
            "FROM rca_ticket_fix_links WHERE ticket_key = ANY(%s)",
            (list(ticket_keys),),
        ).fetchall()
    return [
        FixLink(r["ticket_key"], r["repo"], r["commit_sha"],
                list(r["changed_files"] or []), r["source"])
        for r in rows
    ]


def changed_files_for(settings: Settings, ticket_keys: list[str]) -> dict[str, list[str]]:
    """Aggregate changed files per ticket for a set of keys (retrieval signal)."""
    if not settings.database_url or not ticket_keys:
        return {}
    with _connect(settings) as conn:
        rows = conn.execute(
            "SELECT ticket_key, changed_files FROM rca_ticket_fix_links "
            "WHERE ticket_key = ANY(%s)",
            (list(ticket_keys),),
        ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["ticket_key"], [])
        out[r["ticket_key"]].extend(r["changed_files"] or [])
    return out


# ── Neo4j-backed linkage ──────────────────────────────────────────────────────

def _neo4j_driver(settings: Settings):
    try:
        from neo4j import GraphDatabase
        uri = settings.neo4j_uri
        if "localhost" in uri or "127.0.0.1" in uri:
            uri = "bolt://jira-ai-neo4j:7687"
        password = settings.neo4j_password if settings.neo4j_password and settings.neo4j_password != "your-neo4j-password" else "password"
        return GraphDatabase.driver(uri, auth=(settings.neo4j_user, password))
    except Exception as exc:
        log.warning("Neo4j driver creation failed: %s", exc)
        return None


def link_ticket_neo4j(settings: Settings, ticket_key: str) -> list[FixLink]:
    """Resolve a single ticket's fixing commits + files from the graph."""
    driver = None
    try:
        driver = _neo4j_driver(settings)
        if driver is None:
            return []
        with driver.session(database=settings.neo4j_database) as s:
            records = s.run(
                """
                MATCH (c:Commit)
                WHERE c.message CONTAINS $key
                OPTIONAL MATCH (c)-[:TOUCHES]->(f:File)
                RETURN c.repo AS repo, c.hash AS sha, c.message AS message,
                       collect(DISTINCT f.path) AS files
                """,
                key=ticket_key,
            )
            links: list[FixLink] = []
            for rec in records:
                if not _key_in_message(ticket_key, rec["message"] or ""):
                    continue  # CONTAINS is loose; enforce word-boundary match
                repo = rec["repo"] or ""
                sha = rec["sha"] or ""
                files = [p for p in (rec["files"] or []) if p]
                # Not every commit carries TOUCHES edges (merge/capped commits);
                # fill from the on-disk repo when the graph has none.
                if not files and repo and sha:
                    files = repos.commit_files(settings, repo, sha)
                links.append(FixLink(
                    ticket_key=ticket_key, repo=repo, commit_sha=sha,
                    changed_files=files, source="neo4j",
                ))
            return [l for l in links if l.repo and l.commit_sha]
    except Exception as exc:
        log.warning("link_ticket_neo4j error for %s: %s", ticket_key, exc)
        return []
    finally:
        if driver:
            try:
                driver.close()
            except Exception:
                pass


def backfill_neo4j(
    settings: Settings,
    keys: set[str],
) -> list[FixLink]:
    """Single-pass linkage for many tickets: scan commits once, match keys.

    Streams all commits from the graph, regex-extracts keys, intersects with the
    provided set, then resolves touched files for matched commits in one query.
    """
    driver = None
    try:
        driver = _neo4j_driver(settings)
        if driver is None:
            return []
        matched: dict[str, list[tuple[str, str]]] = {}  # key -> [(repo, sha)]
        sha_to_pair: dict[str, tuple[str, str]] = {}
        with driver.session(database=settings.neo4j_database) as s:
            for rec in s.run("MATCH (c:Commit) RETURN c.repo AS repo, c.hash AS sha, c.message AS message"):
                msg = rec["message"] or ""
                sha = rec["sha"] or ""
                repo = rec["repo"] or ""
                if not sha:
                    continue
                for found in set(_KEY_RE.findall(msg)):
                    if found in keys:
                        matched.setdefault(found, []).append((repo, sha))
                        sha_to_pair[sha] = (repo, sha)

            if not sha_to_pair:
                return []

            files_by_sha: dict[str, list[str]] = {}
            shas = list(sha_to_pair.keys())
            for i in range(0, len(shas), 1000):
                batch = shas[i : i + 1000]
                for rec in s.run(
                    """
                    MATCH (c:Commit)-[:TOUCHES]->(f:File)
                    WHERE c.hash IN $shas
                    RETURN c.hash AS sha, collect(DISTINCT f.path) AS files
                    """,
                    shas=batch,
                ):
                    files_by_sha[rec["sha"]] = [p for p in (rec["files"] or []) if p]
    except Exception as exc:
        log.warning("backfill_neo4j error: %s", exc)
        return []
    finally:
        if driver:
            try:
                driver.close()
            except Exception:
                pass

    links: list[FixLink] = []
    on_disk = set(repos.list_repos(settings))
    for key, pairs in matched.items():
        for repo, sha in pairs:
            if not repo:
                continue
            files = files_by_sha.get(sha, [])
            if not files and repo in on_disk:
                files = repos.commit_files(settings, repo, sha)
            links.append(FixLink(
                ticket_key=key, repo=repo, commit_sha=sha,
                changed_files=files, source="neo4j",
            ))
    return links


# ── git fallback (repos not in the graph) ─────────────────────────────────────

def link_ticket_git(settings: Settings, ticket_key: str, repo: str) -> list[FixLink]:
    """Resolve a ticket's fixing commits + files in one repo via `git log --grep`."""
    try:
        out = repos._git(
            settings, repo, "log", "--all", "-i", f"--grep={ticket_key}",
            "--pretty=format:%H%x1f%s", "--name-only",
        )
    except repos.RepoAccessError:
        return []

    links: list[FixLink] = []
    cur_sha: Optional[str] = None
    cur_msg = ""
    cur_files: list[str] = []

    def _flush():
        if cur_sha and _key_in_message(ticket_key, cur_msg):
            links.append(FixLink(ticket_key, repo, cur_sha, list(cur_files), "git"))

    for line in out.splitlines():
        if "\x1f" in line:
            _flush()
            sha, msg = line.split("\x1f", 1)
            cur_sha, cur_msg, cur_files = sha, msg, []
        elif line.strip():
            cur_files.append(line.strip())
    _flush()
    return links


# ── orchestration ─────────────────────────────────────────────────────────────

def link_ticket(settings: Settings, ticket_key: str, *, persist: bool = True) -> list[FixLink]:
    """Resolve and (optionally) persist one ticket's fix linkage.

    Tries the Neo4j graph first; falls back to scanning on-disk repos with git
    when the graph yields nothing.
    """
    init_schema(settings)
    links = link_ticket_neo4j(settings, ticket_key)
    if not links:
        for repo in repos.list_repos(settings):
            links.extend(link_ticket_git(settings, ticket_key, repo))
    if persist:
        _upsert_links(settings, links)
    log.info("Linked ticket %s → %d fixing commit(s)", ticket_key, len(links))
    return links


def _resolved_ticket_keys(settings: Settings, project_keys: Optional[list[str]], limit: int) -> set[str]:
    """Keys of cached tickets to backfill (defaults to all cached tickets)."""
    if not settings.database_url:
        return set()
    sql = "SELECT ticket_key FROM jira_ticket_cache"
    params: list[Any] = []
    if project_keys:
        sql += " WHERE project_key = ANY(%s)"
        params.append(project_keys)
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    with _connect(settings) as conn:
        rows = conn.execute(sql, params or None).fetchall()
    return {r["ticket_key"] for r in rows if r["ticket_key"]}


def backfill(
    settings: Settings,
    project_keys: Optional[list[str]] = None,
    limit: int = 0,
) -> dict[str, Any]:
    """Backfill linkage for cached tickets. Returns a summary."""
    init_schema(settings)
    keys = _resolved_ticket_keys(settings, project_keys, limit)
    if not keys:
        return {"tickets_considered": 0, "links_written": 0, "tickets_linked": 0}

    links = backfill_neo4j(settings, keys)
    written = _upsert_links(settings, links)
    linked_keys = {l.ticket_key for l in links}
    log.info(
        "Fix-link backfill: %d tickets considered, %d links across %d tickets",
        len(keys), written, len(linked_keys),
    )
    return {
        "tickets_considered": len(keys),
        "links_written": written,
        "tickets_linked": len(linked_keys),
    }
