"""Phase E — hybrid retrieval with reciprocal rank fusion (RRF).

Runs five read-only retrievers over the localized repo(s) and fuses their ranked
outputs into one candidate set, each candidate tagged with which retriever(s)
surfaced it:

  1. exact-grep of extracted error strings (on-disk repo working tree)
  2. stack-frame symbols resolved to code locations via the Neo4j graph
  3. similar resolved tickets → files their fixing commits changed
  4. semantic code search over the `code_chunks` Qdrant collection
  5. recent-change weighting via `git log` on candidate files

All retrievers read only; none mutate a repo or run code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import Settings
from app.rca import fix_links, repos as repo_access

log = logging.getLogger(__name__)

_RRF_K = 60  # standard RRF damping constant


@dataclass
class Candidate:
    repo: str
    file_path: str
    symbol: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    retrievers: set[str] = field(default_factory=set)
    score: float = 0.0
    evidence: list[dict[str, str]] = field(default_factory=list)

    def key(self) -> tuple[str, str]:
        return (self.repo, self.file_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "file_path": self.file_path, "symbol": self.symbol,
            "start_line": self.start_line, "end_line": self.end_line,
            "retrievers": sorted(self.retrievers), "score": round(self.score, 4),
            "evidence": self.evidence,
        }


def _fuse(fused: dict[tuple[str, str], Candidate], ranked: list[Candidate],
          retriever: str) -> None:
    """Merge one retriever's ranked list into the fused set via RRF."""
    for rank, cand in enumerate(ranked):
        contrib = 1.0 / (_RRF_K + rank + 1)
        existing = fused.get(cand.key())
        if existing is None:
            cand.retrievers.add(retriever)
            cand.score = contrib
            fused[cand.key()] = existing = cand
        else:
            existing.score += contrib
            existing.retrievers.add(retriever)
            # enrich line/symbol info if this retriever has it and prior didn't
            existing.symbol = existing.symbol or cand.symbol
            existing.start_line = existing.start_line or cand.start_line
            existing.end_line = existing.end_line or cand.end_line
        existing.evidence.extend(cand.evidence)


# ── retriever 1: exact grep of error strings ──────────────────────────────────

