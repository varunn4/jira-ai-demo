"""Repo auto-detection for JIRA tickets.

Strategy:

1. Keyword match: tokenize the ticket text, score each repo by how many of
   its name/description tokens appear in the ticket. This catches obvious
   cases ("Fix order sync for Newme brand" -> order-sync-workers) without
   any LLM call.

2. Ask the LLM to rank repos using one-line descriptions, architecture
   previews, and Repomix directory previews. Keyword matches are retained as a
   deterministic fallback if the LLM returns no valid repo.

The selected repo(s) are then used to ground the test-generation prompt.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..config import Config, RepoConfig
from ..llm import LLMClient
from ..sections import parse_sections

log = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "have", "has",
    "are", "was", "were", "but", "not", "all", "any", "can", "should",
    "would", "could", "user", "users", "need", "needs", "test", "tests",
    "issue", "ticket", "bug", "fix", "fixed", "add", "added", "new",
    "update", "updated", "change", "changed", "into", "when", "then",
    "given", "must", "doesn", "isn", "wasn", "didn", "use", "using",
}


def _stem(token: str) -> str:
    """Cheap plural-strip so 'invoice' matches 'invoices'. Not a real stemmer
    — just enough to catch the most common case without pulling in NLTK."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text: str) -> set[str]:
    return {
        _stem(t.lower())
        for t in _TOKEN_RE.findall(text)
        if t.lower() not in _STOPWORDS
    }


@dataclass
class RepoMatch:
    repo_name: str
    score: float
    reason: str


def keyword_match_repos(
    ticket_text: str, repos: List[RepoConfig], per_repo_dir: Path,
) -> List[RepoMatch]:
    """Score every repo by keyword overlap with the ticket. Higher is better.

    Signals (each contributes points):
      - Repo name tokens appearing in the ticket (3 pts each)
      - Description tokens appearing in the ticket (1 pt each)
      - File paths from the repo's ROUTES/DATA_MODELS section appearing in
        the ticket (2 pts each — strong signal that the ticket touches that
        repo's code)
    """
    ticket_tokens = _tokenize(ticket_text)
    if not ticket_tokens:
        return []

    matches: List[RepoMatch] = []
    for repo in repos:
        score = 0.0
        reasons: list[str] = []

        # Name tokens
        name_tokens = _tokenize(repo.name.replace("-", " ").replace("_", " "))
        name_hits = name_tokens & ticket_tokens
        if name_hits:
            score += 3 * len(name_hits)
            reasons.append(f"name:{','.join(sorted(name_hits))}")

        # Description tokens
        desc_tokens = _tokenize(repo.description or "")
        desc_hits = desc_tokens & ticket_tokens
        if desc_hits:
            score += 1 * len(desc_hits)
            reasons.append(f"desc:{','.join(sorted(desc_hits))}")

        # File path tokens from the existing arch map (if any)
        md_file = per_repo_dir / f"{repo.name}.md"
        if md_file.exists():
            try:
                sections = parse_sections(md_file.read_text())
                path_text = sections.routes + "\n" + sections.data_models
                path_tokens = _tokenize(path_text)
                path_hits = path_tokens & ticket_tokens
                # Cap path contribution so a repo with a huge maps doesn't
                # auto-win every ticket.
                if path_hits:
                    capped = min(len(path_hits), 5)
                    score += 2 * capped
                    reasons.append(f"paths:{','.join(sorted(list(path_hits)[:5]))}")
            except Exception:
                pass  # Bad map; ignore for scoring

        if score > 0:
            matches.append(RepoMatch(
                repo_name=repo.name,
                score=score,
                reason=" | ".join(reasons),
            ))

    matches.sort(key=lambda m: m.score, reverse=True)
    return matches


# ============================================================
# LLM-based ranking (fallback when keyword match is ambiguous)
# ============================================================

_LLM_RANK_SYSTEM = """You are routing a JIRA ticket to the most relevant repository.
You will see a ticket and a list of repos with one-line descriptions and the
first lines of their architecture map plus Repomix directory previews when available.

Output ONLY the repo names you think are most likely to need code changes for
this ticket, one per line, most-relevant first. Maximum 2 repos. If no repo
seems relevant, output the single line: NONE.

No explanations. No prose. Just repo names or NONE."""


def llm_rank_repos(
    ticket_text: str, repos: List[RepoConfig], per_repo_dir: Path, llm: LLMClient,
) -> List[str]:
    """Ask the LLM to rank repos by relevance to the ticket. Cheap: only sends
    one-line descriptions + section headers + Repomix directory previews, not
    the full architecture maps or packed files."""
    summaries = []
    packed_dir = per_repo_dir.parent / "packed"
    for repo in repos:
        md_file = per_repo_dir / f"{repo.name}.md"
        head = ""
        if md_file.exists():
            try:
                lines = md_file.read_text().splitlines()
                # First 5 lines of arch section
                arch_start = next((i for i, l in enumerate(lines) if l.startswith("## ARCHITECTURE")), -1)
                if arch_start >= 0:
                    head = "\n".join(lines[arch_start + 1: arch_start + 6])
            except Exception:
                pass
        repomix_preview = _repomix_directory_preview(packed_dir / f"{repo.name}.xml")

        summaries.append(
            f"- {repo.name}: {repo.description or '(no description)'}\n"
            f"  arch preview:\n  {head[:300] if head else '(no map)'}"
            f"\n  repomix directory preview:\n  {repomix_preview[:700] if repomix_preview else '(no packed preview)'}"
        )

    user_prompt = (
        f"JIRA ticket:\n{ticket_text}\n\n"
        f"Repos:\n" + "\n".join(summaries) + "\n\n"
        f"Which repos are most relevant? (max 2, one per line, or NONE)"
    )

    result = llm.complete(
        system=_LLM_RANK_SYSTEM, user=user_prompt,
        cache_system=True, max_tokens=200,
    )
    out = result.text.strip()
    if out == "NONE":
        return []
    valid_names = {r.name for r in repos}
    return [line.strip() for line in out.splitlines() if line.strip() in valid_names]


def _repomix_directory_preview(packed_file: Path) -> str:
    if not packed_file.exists():
        return ""
    try:
        text = packed_file.read_text(errors="ignore")
    except OSError:
        return ""
    match = re.search(r"<directory_structure>\s*(.*?)\s*</directory_structure>", text, re.S)
    if not match:
        return ""
    lines = [line.rstrip() for line in match.group(1).strip().splitlines() if line.strip()]
    return "\n  ".join(lines[:40])


# ============================================================
# Public entry point
# ============================================================

def detect_relevant_repos(
    ticket_text: str,
    cfg: Config,
    llm: LLMClient,
    max_repos: int = 2,
) -> List[str]:
    """Return up to `max_repos` repo names most likely relevant to the ticket.

    Uses keyword matching as a deterministic fallback, but lets the LLM make
    the routing decision from repository summaries and Repomix previews.
    """
    keyword_matches = keyword_match_repos(ticket_text, cfg.repos, cfg.per_repo_dir)
    log.info(
        "keyword matches: %s",
        [(m.repo_name, m.score) for m in keyword_matches[:5]] or "none",
    )

    ranked = llm_rank_repos(ticket_text, cfg.repos, cfg.per_repo_dir, llm)
    if ranked:
        log.info("LLM-ranked relevant repos: %s", ranked[:max_repos])
        return ranked[:max_repos]

    if keyword_matches:
        log.info("LLM returned no valid repo — falling back to keyword matches")
        return [m.repo_name for m in keyword_matches[:max_repos]]

    return []
