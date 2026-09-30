"""User-level context memory for the AI Governor workflows.

The problem this solves: every Workflow1 validation call previously started from
zero. The LLM had no idea the same person had raised three tickets against the
same repository that morning, so its judgement drifted between otherwise
identical requests.

Storage lives in the `user_memory` table, keyed by (user_identifier, context_type):

  recent_tickets  - a capped, newest-first list of the tickets this person raised
  summary         - a compacted rolling narrative of what they have been working on

A person reaches the system under several identities: an app login email from the
UI, a Jira display name from a webhook, a Slack user id from an event. All of them
are resolved to the same canonical identifier before reading or writing, so memory
written by one path is visible to the others.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)

# How many tickets to keep verbatim before older ones survive only via the summary.
MAX_RECENT_TICKETS = 10

# Characters of rolling summary to carry into a prompt.
MAX_SUMMARY_CHARS = 1500

# How stale a rolling summary may get before it is recomputed. Regenerating it costs an
# LLM call on the ticket-creation request path, so it is deliberately infrequent.
SUMMARY_REFRESH_HOURS = 24


def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.database_url, row_factory=dict_row)


def normalize_identifier(value: str | None) -> str:
    return (value or "").strip().lower()


def resolve_identities(settings: Settings, *candidates: str | None) -> list[str]:
    """Expand any known identity of a person into every identity we hold for them.

    Accepts an email, a Slack user id, a Slack user name or a Jira display name and
    returns the full set, so a lookup keyed by one identity finds memory written
    under another. The input values are always included, even when the lookup table
    has no row for them.
    """
    seen: list[str] = []
    for c in candidates:
        norm = normalize_identifier(c)
        if norm and norm not in seen:
            seen.append(norm)

    if not seen or not settings.database_url:
        return seen

    try:
        with _connect(settings) as conn:
            rows = conn.execute(
                """
                SELECT email_id, slack_user_id, slack_user_name, display_name
                FROM channelid_table
                WHERE LOWER(email_id) = ANY(%s)
                   OR LOWER(slack_user_id) = ANY(%s)
                   OR LOWER(slack_user_name) = ANY(%s)
                   OR LOWER(display_name) = ANY(%s)
                """,
                (seen, seen, seen, seen),
            ).fetchall()
            for row in rows or []:
                for field in ("email_id", "slack_user_id", "slack_user_name", "display_name"):
                    norm = normalize_identifier(row.get(field))
                    if norm and norm not in seen:
                        seen.append(norm)
    except Exception as exc:
        log.warning("Could not expand user identities %s: %s", seen, exc)

    return seen


def record_ticket(
    settings: Settings,
    *,
    identifiers: list[str] | str | None,
    ticket_key: str,
    summary: str,
    repo: str,
    review: str = "",
    nature: str = "",
) -> None:
    """Append a ticket to this person's recent history.

    Newest first, de-duplicated by ticket key so a re-reviewed ticket updates in
    place rather than appearing twice, and capped at MAX_RECENT_TICKETS.
    """
    if not settings.database_url or not ticket_key:
        return

    ids = [identifiers] if isinstance(identifiers, str) else (identifiers or [])
    primary = next((normalize_identifier(i) for i in ids if normalize_identifier(i)), "")
    if not primary:
        return

    entry = {
        "ticket_key": ticket_key,
        "summary": (summary or "")[:400],
        "repo": repo or "",
        "nature": nature,
        "review_excerpt": (review or "")[:600],
    }

    try:
        with _connect(settings) as conn:
            row = conn.execute(
                "SELECT data FROM user_memory WHERE user_identifier = %s AND context_type = 'recent_tickets'",
                (primary,),
            ).fetchone()

            existing = []
            if row and isinstance(row.get("data"), dict):
                existing = row["data"].get("tickets") or []

            tickets = [entry] + [t for t in existing if t.get("ticket_key") != ticket_key]
            tickets = tickets[:MAX_RECENT_TICKETS]

            conn.execute(
                """
                INSERT INTO user_memory (user_identifier, context_type, data, updated_at)
                VALUES (%s, 'recent_tickets', %s, NOW())
                ON CONFLICT (user_identifier, context_type) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = NOW()
                """,
                (primary, json.dumps({"tickets": tickets})),
            )

            # Keep the legacy single-ticket row in step: workflow2 thread discovery
            # still falls back to it when no thread mapping exists.
            conn.execute(
                """
                INSERT INTO user_memory (user_identifier, context_type, data, updated_at)
                VALUES (%s, 'recent_ticket', %s, NOW())
                ON CONFLICT (user_identifier, context_type) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = NOW()
                """,
                (
                    primary,
                    json.dumps(
                        {
                            "last_ticket_key": ticket_key,
                            "last_repo": repo or "",
                            "last_summary": (summary or "")[:400],
                        }
                    ),
                ),
            )
            conn.commit()
    except Exception as exc:
        log.warning("Could not record ticket %s into user memory: %s", ticket_key, exc)


def load_memory(settings: Settings, identifiers: list[str] | str | None) -> dict[str, Any]:
    """Return {'tickets': [...], 'summary': str} merged across all of a person's identities."""
    empty: dict[str, Any] = {"tickets": [], "summary": ""}
    if not settings.database_url:
        return empty

    ids = [identifiers] if isinstance(identifiers, str) else (identifiers or [])
    ids = [normalize_identifier(i) for i in ids if normalize_identifier(i)]
    if not ids:
        return empty

    try:
        with _connect(settings) as conn:
            rows = conn.execute(
                """
                SELECT context_type, data, updated_at
                FROM user_memory
                WHERE LOWER(user_identifier) = ANY(%s)
                  AND context_type IN ('recent_tickets', 'summary')
                ORDER BY updated_at DESC
                """,
                (ids,),
            ).fetchall()
    except Exception as exc:
        log.warning("Could not load user memory for %s: %s", ids, exc)
        return empty

    tickets: list[dict[str, Any]] = []
    summary = ""
    seen_keys: set[str] = set()
    for row in rows or []:
        data = row.get("data") or {}
        if row.get("context_type") == "recent_tickets":
            for t in data.get("tickets") or []:
                key = t.get("ticket_key")
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    tickets.append(t)
        elif row.get("context_type") == "summary" and not summary:
            summary = str(data.get("text") or "")

    return {"tickets": tickets[:MAX_RECENT_TICKETS], "summary": summary[:MAX_SUMMARY_CHARS]}


