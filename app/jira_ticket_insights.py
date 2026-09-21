"""Keyword insights over fetched Jira tickets stored in PostgreSQL."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from app.config import Settings


DEFAULT_EXCLUDED_PROJECT_KEYS = {"AIGOV"}


def _adf_to_text(node: Any) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(child) for child in node.get("content", [])]
        sep = "\n" if node.get("type") in ("paragraph", "heading", "listItem") else " "
        return sep.join(p for p in parts if p)
    return ""


_TEST_CASE_PATTERNS = [
    re.compile(r"\btest[\s-]+cases?\b", re.IGNORECASE),
    re.compile(r"\btestcases?\b", re.IGNORECASE),
    re.compile(r"\bTC[-_\s]?\d{1,5}\b", re.IGNORECASE),
]

_REQUIREMENT_DOC_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "PRD": [
        re.compile(r"\bPRD\b", re.IGNORECASE),
        re.compile(r"\bproduct\s+requirements?\s+document\b", re.IGNORECASE),
    ],
    "BRD": [
        re.compile(r"\bBRD\b", re.IGNORECASE),
        re.compile(r"\bbusiness\s+requirements?\s+document\b", re.IGNORECASE),
    ],
    "SRS": [
        re.compile(r"\bSRS\b", re.IGNORECASE),
        re.compile(r"\bsoftware\s+requirements?\s+(?:specification|document)\b", re.IGNORECASE),
        re.compile(r"\bsystem\s+requirements?\s+specification\b", re.IGNORECASE),
    ],
}

_STANDARD_FIELD_KEYS = {
    "summary",
    "description",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "created",
    "updated",
    "parent",
    "subtasks",
    "components",
    "labels",
    "fixVersions",
    "comment",
}


@dataclass(frozen=True)
class TextSource:
    source: str
    text: str


def scan_jira_ticket_cache(
    settings: "Settings",
    *,
    project_key: str | None = None,
    match_type: str = "all",
    limit: int = 200,
    excluded_project_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Scan cached Jira tickets for test-case and requirements-doc mentions."""
    if not settings.database_url:
        return {
            "total_tickets": 0,
            "matching_tickets": 0,
            "returned_tickets": 0,
            "counts": _empty_counts(),
            "excluded_projects": sorted(DEFAULT_EXCLUDED_PROJECT_KEYS),
            "tickets": [],
            "error": "DATABASE_URL not configured",
        }

    import psycopg
    from psycopg.rows import dict_row

    excluded = _normalized_project_keys(
        DEFAULT_EXCLUDED_PROJECT_KEYS if excluded_project_keys is None else excluded_project_keys
    )
    where, params = _project_where_clause(project_key, excluded)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"""
            SELECT ticket_key, project_key, summary, description, status,
                   issue_type, labels, updated_at, fetched_at, data
            FROM jira_ticket_cache
            {where}
            ORDER BY updated_at DESC NULLS LAST, fetched_at DESC NULLS LAST
            """,
            params,
        ).fetchall()

    counts = _empty_counts()
    tickets: list[dict[str, Any]] = []
    normalized_match_type = (match_type or "all").strip().lower()

    for row in rows:
        counts["total_tickets"] += 1
        insight = _scan_ticket(dict(row), settings.jira_base_url)
        if not _include_ticket(insight, normalized_match_type):
            continue

        counts["matching_tickets"] += 1
        if insight["has_test_cases"]:
            counts["test_cases"] += 1
        if insight["requirement_docs"]:
            counts["requirements_docs"] += 1
        for doc in insight["requirement_docs"]:
            counts[doc.lower()] += 1

        if len(tickets) < limit:
            tickets.append(insight)

    return {
        "total_tickets": counts["total_tickets"],
        "matching_tickets": counts["matching_tickets"],
        "returned_tickets": len(tickets),
        "counts": counts,
        "excluded_projects": sorted(excluded),
        "tickets": tickets,
    }


def _scan_ticket(row: dict[str, Any], jira_base_url: str) -> dict[str, Any]:
    sources = _ticket_sources(row)
    matches: list[dict[str, str]] = []
    has_test_cases = False
    requirement_docs: set[str] = set()
    seen_matches: set[tuple[str, str, str]] = set()

    for source in sources:
        text = _normalize_space(source.text)
        if not text:
            continue

        for pattern in _TEST_CASE_PATTERNS:
            for match in pattern.finditer(text):
                if _is_request_only_test_case_phrase(text, match.start()):
                    continue
                has_test_cases = True
                _append_match(
                    matches,
                    seen_matches,
                    category="test_cases",
                    label="Test Cases",
                    source=source.source,
                    text=text,
                    start=match.start(),
                    end=match.end(),
                )

        for doc, patterns in _REQUIREMENT_DOC_PATTERNS.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    requirement_docs.add(doc)
                    _append_match(
                        matches,
                        seen_matches,
                        category="requirements_doc",
                        label=doc,
                        source=source.source,
                        text=text,
                        start=match.start(),
                        end=match.end(),
                    )

    ticket_key = row.get("ticket_key") or ""
    return {
        "ticket_key": ticket_key,
        "project_key": row.get("project_key") or "",
        "summary": row.get("summary") or "",
        "status": row.get("status") or "",
        "issue_type": row.get("issue_type") or "",
        "labels": row.get("labels") or [],
        "updated_at": _fmt_dt(row.get("updated_at")),
        "fetched_at": _fmt_dt(row.get("fetched_at")),
        "url": f"{jira_base_url}/browse/{ticket_key}" if jira_base_url and ticket_key else "",
        "has_test_cases": has_test_cases,
        "requirement_docs": sorted(requirement_docs),
        "matches": matches[:12],
    }


