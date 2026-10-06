"""Shared store and time maths for WF3/WF4 effort tracking.

The clock measures a developer against *their own* Jira Original Estimate, in
working hours, from the moment the ticket enters the dev status. It is not a
judgement of work quality - only of timeline adherence.

Elapsed hours are recomputed from `tracking_started_at` on every run rather than
accumulated, so a missed or duplicated cron run cannot drift the total.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


# ── time maths ────────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))


def elapsed_working_hours(
    start: datetime,
    now: datetime,
    hours_per_day: float = 8.0,
    work_start_hour: int = 9,
    work_start_minute: int = 30,
) -> float:
    """Calculate exact elapsed working hours within IST business hours (Monday-Friday, 40h/week).

    Default working window is 9:30 AM to 5:30 PM IST (8.0 hours per day).
    Weekends (Saturday & Sunday) and hours outside business hours (nights/evenings)
    are completely excluded and do not count toward elapsed TAT.
    """
    if not start or now <= start:
        return 0.0

    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    start_ist = start.astimezone(IST)
    now_ist = now.astimezone(IST)

    total_seconds = 0.0
    current_day = start_ist.date()
    end_day = now_ist.date()

    while current_day <= end_day:
        if current_day.weekday() < 5:  # Monday (0) through Friday (4) only
            day_work_start = datetime(
                current_day.year, current_day.month, current_day.day,
                work_start_hour, work_start_minute, 0, tzinfo=IST
            )
            day_work_end = day_work_start + timedelta(hours=hours_per_day)

            effective_start = max(start_ist, day_work_start)
            effective_end = min(now_ist, day_work_end)

            if effective_end > effective_start:
                total_seconds += (effective_end - effective_start).total_seconds()

        current_day += timedelta(days=1)

    return round(total_seconds / 3600.0, 2)


def percentage_used(elapsed: float, estimate: float) -> float:
    if not estimate or estimate <= 0:
        return 0.0
    return (elapsed / estimate) * 100.0


def alert_level(pct: float, row: dict[str, Any]) -> str | None:
    """Which alert is due, honouring the once-only flags. Highest level wins."""
    if pct >= 100 and not row.get("alert_breached_sent"):
        return "BREACHED"
    if pct >= 75 and not row.get("alert_25_sent"):
        return "25_REMAINING"
    if pct >= 50 and not row.get("alert_50_sent"):
        return "50_USED"
    return None


ALERT_FLAG = {
    "50_USED": "alert_50_sent",
    "25_REMAINING": "alert_25_sent",
    "BREACHED": "alert_breached_sent",
}

# Ordered low to high. Firing a level also marks every level below it as sent:
# a ticket that jumps straight past 50% to 75% must not then fire the 50% alert
# on the following run, walking backwards down the ladder.
_LADDER = ["50_USED", "25_REMAINING", "BREACHED"]


def flags_for_level(level: str) -> dict[str, bool]:
    """Flags to set when `level` fires: that level plus all lower ones."""
    idx = _LADDER.index(level)
    return {ALERT_FLAG[name]: True for name in _LADDER[: idx + 1]}


def drift_pct(estimate_hours: float, predicted_hours: float) -> float | None:
    """How far the dev's estimate sits from the bot's, as a signed percentage.

    Positive means the dev estimated more time than the bot thinks is needed.
    """
    if not predicted_hours or predicted_hours <= 0 or not estimate_hours:
        return None
    return round(((estimate_hours - predicted_hours) / predicted_hours) * 100.0, 1)


# ── store ─────────────────────────────────────────────────────────────────────

@dataclass
class EffortStore:
    settings: Settings

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.settings.database_url, row_factory=dict_row)

    def get(self, jira_ticket_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM effort_tracking WHERE UPPER(jira_ticket_id) = UPPER(%s)",
                (jira_ticket_id,),
            ).fetchone()

    def upsert(self, jira_ticket_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(fields)
        placeholders = ", ".join(["%s"] * len(fields))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in fields)
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO effort_tracking (jira_ticket_id, {cols})
                VALUES (%s, {placeholders})
                ON CONFLICT (jira_ticket_id) DO UPDATE SET {updates}, updated_at = NOW()
                """,
                (jira_ticket_id.upper(), *fields.values()),
            )
            conn.commit()

    def reset_clock(self, jira_ticket_id: str) -> None:
        """Ticket left the dev status: start fresh if it comes back.

        Per the agreed behaviour, time from a previous dev cycle is discarded
        rather than accumulated.
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE effort_tracking
                SET tracking_started_at = NULL, elapsed_hours = 0,
                    alert_50_sent = FALSE, alert_25_sent = FALSE,
                    alert_breached_sent = FALSE, last_checkin_at = NULL,
                    updated_at = NOW()
                WHERE UPPER(jira_ticket_id) = UPPER(%s) AND tracking_started_at IS NOT NULL
                """,
                (jira_ticket_id,),
            )
            conn.commit()

    def in_dev_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM effort_tracking
                WHERE is_complete = FALSE
                  AND tracking_started_at IS NOT NULL
                  AND estimate_hours IS NOT NULL AND estimate_hours > 0
                ORDER BY tracking_started_at ASC
                """
            ).fetchall()

    def cached_tickets_by_status(self, statuses: list[str]) -> list[dict[str, Any]]:
        """Tickets currently in any of the given statuses, from the Jira cache.

        Reconciling from the cache rather than a webhook makes tracking
        self-healing: a missed event, a restart, or tickets already in dev before
        this shipped are all picked up on the next run.
        """
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT ticket_key, project_key, summary, status, assignee_name
                FROM jira_ticket_cache
                WHERE LOWER(TRIM(status)) = ANY(%s)
                """,
                ([s.strip().lower() for s in statuses],),
            ).fetchall()

    def slack_id_for(self, assignee_name: str | None) -> str:
        """Resolve a Jira display name to a Slack DM channel via channelid_table."""
        if not assignee_name:
            return ""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT channel_id FROM channelid_table
                    WHERE LOWER(display_name) = LOWER(%s)
                       OR LOWER(slack_user_name) = LOWER(%s)
                    LIMIT 1
                    """,
                    (assignee_name, assignee_name),
                ).fetchone()
            return str((row or {}).get("channel_id") or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not resolve Slack channel for %s: %s", assignee_name, exc)
            return ""
