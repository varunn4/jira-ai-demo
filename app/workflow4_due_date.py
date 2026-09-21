"""Workflow4 due-date compliance checker for n8n polling."""

from __future__ import annotations

import logging
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from app.config import Settings


LOGGER = logging.getLogger(__name__)

DONE_STATUSES = {
    "Done",
    "Closed",
    "Resolved",
    "Ready for QA",
    "Ready for Deployment",
}

ROLE_QUERIES = {
    "eng_lead": "SELECT channel_id FROM channelid_table WHERE role = 'eng_lead' LIMIT 1",
    "cto": "SELECT channel_id FROM channelid_table WHERE role = 'cto' LIMIT 1",
    "ceo": "SELECT channel_id FROM channelid_table WHERE role = 'ceo' LIMIT 1",
    "team_channel": "SELECT channel_id FROM channelid_table WHERE role = 'team_channel' LIMIT 1",
    "jira_owner": "SELECT channel_id FROM channelid_table WHERE role = 'jira_owner' LIMIT 1",
}

ALERT_FLAG_COLUMNS = {
    "75_REMAINING": "alert_75_sent",
    "50_REMAINING": "alert_50_sent",
    "25_REMAINING": "alert_25_sent",
    "BREACHED": "alert_0_sent",
}

EXCEEDED_INTERVAL_HOURS = {
    "P0": 1,
    "P1": 24,
    "P2": 336,
    "P3": 168,
    "P4": 168,
}


