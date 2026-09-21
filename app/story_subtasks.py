"""Map AIGOV Story → child issues and persist each child's assignee plus
dev/QA due dates, for use when building the workflow4 AIGOV digest.

A Story's ``subtasks`` array (returned by the project fetch) only carries
stubs — key, summary, status — so it has neither the assignee nor the per-phase
custom due-date fields. This module therefore runs a single JQL
``parent in (<story keys>)`` search requesting those fields explicitly, then
upserts one row per child into the ``story_subtasks`` table.

"Child" follows the parent link (the decision: *all child issues under a
Story*), so it includes both native sub-tasks and child Tasks/Bugs.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from app.config import Settings

LOGGER = logging.getLogger(__name__)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _ensure_table(cursor: Any) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS story_subtasks (
            subtask_key         varchar(50) PRIMARY KEY,
            story_key           varchar(50) NOT NULL,
            project_key         varchar(20),
            issue_type          varchar(50),
            summary             text,
            status              varchar(100),
            assignee_name       varchar(255),
            assignee_email      varchar(255),
            assignee_account_id varchar(128),
            dev_due_date        date,
            qa_due_date         date,
            system_due_date     date,
            updated_at          timestamptz DEFAULT NOW()
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_story_subtasks_story ON story_subtasks (story_key)"
    )


def _split_keys(raw: str) -> list[str]:
    import re

    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\s,]+", (raw or "").strip()):
        key = part.strip()
        if key and key.upper() not in seen:
            seen.add(key.upper())
            out.append(key)
    return out


def _excluded_clause(settings: Settings) -> str:
    """JQL fragment excluding JIRA_EXCLUDED_PROJECT_KEYS (e.g. the AIGOV sandbox)."""
    excluded = _split_keys(settings.jira_excluded_project_keys)
    if not excluded:
        return ""
    quoted = ",".join(f'"{k}"' for k in excluded)
    return f" AND project not in ({quoted})"


def _fetch_story_keys(
    settings: Settings, project_keys: Optional[list[str]]
) -> list[str]:
    """Find Story-type issue keys via JQL.

    With ``project_keys`` → restrict to those projects. Without → all visible
    projects minus ``JIRA_EXCLUDED_PROJECT_KEYS`` (the 'all spaces' production
    scope)."""
    from app.jira_fetcher import _jira_get

    if project_keys:
        quoted = ",".join(f'"{k}"' for k in project_keys)
        jql = f"project in ({quoted}) AND issuetype = Story ORDER BY created DESC"
    else:
        jql = f"issuetype = Story{_excluded_clause(settings)} ORDER BY created DESC"

    keys: list[str] = []
    start = 0
    next_token: Optional[str] = None

    while True:
        params: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": "key"}
        if next_token:
            params["nextPageToken"] = next_token
        else:
            params["startAt"] = start

        data = _jira_get("/rest/api/3/search/jql", params)
        batch = data.get("issues", [])
        keys.extend(str(i.get("key")) for i in batch if i.get("key"))
        start += len(batch)
        next_token = data.get("nextPageToken")

        if data.get("isLast") is True:
            break
        if next_token:
            continue
        total = data.get("total")
        if total is not None and start >= total:
            break
        if not batch:
            break

    return keys


def _search_children(settings: Settings, story_keys: list[str]) -> list[dict[str, Any]]:
    """Paginate child issues whose parent is one of ``story_keys``."""
    from app.jira_fetcher import _jira_get

    fields = ["parent", "assignee", "status", "issuetype", "summary", "duedate"]
    for fid in (settings.jira_dev_due_date_field, settings.jira_qa_due_date_field):
        if fid:
            fields.append(fid)

    jql = f"parent in ({','.join(story_keys)}) ORDER BY parent ASC"
    issues: list[dict[str, Any]] = []
    start = 0
    next_token: Optional[str] = None

    while True:
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": 100,
            "fields": ",".join(fields),
        }
        if next_token:
            params["nextPageToken"] = next_token
        else:
            params["startAt"] = start

        data = _jira_get("/rest/api/3/search/jql", params)
        batch = data.get("issues", [])
        issues.extend(batch)
        start += len(batch)
        next_token = data.get("nextPageToken")

        if data.get("isLast") is True:
            break
        if next_token:
            continue
        total = data.get("total")
        if total is not None and start >= total:
            break
        if not batch:
            break

    return issues


def _child_row(settings: Settings, child: dict[str, Any]) -> Optional[dict[str, Any]]:
    fields = child.get("fields", {}) or {}
    parent = fields.get("parent") or {}
    story_key = str(parent.get("key") or "")
    subtask_key = str(child.get("key") or "")
    if not story_key or not subtask_key:
        return None

    assignee = fields.get("assignee") or {}
    project_key = subtask_key.rsplit("-", 1)[0] if "-" in subtask_key else subtask_key

    return {
        "subtask_key": subtask_key,
        "story_key": story_key,
        "project_key": project_key,
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "summary": (fields.get("summary") or "")[:1000],
        "status": (fields.get("status") or {}).get("name"),
        "assignee_name": assignee.get("displayName"),
        "assignee_email": assignee.get("emailAddress"),
        "assignee_account_id": assignee.get("accountId"),
        "dev_due_date": _parse_date(fields.get(settings.jira_dev_due_date_field))
        if settings.jira_dev_due_date_field else None,
        "qa_due_date": _parse_date(fields.get(settings.jira_qa_due_date_field))
        if settings.jira_qa_due_date_field else None,
        "system_due_date": _parse_date(fields.get("duedate")),
    }


def map_and_store(
    settings: Settings, project_keys: Optional[list[str]] = None
) -> int:
    """Map Stories to their children and upsert each into ``story_subtasks``.

    Scope precedence: explicit ``project_keys`` (passed by the WF4 checker, e.g.
    its WORKFLOW4_PROJECT_KEYS) → ``STORY_SUBTASK_PROJECT_KEYS`` → all visible
    spaces minus ``JIRA_EXCLUDED_PROJECT_KEYS``. Story discovery is via JQL.
    Returns the number of child rows written."""
    scope = project_keys or _split_keys(settings.story_subtask_project_keys)
    story_keys = _fetch_story_keys(settings, scope or None)
    LOGGER.info(
        "story_subtasks: %d Story(ies) in scope %s",
        len(story_keys),
        scope or "all-spaces",
    )
    if not story_keys:
        LOGGER.info("story_subtasks: no Story issues to map")
        return 0

    children = _search_children(settings, story_keys)
    LOGGER.info(
        "story_subtasks: %d Story(ies) → %d child issue(s)",
        len(story_keys),
        len(children),
    )
    if not children:
        return 0

    import psycopg2

    written = 0
    with psycopg2.connect(settings.database_url) as conn:
        with conn.cursor() as cursor:
            _ensure_table(cursor)
            for child in children:
                row = _child_row(settings, child)
                if row is None:
                    continue
                cursor.execute(
                    """
                    INSERT INTO story_subtasks (
                        subtask_key, story_key, project_key, issue_type, summary,
                        status, assignee_name, assignee_email, assignee_account_id,
                        dev_due_date, qa_due_date, system_due_date, updated_at
                    ) VALUES (
                        %(subtask_key)s, %(story_key)s, %(project_key)s, %(issue_type)s,
                        %(summary)s, %(status)s, %(assignee_name)s, %(assignee_email)s,
                        %(assignee_account_id)s, %(dev_due_date)s, %(qa_due_date)s,
                        %(system_due_date)s, NOW()
                    )
                    ON CONFLICT (subtask_key) DO UPDATE SET
                        story_key           = EXCLUDED.story_key,
                        project_key         = EXCLUDED.project_key,
                        issue_type          = EXCLUDED.issue_type,
                        summary             = EXCLUDED.summary,
                        status              = EXCLUDED.status,
                        assignee_name       = EXCLUDED.assignee_name,
                        assignee_email      = EXCLUDED.assignee_email,
                        assignee_account_id = EXCLUDED.assignee_account_id,
                        dev_due_date        = EXCLUDED.dev_due_date,
                        qa_due_date         = EXCLUDED.qa_due_date,
                        system_due_date     = EXCLUDED.system_due_date,
                        updated_at          = NOW()
                    """,
                    row,
                )
                written += 1
            conn.commit()

    LOGGER.info("story_subtasks: upserted %d child row(s)", written)
    return written


def get_subtasks_for_stories(
    settings: Settings, story_keys: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Return stored subtasks grouped by story_key for the given Story keys."""
    if not story_keys:
        return {}

    import psycopg2
    from psycopg2.extras import RealDictCursor

    grouped: dict[str, list[dict[str, Any]]] = {}
    try:
        with psycopg2.connect(settings.database_url) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                _ensure_table(cursor)
                cursor.execute(
                    """
                    SELECT subtask_key, story_key, issue_type, status,
                           assignee_name, dev_due_date, qa_due_date, system_due_date
                    FROM story_subtasks
                    WHERE story_key = ANY(%s)
                    ORDER BY story_key, subtask_key
                    """,
                    (story_keys,),
                )
                for row in cursor.fetchall():
                    grouped.setdefault(row["story_key"], []).append(dict(row))
    except Exception:
        LOGGER.exception("story_subtasks: failed to read subtasks (non-fatal)")
    return grouped


