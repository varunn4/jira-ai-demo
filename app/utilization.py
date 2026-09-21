"""Utilization analytics for the admin dashboard.

Two responsibilities:

1. ``ticket_status_history`` — a lightweight, self-healing transition log. The
   Jira webhook (via the n8n "Status Transition Logger" workflow) posts each
   status change to ``POST /workflow/record-transition`` which calls
   ``record_status_change``. Rows accrue going forward; the table starts empty.

2. ``build_utilization_report`` — a read-only aggregation across every table the
   AI Governor system writes (test cases, doc reviews, ticket cache, due-date
   tracking, SLA, documentation generation, Slack conversations, transitions),
   returned as a plain dict for the ``/graph-admin/utilization`` endpoint.

Every section is computed defensively: a missing table or column degrades that
one section to empty rather than failing the whole report, so the dashboard
works across environments at different migration states.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import Settings

LOGGER = logging.getLogger(__name__)


# ── connection + small query helpers ─────────────────────────────────────────
def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.database_url, row_factory=dict_row)


def _table_exists(conn, name: str) -> bool:
    try:
        row = conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()
        return bool(row and row.get("t"))
    except Exception:  # noqa: BLE001
        return False


def _rows(conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Run a query, returning [] on any error (e.g. missing column)."""
    try:
        return conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("utilization query skipped: %s", exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def _one(conn, sql: str, params: tuple = ()) -> dict[str, Any]:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ── transition log (write path) ──────────────────────────────────────────────
def ensure_status_history_schema(settings: Settings) -> None:
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_status_history (
                id             BIGSERIAL PRIMARY KEY,
                jira_ticket_id TEXT NOT NULL,
                project_key    TEXT,
                issue_type     TEXT,
                from_status    TEXT,
                to_status      TEXT NOT NULL,
                assignee_name  TEXT,
                source         TEXT DEFAULT 'webhook',
                changed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_tsh_ticket ON ticket_status_history (jira_ticket_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_tsh_changed ON ticket_status_history (changed_at)"
        )
        # Dedupe guard: the same webhook can be delivered more than once.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_tsh_event "
            "ON ticket_status_history (jira_ticket_id, to_status, changed_at)"
        )
        conn.commit()


def record_status_change(
    settings: Settings,
    *,
    issue_key: str,
    to_status: str,
    from_status: str = "",
    project_key: str = "",
    issue_type: str = "",
    assignee: str = "",
    changed_at: str = "",
    source: str = "webhook",
) -> dict[str, Any]:
    """Persist one status transition. Idempotent on (ticket, to_status, changed_at)."""
    if not settings.database_url:
        return {"recorded": False, "reason": "DATABASE_URL not configured"}
    if not issue_key or not to_status:
        return {"recorded": False, "reason": "issueKey and toStatus are required"}

    ensure_status_history_schema(settings)

    ts: Optional[datetime] = None
    if changed_at:
        try:
            ts = datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
        except ValueError:
            ts = None
    if ts is None:
        ts = datetime.now(timezone.utc)

    with _connect(settings) as conn:
        cur = conn.execute(
            """
            INSERT INTO ticket_status_history
                (jira_ticket_id, project_key, issue_type, from_status,
                 to_status, assignee_name, source, changed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (jira_ticket_id, to_status, changed_at) DO NOTHING
            RETURNING id
            """,
            (
                issue_key.strip(),
                (project_key or "").strip() or None,
                (issue_type or "").strip() or None,
                (from_status or "").strip() or None,
                to_status.strip(),
                (assignee or "").strip() or None,
                source,
                ts,
            ),
        )
        row = cur.fetchone()
        conn.commit()

    recorded = bool(row)
    LOGGER.info(
        "record_status_change issue=%s %s -> %s recorded=%s",
        issue_key,
        from_status or "?",
        to_status,
        recorded,
    )
    return {"recorded": recorded, "ticket": issue_key, "to_status": to_status}


# ── utilization report (read path) ───────────────────────────────────────────
def build_utilization_report(settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "generated_at": now,
        "database_configured": bool(settings.database_url),
    }
    if not settings.database_url:
        return report

    with _connect(settings) as conn:
        report["test_cases"] = _test_cases_section(conn)
        report["doc_reviews"] = _doc_reviews_section(conn)
        report["tickets"] = _tickets_section(conn)
        report["transitions"] = _transitions_section(conn)
        report["due_date"] = _due_date_section(conn)
        report["sla"] = _sla_section(conn)
        report["documentation"] = _documentation_section(conn)
        report["conversations"] = _conversations_section(conn)

    return report


def _test_cases_section(conn) -> dict[str, Any]:
    # NOTE: test cases are AI-*generated* artifacts. There is no pass/fail
    # execution tracking — `test_cases.status` is always inserted as 'pending'
    # and never updated. The "tests passed" signal is the developer moving the
    # ticket out of QA (QA -> Ready for Deployment), which lives in the
    # transitions section, not here. So we report generation volume only.
    if not _table_exists(conn, "test_cases"):
        return {"available": False}
    totals = _one(
        conn,
        "SELECT COUNT(*) AS total, COUNT(DISTINCT jira_ticket_id) AS tickets FROM test_cases",
    )
    total = _int(totals.get("total"))
    tickets = _int(totals.get("tickets"))
    return {
        "available": True,
        "total_test_cases": total,
        "tickets_covered": tickets,
        "avg_per_ticket": round(total / tickets, 1) if tickets else 0,
    }


def _doc_reviews_section(conn) -> dict[str, Any]:
    if not _table_exists(conn, "doc_reviews"):
        return {"available": False}
    totals = _one(
        conn,
        "SELECT COUNT(*) AS docs, COUNT(DISTINCT jira_ticket_id) AS tickets, "
        "COALESCE(SUM(review_count),0) AS reviews FROM doc_reviews",
    )
    by_type = _rows(
        conn,
        "SELECT COALESCE(NULLIF(doc_type,''),'(other)') AS doc_type, COUNT(*) AS docs, "
        "COALESCE(SUM(review_count),0) AS reviews FROM doc_reviews GROUP BY 1 ORDER BY docs DESC",
    )
    return {
        "available": True,
        "docs_reviewed": _int(totals.get("docs")),
        "tickets_covered": _int(totals.get("tickets")),
        "total_reviews": _int(totals.get("reviews")),
        "by_type": [
            {"doc_type": r["doc_type"], "docs": _int(r["docs"]), "reviews": _int(r["reviews"])}
            for r in by_type
        ],
    }


def _tickets_section(conn) -> dict[str, Any]:
    if not _table_exists(conn, "jira_ticket_cache"):
        return {"available": False}
    total = _int(_one(conn, "SELECT COUNT(*) AS c FROM jira_ticket_cache").get("c"))

    def _group(col: str, label: str) -> list[dict[str, Any]]:
        rows = _rows(
            conn,
            f"SELECT COALESCE(NULLIF({col},''),'(none)') AS k, COUNT(*) AS c "
            f"FROM jira_ticket_cache GROUP BY 1 ORDER BY c DESC",
        )
        return [{label: r["k"], "count": _int(r["c"])} for r in rows]

    return {
        "available": True,
        "total": total,
        "by_status": _group("status", "status"),
        "by_project": _group("project_key", "project"),
        "by_issue_type": _group("issue_type", "issue_type"),
        "by_priority": _group("priority", "priority"),
    }


def _transitions_section(conn) -> dict[str, Any]:
    if not _table_exists(conn, "ticket_status_history"):
        return {"available": False, "total": 0}
    totals = _one(
        conn,
        "SELECT COUNT(*) AS total, COUNT(DISTINCT jira_ticket_id) AS tickets "
        "FROM ticket_status_history",
    )
    by_transition = _rows(
        conn,
        "SELECT COALESCE(from_status,'(start)') AS from_status, to_status, COUNT(*) AS count "
        "FROM ticket_status_history GROUP BY 1, 2 ORDER BY count DESC LIMIT 50",
    )
    by_to_status = _rows(
        conn,
        "SELECT to_status, COUNT(*) AS count FROM ticket_status_history "
        "GROUP BY 1 ORDER BY count DESC",
    )
    recent = _rows(
        conn,
        "SELECT jira_ticket_id, from_status, to_status, assignee_name, "
        "to_char(changed_at, 'YYYY-MM-DD\"T\"HH24:MI:SSZ') AS changed_at "
        "FROM ticket_status_history ORDER BY changed_at DESC LIMIT 25",
    )
    return {
        "available": True,
        "total": _int(totals.get("total")),
        "distinct_tickets": _int(totals.get("tickets")),
        "by_transition": [
            {
                "from_status": r["from_status"],
                "to_status": r["to_status"],
                "count": _int(r["count"]),
            }
            for r in by_transition
        ],
        "by_to_status": [
            {"to_status": r["to_status"], "count": _int(r["count"])} for r in by_to_status
        ],
        "recent": recent,
    }


def _due_date_section(conn) -> dict[str, Any]:
    if not _table_exists(conn, "due_date_tracking"):
        return {"available": False}
    tracked = _int(_one(conn, "SELECT COUNT(*) AS c FROM due_date_tracking").get("c"))
    alerts = _one(
        conn,
        "SELECT "
        "COALESCE(SUM(CASE WHEN alert_75_sent THEN 1 ELSE 0 END),0) AS a75, "
        "COALESCE(SUM(CASE WHEN alert_50_sent THEN 1 ELSE 0 END),0) AS a50, "
        "COALESCE(SUM(CASE WHEN alert_25_sent THEN 1 ELSE 0 END),0) AS a25, "
        "COALESCE(SUM(CASE WHEN alert_0_sent  THEN 1 ELSE 0 END),0) AS a0 "
        "FROM due_date_tracking",
    )
    return {
        "available": True,
        "tracked": tracked,
        "alerts": {
            "lt_75pct": _int(alerts.get("a75")),
            "lt_50pct": _int(alerts.get("a50")),
            "lt_25pct": _int(alerts.get("a25")),
            "breached": _int(alerts.get("a0")),
        },
    }


def _sla_section(conn) -> dict[str, Any]:
    if not _table_exists(conn, "sla_tracking"):
        return {"available": False}
    total = _int(_one(conn, "SELECT COUNT(*) AS c FROM sla_tracking").get("c"))
    return {"available": True, "tracked": total}


def _documentation_section(conn) -> dict[str, Any]:
    if not _table_exists(conn, "doc_generation_usage"):
        return {"available": False}
    totals = _one(
        conn,
        "SELECT COUNT(*) AS jobs, "
        "COALESCE(SUM(CASE WHEN reused THEN 1 ELSE 0 END),0) AS reused, "
        "COALESCE(SUM(input_tokens + output_tokens),0) AS tokens, "
        "COALESCE(SUM(cost_usd),0) AS cost FROM doc_generation_usage",
    )
    by_type = _rows(
        conn,
        "SELECT doc_type, COUNT(*) AS jobs FROM doc_generation_usage "
        "GROUP BY 1 ORDER BY jobs DESC",
    )
    return {
        "available": True,
        "generated": _int(totals.get("jobs")),
        "reused": _int(totals.get("reused")),
        "total_tokens": _int(totals.get("tokens")),
        "total_cost_usd": round(float(totals.get("cost") or 0), 4),
        "by_type": [{"doc_type": r["doc_type"], "jobs": _int(r["jobs"])} for r in by_type],
    }


def _conversations_section(conn) -> dict[str, Any]:
    if not _table_exists(conn, "jira_slack_conversations"):
        return {"available": False}
    totals = _one(
        conn,
        "SELECT COUNT(*) AS threads, COUNT(DISTINCT jira_issue_key) AS tickets "
        "FROM jira_slack_conversations",
    )
    return {
        "available": True,
        "threads": _int(totals.get("threads")),
        "tickets": _int(totals.get("tickets")),
    }


# ── metric drill-down (which tickets back each stat) ─────────────────────────
# Each dashboard box maps to a SQL query that lists the tickets contributing to
# it. `kind` is "ticket" (jira_ticket_id → Jira link) or "repo" (no ticket, e.g.
# generated docs are keyed by repo+doc_type, not a ticket). Lists are capped so
# a click never returns thousands of rows; `truncated`/`total` tell the UI.
_DRILL_LIMIT = 500


def _metric_specs() -> dict[str, dict[str, Any]]:
    return {
        # Test Cases Built / Tickets w/ Test Cases / Avg Cases per Ticket all
        # resolve to the same set: the tickets that have AI-generated cases.
        "test_cases": {
            "label": "Tickets with AI-generated test cases",
            "kind": "ticket",
            "table": "test_cases",
            "sql": (
                "SELECT jira_ticket_id AS id, COUNT(*) AS n "
                "FROM test_cases GROUP BY jira_ticket_id ORDER BY n DESC"
            ),
            "extra": lambda r: f"{_int(r.get('n'))} cases",
        },
        "doc_reviews": {
            "label": "Tickets with reviewed PRD/BRD/Tech docs",
            "kind": "ticket",
            "table": "doc_reviews",
            "sql": (
                "SELECT jira_ticket_id AS id, COUNT(*) AS docs, "
                "COALESCE(SUM(review_count),0) AS runs "
                "FROM doc_reviews GROUP BY jira_ticket_id ORDER BY runs DESC"
            ),
            "extra": lambda r: f"{_int(r.get('docs'))} docs, {_int(r.get('runs'))} runs",
        },
        "transitions": {
            "label": "Tickets with recorded state transitions",
            "kind": "ticket",
            "table": "ticket_status_history",
            "sql": (
                "SELECT jira_ticket_id AS id, COUNT(*) AS n "
                "FROM ticket_status_history GROUP BY jira_ticket_id ORDER BY n DESC"
            ),
            "extra": lambda r: f"{_int(r.get('n'))} moves",
        },
        "tickets": {
            "label": "Cached Jira tickets",
            "kind": "ticket",
            "table": "jira_ticket_cache",
            # Filtered by status when the UI drills from the status table.
            "sql": (
                "SELECT ticket_key AS id, status, summary "
                "FROM jira_ticket_cache {where} ORDER BY ticket_key"
            ),
            "filter_col": "status",
        },
        "conversations": {
            "label": "Tickets with Slack Q&A threads",
            "kind": "ticket",
            "table": "jira_slack_conversations",
            "sql": (
                "SELECT jira_issue_key AS id, COUNT(*) AS n "
                "FROM jira_slack_conversations GROUP BY jira_issue_key ORDER BY n DESC"
            ),
            "extra": lambda r: f"{_int(r.get('n'))} threads",
        },
        "documentation": {
            "label": "Generated documents (by repo)",
            "kind": "repo",
            "table": "doc_generation_usage",
            "sql": (
                "SELECT repo AS id, doc_type, COUNT(*) AS n "
                "FROM doc_generation_usage GROUP BY repo, doc_type ORDER BY n DESC"
            ),
            "extra": lambda r: f"{r.get('doc_type') or '?'} · {_int(r.get('n'))} jobs",
        },
    }


def build_metric_drilldown(
    settings: Settings, metric: str, *, status: str = ""
) -> dict[str, Any]:
    """List the tickets (or repos) that make up one dashboard stat box."""
    specs = _metric_specs()
    spec = specs.get(metric)
    base = {
        "metric": metric,
        "jira_base_url": settings.jira_base_url,
        "items": [],
        "total": 0,
        "truncated": False,
    }
    if spec is None:
        return {**base, "label": metric, "error": f"unknown metric '{metric}'"}
    base["label"] = spec["label"]
    base["kind"] = spec["kind"]
    if not settings.database_url:
        return base

    with _connect(settings) as conn:
        if not _table_exists(conn, spec["table"]):
            return base

        params: tuple = ()
        sql = spec["sql"]
        if "{where}" in sql:
            if status and spec.get("filter_col"):
                sql = sql.replace("{where}", f"WHERE {spec['filter_col']} = %s")
                params = (status,)
            else:
                sql = sql.replace("{where}", "")

        rows = _rows(conn, sql, params)
        base["total"] = len(rows)
        if len(rows) > _DRILL_LIMIT:
            base["truncated"] = True
            rows = rows[:_DRILL_LIMIT]

        extra = spec.get("extra")
        items = []
        for r in rows:
            item: dict[str, Any] = {"id": r.get("id")}
            if "summary" in r:
                item["summary"] = r.get("summary")
            if "status" in r:
                item["status"] = r.get("status")
            if extra:
                item["extra"] = extra(r)
            items.append(item)

        # Enrich ticket-kind metrics that lack inline summaries with the cached
        # title/status, so the popover shows what each ticket is about.
        if spec["kind"] == "ticket" and items and "summary" not in (items[0] or {}):
            ids = [i["id"] for i in items if i.get("id")]
            if ids and _table_exists(conn, "jira_ticket_cache"):
                meta = _rows(
                    conn,
                    "SELECT ticket_key, status, summary FROM jira_ticket_cache "
                    "WHERE ticket_key = ANY(%s)",
                    (ids,),
                )
                by_key = {m["ticket_key"]: m for m in meta}
                for it in items:
                    m = by_key.get(it["id"])
                    if m:
                        it["summary"] = m.get("summary")
                        it["status"] = m.get("status")

        base["items"] = items

    # Overlay the *current* status straight from Jira so the list never shows a
    # stale cached value. Best-effort: on any failure each item keeps its cached
    # status. Done outside the DB connection block (it's a network call).
    if spec["kind"] == "ticket" and base["items"]:
        try:
            from app.jira_fetcher import fetch_live_statuses

            live = fetch_live_statuses([i["id"] for i in base["items"] if i.get("id")])
            if live:
                base["status_source"] = "live"
                for it in base["items"]:
                    fresh = live.get(it["id"])
                    if fresh:
                        it["status"] = fresh
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("live status overlay failed: %s", exc)

    return base