def _append_match(
    matches: list[dict[str, str]],
    seen_matches: set[tuple[str, str, str]],
    *,
    category: str,
    label: str,
    source: str,
    text: str,
    start: int,
    end: int,
) -> None:
    snippet = _snippet(text, start, end)
    key = (category, source, snippet)
    if key in seen_matches:
        return
    seen_matches.add(key)
    matches.append(
        {
            "category": category,
            "label": label,
            "source": source,
            "snippet": snippet,
        }
    )


def _ticket_sources(row: dict[str, Any]) -> list[TextSource]:
    sources: list[TextSource] = []

    def add(source: str, text: Any) -> None:
        if text is None:
            return
        value = _normalize_space(str(text))
        if value:
            sources.append(TextSource(source=source, text=value))

    add("Summary", row.get("summary"))
    add("Description", row.get("description"))
    add("Issue type", row.get("issue_type"))

    ticket = _load_ticket_data(row.get("data"))
    fields = ticket.get("fields") if isinstance(ticket, dict) else None
    if not isinstance(fields, dict):
        return sources

    add("Description", _adf_to_text(fields.get("description")))
    add("Labels", " ".join(str(label) for label in fields.get("labels") or []))
    add("Components", " ".join(_named_values(fields.get("components"))))
    add("Fix versions", " ".join(_named_values(fields.get("fixVersions"))))

    comment_block = fields.get("comment") or {}
    comments = comment_block.get("comments") if isinstance(comment_block, dict) else []
    comment_texts = [
        _adf_to_text(comment.get("body"))
        for comment in comments or []
        if isinstance(comment, dict)
    ]
    add("Comments", "\n".join(comment_texts))

    parent = fields.get("parent") or {}
    if isinstance(parent, dict):
        add("Parent", parent.get("key"))
        parent_fields = parent.get("fields") or {}
        if isinstance(parent_fields, dict):
            add("Parent", parent_fields.get("summary"))

    subtask_texts = []
    for subtask in fields.get("subtasks") or []:
        if not isinstance(subtask, dict):
            continue
        subtask_texts.append(str(subtask.get("key") or ""))
        subtask_fields = subtask.get("fields") or {}
        if isinstance(subtask_fields, dict):
            subtask_texts.append(str(subtask_fields.get("summary") or ""))
    add("Subtasks", " ".join(subtask_texts))

    for field_key, value in fields.items():
        if field_key in _STANDARD_FIELD_KEYS:
            continue
        text = _flatten_text(value)
        if text:
            add(f"Field {field_key}", text)

    return sources


def _load_ticket_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _named_values(values: Any) -> Iterable[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, dict):
            name = value.get("name") or value.get("value")
            if name:
                out.append(str(name))
        elif value:
            out.append(str(value))
    return out


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return ""
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        adf_text = _adf_to_text(value)
        child_text = " ".join(_flatten_text(item) for item in value.values())
        return f"{adf_text} {child_text}".strip()
    return ""


def _include_ticket(ticket: dict[str, Any], match_type: str) -> bool:
    if match_type in {"all", "all_tickets", "everything"}:
        return True
    if match_type == "test_cases":
        return bool(ticket["has_test_cases"])
    if match_type == "requirements":
        return bool(ticket["requirement_docs"])
    if match_type in {"prd", "brd", "srs"}:
        return match_type.upper() in ticket["requirement_docs"]
    if match_type == "matches_only":
        return bool(ticket["has_test_cases"] or ticket["requirement_docs"])
    return True


def _normalized_project_keys(project_keys: Iterable[str]) -> set[str]:
    return {str(key).strip().upper() for key in project_keys if str(key).strip()}


def _project_where_clause(
    project_key: str | None,
    excluded_project_keys: Iterable[str] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    where_parts: list[str] = []
    params: list[Any] = []
    clean_project_key = str(project_key).strip().upper() if project_key else ""
    if clean_project_key:
        where_parts.append("UPPER(project_key) = %s")
        params.append(clean_project_key)

    excluded = sorted(_normalized_project_keys(excluded_project_keys or []))
    if excluded:
        placeholders = ", ".join(["%s"] * len(excluded))
        where_parts.append(f"UPPER(project_key) NOT IN ({placeholders})")
        params.extend(excluded)

    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    return where, tuple(params)


def _is_request_only_test_case_phrase(text: str, start: int) -> bool:
    prefix = text[max(0, start - 32):start].lower()
    return bool(
        re.search(
            r"\b(?:build|create|generate|write|prepare|provide|develop|add)\s+(?:the\s+|a\s+|new\s+)?$",
            prefix,
        )
    )


def _empty_counts() -> dict[str, int]:
    return {
        "total_tickets": 0,
        "matching_tickets": 0,
        "test_cases": 0,
        "requirements_docs": 0,
        "prd": 0,
        "brd": 0,
        "srs": 0,
    }


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _snippet(text: str, start: int, end: int, radius: int = 90) -> str:
    prefix_start = max(0, start - radius)
    suffix_end = min(len(text), end + radius)
    snippet = text[prefix_start:suffix_end].strip()
    if prefix_start > 0:
        snippet = f"...{snippet}"
    if suffix_end < len(text):
        snippet = f"{snippet}..."
    return snippet


def _fmt_dt(val: Any) -> str | None:
    if val is None:
        return None
    try:
        return val.isoformat()
    except AttributeError:
        return str(val)
