"""Assemble the full context for test case generation.

The old generator passed the LLM a ticket summary and description and nothing else,
which is why its output read as a restatement of the ticket. Every section here is
built at request time from live sources, so the prompt describes the system as it
actually is rather than repeating what the ticket claims.

Sections, each degrading to an explicit "not available" note rather than vanishing
silently - a missing section the model cannot see is a section it will invent around:

    repository        which repo, and how it was resolved
    codebase          real files and snippets from the checkout
    dependency map    call graph around the functions the ticket touches
    related tickets   prior work on the same repo, and its test coverage
    user context      what this person has been working on

This is used by the application and Jira paths only. The Slack test case chat path
deliberately does not receive repository context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)

MAX_RELATED_TICKETS = 5


@dataclass
class TestCaseContext:
    repository: str = ""
    codebase: str = ""
    dependency_map: str = ""
    related_tickets: str = ""
    user_context: str = ""
    sources: list[str] = field(default_factory=list)

    def as_prompt_fields(self) -> dict[str, str]:
        return {
            "repository_context": self.repository or "No repository is linked to this ticket.",
            "codebase_context": self.codebase
            or "The repository is not checked out, so no source code could be read. "
            "Do not invent file or function names - say which areas you could not verify.",
            "dependency_context": self.dependency_map
            or "No call graph is available for this ticket. Derive dependencies from the "
            "source files above where possible, and say so where you cannot.",
            "related_tickets_context": self.related_tickets
            or "No prior tickets were found for this repository.",
            "user_context": self.user_context or "No prior context for this user.",
        }


def build(
    settings: Settings,
    *,
    ticket: dict[str, Any],
    repo: str | None,
    identifiers: list[str] | None = None,
) -> TestCaseContext:
    """Gather every available context source. Never raises."""
    ctx = TestCaseContext()
    summary = str(ticket.get("summary") or ticket.get("title") or "")
    description = str(ticket.get("description") or ticket.get("description_text") or "")
    ticket_key = str(ticket.get("issueKey") or ticket.get("key") or ticket.get("issue_key") or "")

    if repo:
        ctx.repository = f"Target repository: {repo}"
        ctx.sources.append("repository")

    # 1. Source files from the checkout.
    try:
        from app.prompt_store import PromptStore
        from app.workflow1_reviewer import Workflow1Reviewer

        reviewer = Workflow1Reviewer(settings=settings, prompt_store=PromptStore(settings.prompt_dir))
        if repo:
            text = reviewer._get_codebase_context(repo, summary, description)
            # The helper returns a prose apology rather than raising when the repo is
            # absent; treat that as no context so the prompt says so explicitly.
            if text and "not yet cloned" not in text and "unavailable" not in text:
                ctx.codebase = text
                ctx.sources.append("codebase")
    except Exception as exc:
        log.warning("Test case context: codebase unavailable: %s", exc)

    # 2. Call graph.
    try:
        from app.neo4j_graph.ticket_context import open_reader

        reader = open_reader()
        if reader is not None:
            try:
                dep = reader.dependency_text_for(
                    {"summary": summary, "description": description, "key": ticket_key}
                )
                if dep:
                    ctx.dependency_map = dep
                    ctx.sources.append("call-graph")
            finally:
                reader.close()
    except Exception as exc:
        log.warning("Test case context: call graph unavailable: %s", exc)

    # 3. Prior tickets on the same repo, with whether they already have test cases.
    try:
        related = _related_tickets(settings, repo=repo, exclude_key=ticket_key)
        if related:
            ctx.related_tickets = related
            ctx.sources.append("related-tickets")
    except Exception as exc:
        log.warning("Test case context: related tickets unavailable: %s", exc)

    # 4. User memory.
    try:
        if identifiers:
            from app import user_memory as memory

            mem = memory.load_memory(settings, identifiers)
            if mem.get("tickets") or mem.get("summary"):
                ctx.user_context = memory.render_for_prompt(mem)
                ctx.sources.append("user-memory")
    except Exception as exc:
        log.warning("Test case context: user memory unavailable: %s", exc)

    log.info(
        "Test case context for %s assembled from: %s",
        ticket_key or "(unkeyed)",
        ", ".join(ctx.sources) or "nothing",
    )
    return ctx


def _related_tickets(settings: Settings, *, repo: str | None, exclude_key: str) -> str:
    """Prior tickets for this repository, flagged with existing test case counts.

    Knowing a neighbouring ticket already has test cases lets the model avoid
    regenerating the same coverage and point at regression risk instead.
    """
    if not settings.database_url or not repo:
        return ""

    from app.duplicate_detector import DuplicateDetector

    candidates = DuplicateDetector(settings)._candidates_from_postgres(
        __import__("app.duplicate_detector", fromlist=["normalize_repo"]).normalize_repo(repo)
    )
    candidates = [c for c in candidates if c.key.upper() != (exclude_key or "").upper()]
    if not candidates:
        return ""

    counts: dict[str, int] = {}
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT UPPER(jira_ticket_id) AS key, COUNT(*) AS n
                FROM test_cases
                WHERE UPPER(jira_ticket_id) = ANY(%s)
                GROUP BY UPPER(jira_ticket_id)
                """,
                ([c.key.upper() for c in candidates],),
            ).fetchall()
        counts = {str(r["key"]): int(r["n"]) for r in rows or []}
    except Exception as exc:
        log.debug("Could not count existing test cases: %s", exc)

    lines = ["Prior tickets on this repository (use these to avoid duplicating coverage and to identify regression risk):"]
    for c in candidates[:MAX_RELATED_TICKETS]:
        n = counts.get(c.key.upper(), 0)
        coverage = f"{n} existing test case(s)" if n else "no test cases yet"
        lines.append(f"  - {c.key} [{c.status or 'unknown'}] {c.summary} ({coverage})")
    return "\n".join(lines)