def get_all_subtasks(
    settings: Settings, project_keys: Optional[list[str]] = None
) -> dict[str, list[dict[str, Any]]]:
    """Return every stored subtask grouped by story_key, optionally scoped to
    ``project_keys`` (matched against the stored ``project_key`` column)."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    grouped: dict[str, list[dict[str, Any]]] = {}
    try:
        with psycopg2.connect(settings.database_url) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                _ensure_table(cursor)
                if project_keys:
                    cursor.execute(
                        """
                        SELECT story_key, subtask_key, issue_type, status,
                               assignee_name, dev_due_date, qa_due_date, system_due_date
                        FROM story_subtasks
                        WHERE project_key = ANY(%s)
                        ORDER BY story_key, subtask_key
                        """,
                        (list(project_keys),),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT story_key, subtask_key, issue_type, status,
                               assignee_name, dev_due_date, qa_due_date, system_due_date
                        FROM story_subtasks
                        ORDER BY story_key, subtask_key
                        """
                    )
                for row in cursor.fetchall():
                    grouped.setdefault(row["story_key"], []).append(dict(row))
    except Exception:
        LOGGER.exception("story_subtasks: get_all_subtasks failed (non-fatal)")
    return grouped


def format_breakdown(grouped: dict[str, list[dict[str, Any]]]) -> str:
    """Render the per-Story subtask breakdown as plain text for the LLM prompt."""
    if not grouped:
        return ""
    lines = ["", "Subtask breakdown by Story:"]
    for story_key in sorted(grouped):
        rows = grouped[story_key]
        lines.append(f"{story_key} — {len(rows)} child issue(s):")
        for r in rows:
            who = r.get("assignee_name") or "Unassigned"
            dev = r.get("dev_due_date") or "—"
            qa = r.get("qa_due_date") or "—"
            status = r.get("status") or "—"
            itype = r.get("issue_type") or "—"
            lines.append(
                f"   • {r['subtask_key']} [{itype}/{status}] — {who} · "
                f"dev {dev} · qa {qa}"
            )
    return "\n".join(lines)
