"""Run the AI Governor on tickets that were created/edited directly in Jira.

The n8n Jira webhook is the primary trigger for ``/workflow1``. Tickets created
in Jira while n8n is unreachable were only ever cached, never validated, so they
stayed in ``To Do`` forever. This runs the same reviewer over cached tickets that
are still awaiting approval and have not been reviewed with their current content.
"""

from __future__ import annotations

import logging
import os

import psycopg
from psycopg.rows import dict_row

from app.config import Settings
from app.prompt_store import PromptStore
from app.schemas import Workflow1ReviewRequest
from app.workflow1_reviewer import JIRA_PRIORITY_TO_P, Workflow1Reviewer

log = logging.getLogger(__name__)

# Jira custom fields holding the repository link ("github link", "Repository URL").
REPO_FIELD_IDS = tuple(
    f.strip() for f in os.getenv("JIRA_REPO_FIELD_IDS", "customfield_10177,customfield_10109").split(",") if f.strip()
)
AWAITING_APPROVAL_STATUSES = ("to do", "created", "open", "backlog")
# Only auto-review tickets created recently, so enabling this never backfills old backlog tickets.
MAX_AGE_HOURS = float(os.getenv("GOVERNOR_AUTO_MAX_AGE_HOURS", "24"))
_FIELDS = ("summary", "description", "assignee", "priority", "github_repo_url")


def _pending(settings: Settings, limit: int) -> list[dict]:
    """Cached tickets awaiting approval whose current content has not been reviewed yet."""
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT c.ticket_key, c.project_key, c.summary, c.description, c.status, c.issue_type,
                   c.priority, c.data->'fields' AS raw_fields, c.assignee_name, c.assignee_email, c.reporter_name, c.created_at,
                   t.jira_payload AS reviewed
            FROM jira_ticket_cache c
            LEFT JOIN tickets t ON t.jira_ticket_id = c.ticket_key
            WHERE LOWER(TRIM(c.status)) = ANY(%s)
              AND COALESCE(c.created_at, (c.data->'fields'->>'created')::timestamptz) >= NOW() - %s * INTERVAL '1 hour'
            ORDER BY COALESCE(c.created_at, (c.data->'fields'->>'created')::timestamptz) DESC NULLS LAST
            """,
            (list(AWAITING_APPROVAL_STATUSES), MAX_AGE_HOURS),
        ).fetchall()

    pending = []
    for r in rows:
        reviewed = r.pop("reviewed", None)
        raw = r.pop("raw_fields", None) or {}
        r["repo"] = next((str(raw[f]).strip() for f in REPO_FIELD_IDS if isinstance(raw.get(f), str) and raw[f].strip()), "")
        if reviewed:
            current = {
                "summary": r["summary"] or "",
                "description": r["description"] or "",
                "assignee": r["assignee_name"] or "",
                "priority": r["priority"] or "",
                "github_repo_url": r["repo"],
            }
            # The stored payload holds the normalised P0-P4 priority; normalise the cached name the same way.
            current["priority"] = JIRA_PRIORITY_TO_P.get(current["priority"].upper(), current["priority"])
            if all(str(reviewed.get(k) or "") == current[k] for k in _FIELDS):
                continue  # already reviewed with this exact content (avoids re-commenting on every sync)
        pending.append(r)
    return pending[:limit]


def run_pending_reviews(settings: Settings, prompt_store: PromptStore, limit: int = 5) -> int:
    """Review up to ``limit`` pending tickets; returns how many were reviewed."""
    if not settings.database_url:
        return 0
    reviewer = Workflow1Reviewer(settings=settings, prompt_store=prompt_store)
    done = 0
    for r in _pending(settings, limit):
        request = Workflow1ReviewRequest(
            issueKey=r["ticket_key"],
            summary=r["summary"] or "",
            description=r["description"] or "",
            assignee=r["assignee_name"] or "",
            email=r["assignee_email"] or "",
            priority=r["priority"] or "",
            issueType=r["issue_type"] or "",
            status=r["status"] or "",
            reporter=r["reporter_name"] or "",
            github_repo_url=r["repo"],
            createdAt=r["created_at"].isoformat() if r["created_at"] else "",
        )
        try:
            result = reviewer.review(request)
            log.info("Auto governor review %s -> %s", r["ticket_key"], result.get("nature"))
            done += 1
        except Exception:
            log.exception("Auto governor review failed for %s", r["ticket_key"])
    return done
