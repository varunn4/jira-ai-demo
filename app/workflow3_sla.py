"""Workflow3 SLA breach checker for n8n polling."""

from __future__ import annotations

import logging
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from app.config import Settings


LOGGER = logging.getLogger(__name__)

ROLE_QUERIES = {
    "eng_lead_channel": "SELECT channel_id FROM channelid_table WHERE role = 'eng_lead' LIMIT 1",
    "cto_channel": "SELECT channel_id FROM channelid_table WHERE role = 'cto' LIMIT 1",
    "ceo_channel": "SELECT channel_id FROM channelid_table WHERE role = 'ceo' LIMIT 1",
    "team_channel": "SELECT channel_id FROM channelid_table WHERE role = 'team_channel' LIMIT 1",
}

ALERT_FLAG_COLUMNS = {
    "75_REMAINING": "alert_75_sent",
    "50_REMAINING": "alert_50_sent",
    "25_REMAINING": "alert_25_sent",
    "BREACHED": "alert_0_sent",
}

SILENT_COMBINATIONS = {
    "P1": ["75_REMAINING"],
    "P2": ["75_REMAINING", "50_REMAINING"],
    "P3": ["75_REMAINING", "50_REMAINING", "25_REMAINING"],
    "P4": ["75_REMAINING", "50_REMAINING", "25_REMAINING"],
}