def _r_error_grep(settings: Settings, repos: list[str],
                  error_messages: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for repo in repos:
        for msg in error_messages:
            # search a distinctive slice of the error (full strings rarely match)
            needle = msg.strip()[:80]
            if len(needle) < 4:
                continue
            try:
                hits = repo_access.grep(settings, repo, needle, is_regex=False, max_matches=10)
            except repo_access.RepoAccessError:
                continue
            for h in hits:
                k = (repo, h["path"])
                if k in seen:
                    continue
                seen.add(k)
                out.append(Candidate(
                    repo=repo, file_path=h["path"], start_line=h["line"],
                    evidence=[{"type": "matched_error_string",
                               "detail": f"'{needle}' at {h['path']}:{h['line']}"}],
                ))
    return out


# ── retriever 2: stack-frame symbols → graph locations ────────────────────────

def _r_stack_symbols(settings: Settings, repos: list[str],
                     stack_frames: list[dict[str, Any]]) -> list[Candidate]:
    symbols = [f.get("symbol") for f in stack_frames if f.get("symbol")]
    if not symbols:
        return []
    locations = resolve_symbols(settings, repos, symbols)
    out: list[Candidate] = []
    for loc in locations:
        out.append(Candidate(
            repo=loc["repo"], file_path=loc["file"], symbol=loc["symbol"],
            start_line=loc["start_line"], end_line=loc["end_line"],
            evidence=[{"type": "stack_frame_symbol",
                       "detail": f"{loc['symbol']} → {loc['repo']}/{loc['file']}:"
                                 f"{loc['start_line']}"}],
        ))
    return out


def resolve_symbols(settings: Settings, repos: list[str],
                    symbols: list[str]) -> list[dict[str, Any]]:
    """Resolve symbol names to file+line via the Neo4j Function/Class graph."""
    if not settings.neo4j_password or not symbols:
        return []
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(settings.neo4j_uri,
                                  auth=(settings.neo4j_user, settings.neo4j_password))
    out: list[dict[str, Any]] = []
    try:
        with driver.session(database=settings.neo4j_database) as s:
            for rec in s.run(
                """
                UNWIND $symbols AS sym
                MATCH (fn:Function {name: sym})
                WHERE fn.repo IN $repos
                RETURN fn.repo AS repo, fn.file AS file, fn.name AS symbol,
                       fn.start_line AS start_line, fn.end_line AS end_line
                LIMIT 50
                """,
                symbols=symbols, repos=repos,
            ):
                out.append(dict(rec))
    finally:
        driver.close()
    return out


# ── retriever 3: similar tickets → changed files ──────────────────────────────

def _r_similar_fixes(settings: Settings, repos: list[str],
                     similar_ticket_keys: list[str]) -> list[Candidate]:
    if not similar_ticket_keys:
        return []
    links = fix_links.get_links_for_keys(settings, similar_ticket_keys)
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        if repos and link.repo not in repos:
            continue
        for path in link.changed_files:
            k = (link.repo, path)
            if k in seen:
                continue
            seen.add(k)
            out.append(Candidate(
                repo=link.repo, file_path=path,
                evidence=[{"type": "similar_ticket_file",
                           "detail": f"{link.ticket_key} fix touched {path}"}],
            ))
    return out


# ── retriever 4: semantic code search ─────────────────────────────────────────

def _r_semantic(settings: Settings, repos: list[str], query_text: str) -> list[Candidate]:
    if not query_text:
        return []
    out: list[Candidate] = []
    try:
        from app.rca import code_index
        for repo in repos or [None]:
            for h in code_index.search(settings, query_text, repo=repo, k=8):
                out.append(Candidate(
                    repo=h["repo"], file_path=h["file_path"], symbol=h.get("symbol"),
                    start_line=h.get("start_line"), end_line=h.get("end_line"),
                    evidence=[{"type": "semantic_code_match",
                               "detail": f"{h.get('symbol')} score={h.get('score')}"}],
                ))
    except Exception as exc:
        log.warning("semantic retriever failed: %s", exc)
    return out


# ── retriever 5: recent-change weighting ──────────────────────────────────────

def _r_recency(settings: Settings, candidates: list[Candidate]) -> list[Candidate]:
    """Rank existing candidate files by how recently they changed (git log)."""
    scored: list[tuple[float, Candidate]] = []
    for cand in candidates:
        try:
            log_rows = repo_access.git_log(settings, cand.repo, cand.file_path, limit=1)
        except repo_access.RepoAccessError:
            continue
        if not log_rows:
            continue
        cand_copy = Candidate(repo=cand.repo, file_path=cand.file_path,
                              evidence=[{"type": "recent_change",
                                         "detail": f"last commit {log_rows[0]['date']} "
                                                   f"{log_rows[0]['subject'][:60]}"}])
        scored.append((log_rows[0]["date"], cand_copy))
    # most-recent first
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored]


# ── orchestration ─────────────────────────────────────────────────────────────

def retrieve(
    settings: Settings,
    *,
    repos: list[str],
    error_messages: list[str],
    stack_frames: list[dict[str, Any]],
    query_text: str,
    similar_ticket_keys: list[str],
    top_k: int = 12,
) -> list[Candidate]:
    """Run all five retrievers and fuse by RRF. Returns ranked candidates."""
    fused: dict[tuple[str, str], Candidate] = {}

    _fuse(fused, _r_error_grep(settings, repos, error_messages), "error_grep")
    _fuse(fused, _r_stack_symbols(settings, repos, stack_frames), "stack_symbol")
    _fuse(fused, _r_similar_fixes(settings, repos, similar_ticket_keys), "similar_fix")
    _fuse(fused, _r_semantic(settings, repos, query_text), "semantic")

    # recency re-weights the files surfaced so far
    _fuse(fused, _r_recency(settings, list(fused.values())), "recency")

    ranked = sorted(fused.values(), key=lambda c: c.score, reverse=True)
    log.info("RCA retrieval: %d candidates across %d repos", len(ranked), len(repos))
    return ranked[:top_k]