def render_for_prompt(memory: dict[str, Any]) -> str:
    """Format memory as a prompt block.

    Explicitly frames the history as background, because an earlier rejection in
    the list must not bias the model into rejecting the ticket under review.
    """
    tickets = memory.get("tickets") or []
    summary = memory.get("summary") or ""

    if not tickets and not summary:
        return (
            "No previous ticket history is on record for this user. "
            "Judge this ticket entirely on its own merits."
        )

    lines = ["Prior context for this user (background only - judge the current ticket on its own merits):"]
    if summary:
        lines.append(f"Working context: {summary}")
    if tickets:
        lines.append("Recently raised tickets by this user:")
        for t in tickets:
            verdict = f" [previously {t['nature']}]" if t.get("nature") else ""
            repo = f" | repo: {t['repo']}" if t.get("repo") else ""
            lines.append(f"- {t.get('ticket_key')}: {t.get('summary', '')}{repo}{verdict}")
    return "\n".join(lines)


def update_summary(settings: Settings, *, identifier: str, memory: dict[str, Any]) -> None:
    """Compact recent ticket history into a short rolling narrative.

    Called after a ticket is recorded. Best-effort: a failure here costs nothing
    because the verbatim ticket list is still carried into prompts.
    """
    primary = normalize_identifier(identifier)
    tickets = memory.get("tickets") or []
    if not settings.database_url or not primary or len(tickets) < 3:
        return

    # Rate limit: this is an extra LLM call on the ticket-creation request path, and a
    # rolling summary does not need to be recomputed for every single ticket.
    try:
        with _connect(settings) as conn:
            fresh = conn.execute(
                """
                SELECT 1 FROM user_memory
                WHERE user_identifier = %s
                  AND context_type = 'summary'
                  AND updated_at > NOW() - make_interval(hours => %s)
                """,
                (primary, SUMMARY_REFRESH_HOURS),
            ).fetchone()
        if fresh:
            return
    except Exception as exc:
        log.warning("Could not check summary freshness for %s: %s", primary, exc)
        return

    try:
        from app.llm_client import build_llm_client

        client = build_llm_client(settings)
        listing = "\n".join(
            f"- {t.get('ticket_key')}: {t.get('summary', '')} (repo: {t.get('repo') or 'none'})"
            for t in tickets
        )
        text = client.complete(
            system_prompt=(
                "Summarise what this engineer has been working on, in at most four sentences. "
                "State the repositories and recurring themes. Plain factual prose, no emojis, "
                "no headings, no speculation about their skill or intent."
            ),
            user_message=f"Tickets raised by this user, newest first:\n{listing}",
            max_tokens=300,
        ).strip()

        if not text:
            return

        with _connect(settings) as conn:
            conn.execute(
                """
                INSERT INTO user_memory (user_identifier, context_type, data, updated_at)
                VALUES (%s, 'summary', %s, NOW())
                ON CONFLICT (user_identifier, context_type) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = NOW()
                """,
                (primary, json.dumps({"text": text[:MAX_SUMMARY_CHARS]})),
            )
            conn.commit()
    except Exception as exc:
        log.warning("Could not update rolling summary for %s: %s", primary, exc)