class Workflow3SLAChecker:
    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    def check(self) -> dict[str, Any]:
        self._log_step("validate_config", "started")
        if not self.settings.database_url:
            self._log_step("validate_config", "failed", output={"missing": "DATABASE_URL"})
            raise RuntimeError("DATABASE_URL is required for workflow3")
        self._log_step("validate_config", "completed", output={"database_url_configured": True})

        try:
            self._log_step("load_database_driver", "started", input_data={"driver": "psycopg2"})
            import psycopg2
            from psycopg2.extras import RealDictCursor
            self._log_step("load_database_driver", "completed", output={"driver": "psycopg2"})
        except ImportError as exc:
            self._log_step("load_database_driver", "failed", output={"driver": "psycopg2"})
            raise RuntimeError("The 'psycopg2-binary' package is required") from exc

        alerts: list[dict[str, str]] = []
        resolved_count = 0

        with psycopg2.connect(self.settings.database_url) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                self._log_step("fetch_role_channels", "started")
                role_channels = self._fetch_role_channels(cursor)
                self._log_step("fetch_role_channels", "completed", output=role_channels)
                self._log_step("fetch_unresolved_tickets", "started")
                tickets = self._fetch_unresolved_tickets(cursor)
                total_count = len(tickets)
                self._log_step(
                    "fetch_unresolved_tickets",
                    "completed",
                    output={"ticket_count": total_count},
                )
                LOGGER.info("workflow3 started: checking %s active tickets", total_count)

                for ticket in tickets:
                    jira_ticket_id = str(ticket.get("jira_ticket_id") or "")
                    try:
                        self._log_step(
                            "process_ticket",
                            "started",
                            input_data={
                                "jira_ticket_id": jira_ticket_id,
                                "priority": ticket.get("priority"),
                                "sla_window_hours": ticket.get("sla_window_hours"),
                            },
                        )
                        self._log_step(
                            "fetch_jira_due_date",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id},
                        )
                        jira_due_date = self._jira_due_date(jira_ticket_id)
                        self._log_step(
                            "fetch_jira_due_date",
                            "completed",
                            output={"jira_ticket_id": jira_ticket_id, "due_date": jira_due_date},
                        )
                        if jira_due_date is not None:
                            self._log_step(
                                "resolve_sla_and_create_due_date_tracking",
                                "started",
                                input_data={"jira_ticket_id": jira_ticket_id},
                            )
                            self._mark_resolved(cursor, jira_ticket_id)
                            tracking_start_date = date.today()
                            total_working_days = self._count_working_days(
                                tracking_start_date,
                                jira_due_date,
                            )
                            if total_working_days <= 0:
                                total_working_days = 1
                            self._insert_due_date_tracking(
                                cursor,
                                ticket=ticket,
                                due_date=jira_due_date,
                                tracking_start_date=tracking_start_date,
                                total_working_days=total_working_days,
                            )
                            conn.commit()
                            resolved_count += 1
                            self._log_step(
                                "resolve_sla_and_create_due_date_tracking",
                                "completed",
                                output={
                                    "jira_ticket_id": jira_ticket_id,
                                    "due_date": jira_due_date,
                                    "total_working_days": total_working_days,
                                },
                            )
                            LOGGER.info(
                                "workflow3 %s: due date found = %s, total working days = %s, "
                                "inserted into due_date_tracking",
                                jira_ticket_id,
                                jira_due_date,
                                total_working_days,
                            )
                            continue

                        self._log_step(
                            "calculate_sla_usage",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id},
                        )
                        elapsed_hours, percentage_used = self._calculate_sla_usage(ticket)
                        self._log_step(
                            "calculate_sla_usage",
                            "completed",
                            output={
                                "jira_ticket_id": jira_ticket_id,
                                "elapsed_hours": elapsed_hours,
                                "percentage_used": percentage_used,
                            },
                        )
                        self._log_step(
                            "determine_alert_level",
                            "started",
                            input_data={"jira_ticket_id": jira_ticket_id},
                        )
                        alert_level = self._determine_alert_level(ticket, percentage_used)
                        self._log_step(
                            "determine_alert_level",
                            "completed",
                            output={"jira_ticket_id": jira_ticket_id, "alert_level": alert_level},
                        )
                        if alert_level is None:
                            LOGGER.info("workflow3 %s: no alert needed", jira_ticket_id)
                            self._log_step(
                                "process_ticket",
                                "completed",
                                output={"jira_ticket_id": jira_ticket_id, "result": "no_alert_needed"},
                            )
                            continue

                        priority = str(ticket.get("priority") or "").upper()
                        if alert_level in SILENT_COMBINATIONS.get(priority, []):
                            LOGGER.info(
                                "workflow3 %s: silent alert level for %s",
                                jira_ticket_id,
                                priority,
                            )
                            self._mark_alert_sent(cursor, jira_ticket_id, alert_level)
                            conn.commit()
                            self._log_step(
                                "process_ticket",
                                "completed",
                                output={
                                    "jira_ticket_id": jira_ticket_id,
                                    "result": "silent_alert_marked",
                                    "alert_level": alert_level,
                                },
                            )
                            continue

                        LOGGER.info(
                            "workflow3 %s: %.1f%% SLA used, alert_level=%s",
                            jira_ticket_id,
                            percentage_used,
                            alert_level,
                        )
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
                            LOGGER.info("workflow3 %s: no target channels available", jira_ticket_id)
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
                            jira_ticket_id=jira_ticket_id,
                            priority=str(ticket.get("priority") or ""),
                            assignee_slack_id=str(ticket.get("assignee_slack_id") or ""),
                            elapsed_hours=elapsed_hours,
                            sla_window_hours=ticket.get("sla_window_hours"),
                            alert_level=alert_level,
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
                        LOGGER.exception("workflow3 %s: failed processing ticket", jira_ticket_id)
                        continue

        LOGGER.info(
            "workflow3 completed: %s alerts to send, %s resolved",
            len(alerts),
            resolved_count,
        )
        self._log_step(
            "workflow3_response",
            "completed",
            output={
                "tickets_checked": total_count,
                "alerts_sent": len(alerts),
                "resolved_tickets": resolved_count,
            },
        )
        return {
            "status": "completed",
            "tickets_checked": total_count,
            "alerts_sent": len(alerts),
            "resolved_tickets": resolved_count,
            "alerts": alerts,
        }

    def _fetch_role_channels(self, cursor: Any) -> dict[str, str | None]:
        channels: dict[str, str | None] = {}
        for key, query in ROLE_QUERIES.items():
            cursor.execute(query)
            row = cursor.fetchone()
            channels[key] = str(row["channel_id"]).strip() if row and row.get("channel_id") else None
        return channels

    def _fetch_unresolved_tickets(self, cursor: Any) -> list[dict[str, Any]]:
        cursor.execute(
            """
            SELECT
              s.id,
              s.jira_ticket_id,
              s.priority,
              s.assignee_slack_id,
              s.sla_start_time,
              s.sla_deadline,
              s.sla_window_hours,
              s.alert_75_sent,
              s.alert_50_sent,
              s.alert_25_sent,
              s.alert_0_sent,
              t.slack_channel_id
            FROM sla_tracking s
            JOIN tickets t ON s.ticket_id = t.id
            WHERE s.is_resolved = FALSE
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def _jira_due_date(self, jira_ticket_id: str) -> date | None:
        if not jira_ticket_id:
            raise ValueError("jira_ticket_id is required")
        if not self._jira_is_configured():
            raise RuntimeError("Jira credentials are required for workflow3")

        response = requests.get(
            f"{self.settings.jira_base_url}/rest/api/3/issue/{jira_ticket_id}",
            params={"fields": "duedate"},
            auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
            headers={"Accept": "application/json"},
            timeout=self.settings.external_request_timeout_seconds,
        )
        response.raise_for_status()
        due_date_value = response.json().get("fields", {}).get("duedate")
        if due_date_value is None:
            return None
        return date.fromisoformat(due_date_value)

    def _jira_is_configured(self) -> bool:
        return bool(self.settings.jira_base_url and self.settings.jira_email and self.settings.jira_api_token)

    def _mark_resolved(self, cursor: Any, jira_ticket_id: str) -> None:
        cursor.execute(
            """
            UPDATE sla_tracking
            SET is_resolved = TRUE
            WHERE jira_ticket_id = %s
            """,
            (jira_ticket_id,),
        )

    def _count_working_days(self, start_date: date, end_date: date) -> int:
        count = 0
        current = start_date
        while current <= end_date:
            if current.weekday() < 5:
                count += 1
            current += timedelta(days=1)
        return count

    def _insert_due_date_tracking(
        self,
        cursor: Any,
        *,
        ticket: dict[str, Any],
        due_date: date,
        tracking_start_date: date,
        total_working_days: int,
    ) -> None:
        jira_ticket_id = str(ticket.get("jira_ticket_id") or "")
        priority = str(ticket.get("priority") or "")
        assignee_slack_id = str(ticket.get("assignee_slack_id") or "")
        cursor.execute(
            """
            INSERT INTO due_date_tracking (
              ticket_id,
              jira_ticket_id,
              priority,
              assignee_slack_id,
              due_date,
              tracking_start_date,
              total_working_days
            )
            SELECT
              t.id,
              %s,
              %s,
              %s,
              %s,
              %s,
              %s
            FROM tickets t
            WHERE t.jira_ticket_id = %s
            ON CONFLICT (jira_ticket_id) DO NOTHING
            """,
            (
                jira_ticket_id,
                priority,
                assignee_slack_id,
                due_date,
                tracking_start_date,
                total_working_days,
                jira_ticket_id,
            ),
        )

    def _calculate_sla_usage(self, ticket: dict[str, Any]) -> tuple[float, float]:
        sla_start_time = ticket.get("sla_start_time")
        sla_window_hours = float(ticket.get("sla_window_hours") or 0)
        if not isinstance(sla_start_time, datetime):
            raise ValueError("sla_start_time must be a timestamp")
        if sla_window_hours <= 0:
            raise ValueError("sla_window_hours must be greater than zero")

        now_utc = datetime.now(timezone.utc)
        if sla_start_time.tzinfo is None:
            sla_start_time = sla_start_time.replace(tzinfo=timezone.utc)

        elapsed_hours = (now_utc - sla_start_time).total_seconds() / 3600
        percentage_used = (elapsed_hours / sla_window_hours) * 100
        return elapsed_hours, percentage_used

    def _determine_alert_level(self, ticket: dict[str, Any], percentage_used: float) -> str | None:
        alert_0_sent = ticket.get("alert_0_sent")
        alert_25_sent = ticket.get("alert_25_sent")
        alert_50_sent = ticket.get("alert_50_sent")
        alert_75_sent = ticket.get("alert_75_sent")

        if alert_0_sent:
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
                "50_REMAINING": [assignee_slack_id, role_channels["team_channel"]],
                "25_REMAINING": [role_channels["eng_lead_channel"]],
                "BREACHED": [role_channels["cto_channel"], role_channels["ceo_channel"]],
            },
            "P1": {
                "75_REMAINING": [],
                "50_REMAINING": [assignee_slack_id],
                "25_REMAINING": [assignee_slack_id, role_channels["team_channel"]],
                "BREACHED": [role_channels["eng_lead_channel"]],
            },
            "P2": {
                "75_REMAINING": [],
                "50_REMAINING": [],
                "25_REMAINING": [assignee_slack_id],
                "BREACHED": [assignee_slack_id, role_channels["team_channel"]],
            },
            "P3": {
                "75_REMAINING": [],
                "50_REMAINING": [],
                "25_REMAINING": [],
                "BREACHED": [assignee_slack_id],
            },
            "P4": {
                "75_REMAINING": [],
                "50_REMAINING": [],
                "25_REMAINING": [],
                "BREACHED": [assignee_slack_id],
            },
        }
        raw_target_channels = matrix.get(priority, {}).get(alert_level, [])
        target_channels = [
            channel
            for channel in raw_target_channels
            if channel is not None
        ]
        if raw_target_channels and not target_channels and assignee_slack_id is not None:
            LOGGER.info(
                "workflow3 target fallback: priority=%s alert_level=%s using assignee_slack_id",
                priority,
                alert_level,
            )
            return [assignee_slack_id]

        return target_channels

    def _clean_channel(self, value: Any) -> str | None:
        channel = str(value or "").strip()
        return channel or None

    def _build_message(
        self,
        *,
        jira_ticket_id: str,
        priority: str,
        assignee_slack_id: str,
        elapsed_hours: float,
        sla_window_hours: Any,
        alert_level: str,
    ) -> str:
        return (
            f"⚠️ *SLA Alert — {alert_level}*\n\n"
            f"*Ticket:* {jira_ticket_id}\n"
            f"*Priority:* {priority}\n"
            f"*Assignee:* <@{assignee_slack_id}>\n"
            f"*Time Elapsed:* {elapsed_hours:.1f} hours\n"
            f"*SLA Window:* {sla_window_hours} hours\n"
            f"*Status:* {alert_level}\n\n"
            f"👉 Please set a due date:\n"
            f"{self.settings.jira_base_url}/browse/{jira_ticket_id}"
        )

    def _mark_alert_sent(self, cursor: Any, jira_ticket_id: str, alert_level: str) -> None:
        column = ALERT_FLAG_COLUMNS[alert_level]
        cursor.execute(
            f"""
            UPDATE sla_tracking
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
            "workflow3 structured log: %s",
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