class Workflow4DueDateChecker:
    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    def check(self) -> dict[str, Any]:
        self._log_step("validate_config", "started")
        if not self.settings.database_url:
            self._log_step("validate_config", "failed", output={"missing": "DATABASE_URL"})
            raise RuntimeError("DATABASE_URL is required for workflow4")
        if not self._jira_is_configured():
            self._log_step("validate_config", "failed", output={"missing": "Jira credentials"})
            raise RuntimeError("Jira credentials are required for workflow4")
        self._log_step(
            "validate_config",
            "completed",
            output={"database_url_configured": True, "jira_configured": True},
        )

        try:
            self._log_step("load_database_driver", "started", input_data={"driver": "psycopg2"})
            import psycopg2
            from psycopg2.extras import RealDictCursor
            self._log_step("load_database_driver", "completed", output={"driver": "psycopg2"})
        except ImportError as exc:
            self._log_step("load_database_driver", "failed", output={"driver": "psycopg2"})
            raise RuntimeError("The 'psycopg2-binary' package is required") from exc

        alerts: list[dict[str, str]] = []
        completed_count = 0

        with psycopg2.connect(self.settings.database_url) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                self._log_step("fetch_role_channels", "started")
                role_channels = self._fetch_role_channels(cursor)
                self._log_step("fetch_role_channels", "completed", output=role_channels)
                self._log_step("fetch_active_tickets", "started")
                tickets = self._fetch_active_tickets(cursor)
                total_count = len(tickets)
                self._log_step(
                    "fetch_active_tickets",
                    "completed",
                    output={"ticket_count": total_count},
                )
                LOGGER.info("workflow4 started: checking %s active tickets", total_count)

                for ticket in tickets:
                    jira_ticket_id = str(ticket.get("jira_ticket_id") or "")
                    try:
                        self._log_step(
                            "process_ticket",
                            "started",
                            input_data={
                                "jira_ticket_id": jira_ticket_id,
                                "priority": ticket.get("priority"),
                                "due_date": ticket.get("due_date"),
                            },
                        )
                        self._log_step(
                            "check_jira_status",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id},
                        )
                        if self._jira_ticket_is_done(jira_ticket_id):
                            self._log_step(
                                "check_jira_status",
                                "completed",
                                output={"jira_ticket_id": jira_ticket_id, "is_done": True},
                            )
                            self._mark_completed(cursor, jira_ticket_id)
                            conn.commit()
                            completed_count += 1
                            self._log_step(
                                "process_ticket",
                                "completed",
                                output={"jira_ticket_id": jira_ticket_id, "result": "completed"},
                            )
                            LOGGER.info("workflow4 %s: completed (ticket is Done)", jira_ticket_id)
                            continue
                        self._log_step(
                            "check_jira_status",
                            "completed",
                            output={"jira_ticket_id": jira_ticket_id, "is_done": False},
                        )

                        self._log_step(
                            "calculate_due_date_progress",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id},
                        )
                        progress = self._calculate_progress(ticket)
                        self._log_step(
                            "calculate_due_date_progress",
                            "completed",
                            output={
                                "jira_ticket_id": jira_ticket_id,
                                "today": progress["today"],
                                "due_date": progress["due_date"],
                                "tracking_start": progress["tracking_start"],
                                "elapsed_working_days": progress["elapsed_working_days"],
                                "total_working_days": progress["total_working_days"],
                                "percentage_used": progress["percentage_used"],
                            },
                        )
                        self._log_step(
                            "determine_alert_level",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id},
                        )
                        alert_level = self._determine_alert_level(ticket, progress["percentage_used"])
                        self._log_step(
                            "determine_alert_level",
                            "completed",
                            output={"jira_ticket_id": jira_ticket_id, "alert_level": alert_level},
                        )
                        LOGGER.info(
                            "workflow4 %s: %.1f%% used, alert=%s",
                            jira_ticket_id,
                            progress["percentage_used"],
                            alert_level,
                        )
                        if alert_level is None:
                            self._log_step(
                                "process_ticket",
                                "completed",
                                output={"jira_ticket_id": jira_ticket_id, "result": "no_alert_needed"},
                            )
                            continue

                        priority = str(ticket.get("priority") or "").upper()
                        if alert_level == "EXCEEDED":
                            self._log_step(
                                "evaluate_exceeded_interval",
                                "started",
                                input_data={"jira_ticket_id": jira_ticket_id, "priority": priority},
                            )
                            days_overdue = self._count_working_days(
                                progress["due_date"],
                                progress["today"],
                            )
                            LOGGER.info(
                                "workflow4 %s: due date exceeded, days overdue=%s",
                                jira_ticket_id,
                                days_overdue,
                            )
                            if not self._should_send_exceeded(ticket, priority):
                                self._log_step(
                                    "evaluate_exceeded_interval",
                                    "completed",
                                    output={
                                        "jira_ticket_id": jira_ticket_id,
                                        "should_send": False,
                                        "days_overdue": days_overdue,
                                    },
                                )
                                self._log_step(
                                    "process_ticket",
                                    "completed",
                                    output={
                                        "jira_ticket_id": jira_ticket_id,
                                        "result": "exceeded_alert_suppressed",
                                    },
                                )
                                continue
                            self._log_step(
                                "evaluate_exceeded_interval",
                                "completed",
                                output={
                                    "jira_ticket_id": jira_ticket_id,
                                    "should_send": True,
                                    "days_overdue": days_overdue,
                                },
                            )
                        else:
                            days_overdue = None

                        self._log_step(
                            "select_target_channels",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id, "alert_level": alert_level},
                        )
                        target_channels = self._target_channels(ticket, role_channels, alert_level)
                        self._log_step(
                            "select_target_channels",
                            "completed",
                            output={"jira_ticket_id": jira_ticket_id, "target_channels": target_channels},
                        )
                        if not target_channels:
                            self._log_step(
                                "process_ticket",
                                "completed",
                                output={"jira_ticket_id": jira_ticket_id, "result": "no_target_channels"},
                            )
                            continue

                        self._log_step(
                            "build_alert_message",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id, "alert_level": alert_level},
                        )
                        message = self._build_message(
                            ticket=ticket,
                            alert_level=alert_level,
                            due_date=progress["due_date"],
                            elapsed_working_days=progress["elapsed_working_days"],
                            total_working_days=progress["total_working_days"],
                            days_overdue=days_overdue,
                        )
                        self._log_step(
                            "build_alert_message",
                            "completed",
                            output={
                                "jira_ticket_id": jira_ticket_id,
                                "message_length": len(message),
                            },
                        )
                        for target_channel in target_channels:
                            alerts.append(
                                {
                                    "channel_id": target_channel,
                                    "message": message,
                                }
                            )

                        self._log_step(
                            "mark_alert_sent",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id, "alert_level": alert_level},
                        )
                        self._mark_alert_sent(cursor, jira_ticket_id, alert_level)
                        conn.commit()
                        self._log_step(
                            "process_ticket",
                            "completed",
                            output={
                                "jira_ticket_id": jira_ticket_id,
                                "result": "alert_created",
                                "alert_level": alert_level,
                                "target_channel_count": len(target_channels),
                            },
                        )
                    except Exception:
                        conn.rollback()
                        self._log_step(
                            "process_ticket",
                            "failed",
                            output={"jira_ticket_id": jira_ticket_id},
                        )
                        LOGGER.exception("workflow4 %s: failed processing ticket", jira_ticket_id)
                        continue

        LOGGER.info(
            "workflow4 completed: %s alerts, %s completed",
            len(alerts),
            completed_count,
        )
        self._log_step(
            "workflow4_response",
            "completed",
            output={
                "tickets_checked": total_count,
                "alerts_sent": len(alerts),
                "completed_tickets": completed_count,
            },
        )
        return {
            "status": "completed",
            "tickets_checked": total_count,
            "alerts_sent": len(alerts),
            "completed_tickets": completed_count,
            "alerts": alerts,
        }

    def _fetch_role_channels(self, cursor: Any) -> dict[str, str | None]:
        channels: dict[str, str | None] = {}
        for key, query in ROLE_QUERIES.items():
            cursor.execute(query)
            row = cursor.fetchone()
            channels[key] = self._clean_channel(row.get("channel_id")) if row else None
        return channels

    def _fetch_active_tickets(self, cursor: Any) -> list[dict[str, Any]]:
        # `project_key`, when set on a subclass, scopes the scan to one Jira
        # project (e.g. the AIGOV sandbox). Otherwise we scan every project but
        # exclude the sandbox key(s) in JIRA_EXCLUDED_PROJECT_KEYS so leftover
        # AIGOV test rows never surface in the production digest.
        project_key = getattr(self, "project_key", None)
        include = self._included_project_keys()
        if project_key:
            cursor.execute(
                """
                SELECT *
                FROM due_date_tracking
                WHERE is_completed = FALSE
                  AND jira_ticket_id LIKE %s
                ORDER BY due_date ASC
                """,
                (f"{project_key}-%",),
            )
        elif include:
            like = " OR ".join("jira_ticket_id LIKE %s" for _ in include)
            cursor.execute(
                f"""
                SELECT *
                FROM due_date_tracking
                WHERE is_completed = FALSE
                  AND ({like})
                ORDER BY due_date ASC
                """,
                tuple(f"{key}-%" for key in include),
            )
        else:
            excluded = self._excluded_project_keys()
            if excluded:
                not_like = " AND ".join("jira_ticket_id NOT LIKE %s" for _ in excluded)
                cursor.execute(
                    f"""
                    SELECT *
                    FROM due_date_tracking
                    WHERE is_completed = FALSE
                      AND {not_like}
                    ORDER BY due_date ASC
                    """,
                    tuple(f"{key}-%" for key in excluded),
                )
            else:
                cursor.execute(
                    """
                    SELECT *
                    FROM due_date_tracking
                    WHERE is_completed = FALSE
                    ORDER BY due_date ASC
                    """
                )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _split_project_keys(raw: str) -> list[str]:
        import re

        keys: list[str] = []
        seen: set[str] = set()
        for part in re.split(r"[\s,]+", (raw or "").strip()):
            key = part.strip()
            if key and key.upper() not in seen:
                seen.add(key.upper())
                keys.append(key)
        return keys

    def _excluded_project_keys(self) -> list[str]:
        """Sandbox project keys (JIRA_EXCLUDED_PROJECT_KEYS) to keep out of the
        all-projects scan. Empty for project-scoped subclasses."""
        return self._split_project_keys(getattr(self.settings, "jira_excluded_project_keys", ""))

    def _included_project_keys(self) -> list[str]:
        """Explicit include-list (WORKFLOW4_PROJECT_KEYS) restricting the
        production scan to these projects, e.g. ["RFT"]. Blank = no restriction."""
        return self._split_project_keys(getattr(self.settings, "workflow4_project_keys", ""))

    def _jira_ticket_is_done(self, jira_ticket_id: str) -> bool:
        if not jira_ticket_id:
            raise ValueError("jira_ticket_id is required")

        response = requests.get(
            f"{self.settings.jira_base_url}/rest/api/3/issue/{jira_ticket_id}",
            params={"fields": "status"},
            auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
            headers={"Accept": "application/json"},
            timeout=self.settings.external_request_timeout_seconds,
        )
        response.raise_for_status()
        status_name = response.json().get("fields", {}).get("status", {}).get("name")
        return status_name in DONE_STATUSES

    def _jira_is_configured(self) -> bool:
        return bool(self.settings.jira_base_url and self.settings.jira_email and self.settings.jira_api_token)

    def _mark_completed(self, cursor: Any, jira_ticket_id: str) -> None:
        cursor.execute(
            """
            UPDATE due_date_tracking
            SET is_completed = TRUE
            WHERE jira_ticket_id = %s
            """,
            (jira_ticket_id,),
        )

    def _calculate_progress(self, ticket: dict[str, Any]) -> dict[str, Any]:
        today = date.today()
        due_date = self._coerce_date(ticket.get("due_date"), "due_date")
        tracking_start = self._coerce_date(ticket.get("tracking_start_date"), "tracking_start_date")
        total_working_days = int(ticket.get("total_working_days") or 0)

        elapsed_working_days = self._count_working_days(
            tracking_start,
            min(today, due_date),
        )
        if total_working_days > 0:
            percentage_used = (elapsed_working_days / total_working_days) * 100
        else:
            percentage_used = 100

        return {
            "today": today,
            "due_date": due_date,
            "tracking_start": tracking_start,
            "total_working_days": total_working_days,
            "elapsed_working_days": elapsed_working_days,
            "percentage_used": percentage_used,
        }

    def _coerce_date(self, value: Any, field_name: str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        raise ValueError(f"{field_name} must be a date")

    def _count_working_days(self, start: date, end: date) -> int:
        count = 0
        current = start
        while current <= end:
            if current.weekday() < 5:
                count += 1
            current += timedelta(days=1)
        return count

    def _determine_alert_level(self, ticket: dict[str, Any], percentage_used: float) -> str | None:
        today = date.today()
        due_date = self._coerce_date(ticket.get("due_date"), "due_date")
        is_exceeded = today > due_date
        alert_0_sent = ticket.get("alert_0_sent")
        alert_25_sent = ticket.get("alert_25_sent")
        alert_50_sent = ticket.get("alert_50_sent")
        alert_75_sent = ticket.get("alert_75_sent")

        if is_exceeded:
            return "EXCEEDED"
        elif alert_0_sent:
            return None
        elif percentage_used >= 100:
            return "BREACHED"
        elif alert_25_sent:
            return None
        elif percentage_used >= 75:
            return "25_REMAINING"
        elif alert_50_sent:
            return None
        elif percentage_used >= 50:
            return "50_REMAINING"
        elif alert_75_sent:
            return None
        elif percentage_used >= 25:
            return "75_REMAINING"
        else:
            return None

    def _should_send_exceeded(self, ticket: dict[str, Any], priority: str) -> bool:
        exceeded_interval = EXCEEDED_INTERVAL_HOURS.get(priority, 168)
        now_utc = datetime.now(timezone.utc)
        exceeded_alert_sent_at = ticket.get("exceeded_alert_sent_at")
        if exceeded_alert_sent_at is None:
            return True
        if not isinstance(exceeded_alert_sent_at, datetime):
            raise ValueError("exceeded_alert_sent_at must be a timestamp")
        if exceeded_alert_sent_at.tzinfo is None:
            exceeded_alert_sent_at = exceeded_alert_sent_at.replace(tzinfo=timezone.utc)
        hours_since = (now_utc - exceeded_alert_sent_at).total_seconds() / 3600
        return hours_since >= exceeded_interval

    def _target_channels(
        self,
        ticket: dict[str, Any],
        role_channels: dict[str, str | None],
        alert_level: str,
    ) -> list[str]:
        priority = str(ticket.get("priority") or "").upper()
        assignee_slack_id = self._clean_channel(ticket.get("assignee_slack_id"))
        matrix = {
            "P0": {
                "75_REMAINING": [assignee_slack_id],
                "50_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "25_REMAINING": [
                    assignee_slack_id,
                    role_channels["jira_owner"],
                    role_channels["eng_lead"],
                ],
                "BREACHED": [
                    assignee_slack_id,
                    role_channels["jira_owner"],
                    role_channels["eng_lead"],
                    role_channels["cto"],
                    role_channels["ceo"],
                ],
                "EXCEEDED": [role_channels["team_channel"]],
            },
            "P1": {
                "75_REMAINING": [],
                "50_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "25_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "BREACHED": [assignee_slack_id, role_channels["jira_owner"], role_channels["eng_lead"]],
                "EXCEEDED": [role_channels["team_channel"]],
            },
            "P2": {
                "75_REMAINING": [],
                "50_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "25_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "BREACHED": [assignee_slack_id, role_channels["jira_owner"]],
                "EXCEEDED": [role_channels["team_channel"]],
            },
            "P3": {
                "75_REMAINING": [],
                "50_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "25_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "BREACHED": [assignee_slack_id, role_channels["jira_owner"]],
                "EXCEEDED": [role_channels["team_channel"]],
            },
            "P4": {
                "75_REMAINING": [],
                "50_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "25_REMAINING": [assignee_slack_id, role_channels["jira_owner"]],
                "BREACHED": [assignee_slack_id, role_channels["jira_owner"]],
                "EXCEEDED": [role_channels["team_channel"]],
            },
        }
        return [
            channel
            for channel in matrix.get(priority, {}).get(alert_level, [])
            if channel is not None
        ]

    def _clean_channel(self, value: Any) -> str | None:
        channel = str(value or "").strip()
        return channel or None

    def _build_message(
        self,
        *,
        ticket: dict[str, Any],
        alert_level: str,
        due_date: date,
        elapsed_working_days: int,
        total_working_days: int,
        days_overdue: int | None,
    ) -> str:
        jira_ticket_id = str(ticket.get("jira_ticket_id") or "")
        priority = str(ticket.get("priority") or "")
        assignee_slack_id = str(ticket.get("assignee_slack_id") or "")

        if alert_level == "EXCEEDED":
            return (
                f"🚨 *Due Date EXCEEDED — {jira_ticket_id}*\n\n"
                f"*Priority:* {priority}\n"
                f"*Assignee:* <@{assignee_slack_id}>\n"
                f"*Due Date:* {due_date}\n"
                f"*Days Overdue:* {days_overdue} working days\n\n"
                f"👉 {self.settings.jira_base_url}/browse/{jira_ticket_id}"
            )

        return (
            f"⚠️ *Due Date Alert — {alert_level}*\n\n"
            f"*Ticket:* {jira_ticket_id}\n"
            f"*Priority:* {priority}\n"
            f"*Assignee:* <@{assignee_slack_id}>\n"
            f"*Due Date:* {due_date}\n"
            f"*Working Days Elapsed:* {elapsed_working_days} of {total_working_days}\n"
            f"*Status:* {alert_level}\n\n"
            f"👉 {self.settings.jira_base_url}/browse/{jira_ticket_id}"
        )

    def _mark_alert_sent(self, cursor: Any, jira_ticket_id: str, alert_level: str) -> None:
        if alert_level == "EXCEEDED":
            cursor.execute(
                """
                UPDATE due_date_tracking
                SET exceeded_alert_sent_at = NOW()
                WHERE jira_ticket_id = %s
                """,
                (jira_ticket_id,),
            )
            return

        column = ALERT_FLAG_COLUMNS[alert_level]
        cursor.execute(
            f"""
            UPDATE due_date_tracking
            SET {column} = TRUE
            WHERE jira_ticket_id = %s
            """,
            (jira_ticket_id,),
        )

    def _log_step(
        self,
        step: str,
        status: str,
        *,
        input_data: Any | None = None,
        output: Any | None = None,
    ) -> None:
        LOGGER.info(
            "workflow4 structured log: %s",
            json.dumps(
                {
                    "step": step,
                    "status": status,
                    "input": input_data,
                    "output": output,
                },
                ensure_ascii=False,
                default=str,
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# MoM-2 batch checkers (09:00 assignee / 15:00 TL)
#
# These run as separate scheduled batches and send ONE digest per recipient,
# re-sent every day a ticket stays in band (deduped within the same day via an
# `*_alerted_on` date column). "Time left" is measured against the active
# phase's Jira due date (Dev / QA / Live), chosen from the ticket's status.
# ─────────────────────────────────────────────────────────────────────────────
class _Workflow4BatchChecker(Workflow4DueDateChecker):
    """Shared scaffold for the MoM-2 batches; reuses the parent's working-day and
    role-channel helpers. Subclasses set the daily-dedupe column, the time-left
    threshold, and how the qualifying tickets fan out into digests."""

    name: str = "workflow4-batch"
    alerted_column: str = ""          # date column for once-per-day dedupe
    max_time_left_pct: float = 0.0    # qualify when time_left_pct is under this
    inclusive: bool = False           # True -> "<=" threshold, False -> "<"
    project_key: str | None = None    # when set, scope the scan to one project

    # ── subclass hooks ──────────────────────────────────────────────────────
    def _pre_check(self) -> None:
        """Hook run once before the due-date scan.

        Default is a no-op; subclasses may override to refresh upstream data
        (e.g. refetch Jira tickets and rebuild embeddings) before scanning."""
        return None

    def _build_digests(
        self, qualifying: list[dict[str, Any]], role_channels: dict[str, str | None]
    ) -> list[dict[str, str]]:
        raise NotImplementedError

    # ── Jira phase resolution ───────────────────────────────────────────────
    def _phase_fields(self) -> list[str]:
        fields = ["status", "duedate"]
        for fid in (
            self.settings.jira_dev_due_date_field,
            self.settings.jira_qa_due_date_field,
            self.settings.jira_live_due_date_field,
        ):
            if fid:
                fields.append(fid)
        return fields

    @staticmethod
    def _parse_jira_date(value: Any) -> date | None:
        if isinstance(value, str) and value.strip():
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
        return None

    def _fetch_jira_phase(self, jira_ticket_id: str) -> dict[str, Any]:
        if not jira_ticket_id:
            raise ValueError("jira_ticket_id is required")
        response = requests.get(
            f"{self.settings.jira_base_url}/rest/api/3/issue/{jira_ticket_id}",
            params={"fields": ",".join(self._phase_fields())},
            auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
            headers={"Accept": "application/json"},
            timeout=self.settings.external_request_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json().get("fields", {}) or {}
        status_name = ((data.get("status") or {}).get("name")) or ""
        s = self.settings

        def _d(fid: str) -> date | None:
            return self._parse_jira_date(data.get(fid)) if fid else None

        return {
            "status": status_name,
            "done": status_name in DONE_STATUSES,
            "dev_due": _d(s.jira_dev_due_date_field),
            "qa_due": _d(s.jira_qa_due_date_field),
            "live_due": _d(s.jira_live_due_date_field),
            "system_due": self._parse_jira_date(data.get("duedate")),
        }

    @staticmethod
    def _status_set(raw: str) -> set[str]:
        return {s.strip().lower() for s in (raw or "").split(",") if s.strip()}

    def _active_phase(
        self, status: str, info: dict[str, Any], ticket: dict[str, Any]
    ) -> tuple[str | None, date | None]:
        """Pick the phase due date that governs this ticket right now.

        Status decides the preferred phase; we then fall through to the other
        phase dates, the system `duedate`, and finally the SLA `due_date` so a
        ticket is never silently skipped for a missing custom field."""
        sl = (status or "").lower()
        dev = ("Dev", info.get("dev_due"))
        qa = ("QA", info.get("qa_due"))
        live = ("Live", info.get("live_due"))
        if sl in self._status_set(self.settings.jira_live_statuses):
            order = [live, qa, dev]
        elif sl in self._status_set(self.settings.jira_qa_statuses):
            order = [qa, dev, live]
        else:
            order = [dev, qa, live]
        for label, due in order:
            if due:
                return label, due
        if info.get("system_due"):
            return "Due", info["system_due"]
        row_due = self._safe_date(ticket.get("due_date"))
        return ("Due", row_due) if row_due else (None, None)

    def _safe_date(self, value: Any) -> date | None:
        try:
            return self._coerce_date(value, "date")
        except (ValueError, TypeError):
            return None

    def _phase_progress(self, ticket: dict[str, Any], active_due: date | None) -> dict[str, Any] | None:
        tracking_start = self._safe_date(ticket.get("tracking_start_date"))
        if active_due is None or tracking_start is None:
            return None
        today = date.today()
        total = self._count_working_days(tracking_start, active_due)
        remaining = 0 if today > active_due else self._count_working_days(today, active_due)
        if total <= 0:
            time_left_pct = 0.0
        else:
            time_left_pct = max(0.0, min(100.0, remaining / total * 100))
        return {"today": today, "due": active_due, "total": total,
                "remaining": remaining, "time_left_pct": time_left_pct}

    def _qualifies(self, time_left_pct: float) -> bool:
        if self.inclusive:
            return time_left_pct <= self.max_time_left_pct
        return time_left_pct < self.max_time_left_pct

    # ── digest formatting (shared) ──────────────────────────────────────────
    def _format_digest(self, title: str, items: list[dict[str, Any]], *, show_assignee: bool) -> str:
        rows = sorted(items, key=lambda t: t["time_left_pct"])
        lines = [f"*{title}*", f"_AI Governor · {date.today():%Y-%m-%d}_", ""]
        for t in rows:
            left = "overdue" if t["remaining"] <= 0 else f"{t['time_left_pct']:.0f}% left"
            who = f" · <@{t['assignee_slack_id']}>" if show_assignee and t["assignee_slack_id"] else ""
            prio = f" · {t['priority']}" if t["priority"] else ""
            lines.append(
                f"• *{t['key']}* — _{t['status']}_{prio} · {t['phase']} due {t['due']} · *{left}*{who}\n"
                f"   {self.settings.jira_base_url}/browse/{t['key']}"
            )
        return "\n".join(lines)

    # ── schema self-heal ────────────────────────────────────────────────────
    def _ensure_alert_columns(self, cursor: Any) -> None:
        cursor.execute("ALTER TABLE due_date_tracking ADD COLUMN IF NOT EXISTS dev_due_date date")
        cursor.execute("ALTER TABLE due_date_tracking ADD COLUMN IF NOT EXISTS qa_due_date date")
        cursor.execute("ALTER TABLE due_date_tracking ADD COLUMN IF NOT EXISTS live_due_date date")
        # alerted_column is a class constant (never user input) -> safe to inline.
        cursor.execute(
            f"ALTER TABLE due_date_tracking ADD COLUMN IF NOT EXISTS {self.alerted_column} date"
        )

    def check(self) -> dict[str, Any]:
        if not self.alerted_column:
            raise RuntimeError("alerted_column must be set on the batch checker")
        if not self.settings.database_url:
            raise RuntimeError(f"DATABASE_URL is required for {self.name}")
        if not self._jira_is_configured():
            raise RuntimeError(f"Jira credentials are required for {self.name}")

        self._pre_check()

        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as exc:
            raise RuntimeError("The 'psycopg2-binary' package is required") from exc

        today = date.today()
        qualifying: list[dict[str, Any]] = []
        completed_count = 0
        alerts: list[dict[str, str]] = []

        with psycopg2.connect(self.settings.database_url) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                self._ensure_alert_columns(cursor)
                conn.commit()
                role_channels = self._fetch_role_channels(cursor)
                tickets = self._fetch_active_tickets(cursor)
                LOGGER.info("%s started: checking %s active tickets", self.name, len(tickets))

                for ticket in tickets:
                    key = str(ticket.get("jira_ticket_id") or "")
                    try:
                        info = self._fetch_jira_phase(key)
                        if info["done"]:
                            self._mark_completed(cursor, key)
                            conn.commit()
                            completed_count += 1
                            continue

                        # Cache the phase dates on the row for visibility.
                        cursor.execute(
                            "UPDATE due_date_tracking SET dev_due_date = %s, "
                            "qa_due_date = %s, live_due_date = %s WHERE jira_ticket_id = %s",
                            (info["dev_due"], info["qa_due"], info["live_due"], key),
                        )

                        label, active_due = self._active_phase(info["status"], info, ticket)
                        progress = self._phase_progress(ticket, active_due)
                        if progress is None or not self._qualifies(progress["time_left_pct"]):
                            conn.commit()
                            continue

                        already = ticket.get(self.alerted_column)
                        if isinstance(already, datetime):
                            already = already.date()
                        if already == today:
                            conn.commit()
                            continue  # already alerted today; daily dedupe

                        qualifying.append({
                            "key": key,
                            "status": info["status"],
                            "phase": label,
                            "due": active_due,
                            "time_left_pct": progress["time_left_pct"],
                            "remaining": progress["remaining"],
                            "total": progress["total"],
                            "priority": str(ticket.get("priority") or ""),
                            "assignee_slack_id": self._clean_channel(ticket.get("assignee_slack_id")),
                        })
                        cursor.execute(
                            f"UPDATE due_date_tracking SET {self.alerted_column} = %s "
                            f"WHERE jira_ticket_id = %s",
                            (today, key),
                        )
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        LOGGER.exception("%s %s: failed processing ticket", self.name, key)
                        continue

                alerts = self._build_digests(qualifying, role_channels)

        LOGGER.info(
            "%s completed: %s qualifying ticket(s), %s digest(s), %s completed",
            self.name, len(qualifying), len(alerts), completed_count,
        )
        return {"alerts": alerts, "alerts_sent": len(alerts)}


class Workflow4AssigneeChecker(_Workflow4BatchChecker):
    """09:00 batch — under 75% time left -> a daily digest to each assignee plus
    one consolidated digest to the Jira Governor (jira_owner / Aryan)."""

    name = "workflow4-daily-assignee"
    alerted_column = "assignee_alerted_on"
    max_time_left_pct = 75.0
    inclusive = False  # strictly under 75% left

    def _build_digests(
        self, qualifying: list[dict[str, Any]], role_channels: dict[str, str | None]
    ) -> list[dict[str, str]]:
        if not qualifying:
            return []
        alerts: list[dict[str, str]] = []

        by_assignee: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in qualifying:
            if t["assignee_slack_id"]:
                by_assignee[t["assignee_slack_id"]].append(t)
        for channel, items in by_assignee.items():
            alerts.append({
                "channel_id": channel,
                "message": self._format_digest(
                    f"⏰ You have {len(items)} ticket(s) with under 75% of time left",
                    items, show_assignee=False,
                ),
            })

        governor = role_channels.get("jira_owner")
        if governor:
            alerts.append({
                "channel_id": governor,
                "message": self._format_digest(
                    f"⏰ Due-date summary — {len(qualifying)} ticket(s) under 75% time left",
                    qualifying, show_assignee=True,
                ),
            })
        return alerts


class Workflow4TLChecker(_Workflow4BatchChecker):
    """15:00 batch — 50% or less time left -> one consolidated digest to the
    Tech-Lead channel (eng_lead)."""

    name = "workflow4-tl"
    alerted_column = "tl_alerted_on"
    max_time_left_pct = 50.0
    inclusive = True  # 50% or less left

    def _build_digests(
        self, qualifying: list[dict[str, Any]], role_channels: dict[str, str | None]
    ) -> list[dict[str, str]]:
        tl_channel = role_channels.get("eng_lead")
        if not qualifying or not tl_channel:
            return []
        return [{
            "channel_id": tl_channel,
            "message": self._format_digest(
                f"🚧 TL escalation — {len(qualifying)} ticket(s) with 50% or less time left",
                qualifying, show_assignee=True,
            ),
        }]
