"""WF3 — Timeline tracker.

Holds a developer accountable to their own Jira Original Estimate while a ticket
sits in the dev status. It measures timeline adherence only; it never judges the
quality of the work, and it never re-estimates the ticket.

Two entry points:
  run()           reconcile tracking rows and fire threshold alerts
  daily_checkin() one DM per developer, 10:30 IST, covering all their in-dev work
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.effort_tracking import (
    EffortStore,
    alert_level,
    elapsed_working_hours,
    flags_for_level,
    percentage_used,
)

log = logging.getLogger(__name__)


class Workflow3EffortTracker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = EffortStore(settings)

    # ── reconcile + alert ────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not self.settings.effort_tracking_enabled:
            return {"skipped": "EFFORT_TRACKING_ENABLED is false", "alerts_sent": 0}
        if not self.settings.database_url:
            return {"skipped": "DATABASE_URL not configured", "alerts_sent": 0}

        reconciled = self._reconcile()
        alerts = self._fire_alerts()
        closed = self._close_completed()

        log.info(
            "workflow3 effort check: tracked=%d alerts=%d closed=%d",
            reconciled, len(alerts), closed,
        )
        return {
            "tracked": reconciled,
            "alerts": alerts,
            "alerts_sent": len(alerts),
            "closed": closed,
        }

    def _reconcile(self) -> int:
        """Start clocks for tickets in dev, reset those that have left."""
        status_candidates = [
            s.strip() for s in (self.settings.effort_start_status or "").split(",") if s.strip()
        ]
        status_candidates.extend([
            "In Dev", "In Progress", "In Development", "Development", "In Progress - Dev", "Active"
        ])
        status_list = list({s.lower(): s for s in status_candidates}.values())

        # Always do a live Jira sync if possible so recent board moves are immediately recognized
        try:
            from app import jira_fetcher
            jira_fetcher.fetch_all_tickets(force_refresh=True)
        except Exception as sync_exc:
            log.warning("workflow3 live cache sync skipped: %s", sync_exc)

        cached_rows = self.store.cached_tickets_by_status(status_list)

        in_dev = {
            str(r["ticket_key"]).upper(): r
            for r in cached_rows
        }

        if not in_dev:
            log.info(
                "workflow3: no tickets currently in dev statuses %r.",
                status_list,
            )

        # Reset any tracked ticket that is no longer in the dev status.
        for row in self.store.in_dev_rows():
            if str(row["jira_ticket_id"]).upper() not in in_dev:
                self.store.reset_clock(row["jira_ticket_id"])
                log.info("workflow3: %s left dev statuses, clock reset", row["jira_ticket_id"])

        for key, ticket in in_dev.items():
            existing = self.store.get(key)
            if existing and existing.get("tracking_started_at"):
                continue

            estimate = self._original_estimate_hours(key) or 8.0  # default to 8h if unestimated
            fields: dict[str, Any] = {
                "project_key": ticket.get("project_key"),
                "summary": ticket.get("summary"),
                "assignee_name": ticket.get("assignee_name"),
                "assignee_slack_id": self.store.slack_id_for(ticket.get("assignee_name")),
                "is_complete": False,
                "estimate_hours": estimate,
                "tracking_started_at": datetime.now(timezone.utc),
            }
            self.store.upsert(key, **fields)

        return len(in_dev)

    def _original_estimate_hours(self, issue_key: str) -> float | None:
        """Read the dev's Original Estimate from Jira Cloud or DB metadata."""
        import re
        # 1. Check Jira Cloud live
        try:
            from app.jira_client import JiraClient

            jc = JiraClient(self.settings)
            if jc.is_configured():
                issue = jc._request(
                    "GET", f"/rest/api/3/issue/{issue_key}", params={"fields": "timetracking"}
                )
                tt = ((issue or {}).get("fields") or {}).get("timetracking") or {}
                seconds = tt.get("originalEstimateSeconds")
                if seconds:
                    return round(float(seconds) / 3600.0, 2)
        except Exception as exc:
            log.warning("Could not read Original Estimate from Jira for %s: %s", issue_key, exc)

        # 2. Check tickets table payload
        if self.settings.database_url:
            try:
                import psycopg
                from psycopg.rows import dict_row
                with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                    row = conn.execute(
                        "SELECT jira_payload FROM tickets WHERE UPPER(jira_ticket_id) = %s",
                        (issue_key.upper(),),
                    ).fetchone()
                    if row and row.get("jira_payload"):
                        est_str = str(row["jira_payload"].get("estimated_time") or "")
                        # Parse '8h', '16h', '2d', etc.
                        d_m = re.search(r"(\d+(?:\.\d+)?)\s*d", est_str, re.I)
                        h_m = re.search(r"(\d+(?:\.\d+)?)\s*h", est_str, re.I)
                        hours = 0.0
                        if d_m:
                            hours += float(d_m.group(1)) * float(self.settings.effort_working_hours_per_day or 8.0)
                        if h_m:
                            hours += float(h_m.group(1))
                        if hours > 0:
                            return round(hours, 2)
            except Exception as db_exc:
                log.warning("Could not read estimate from DB for %s: %s", issue_key, db_exc)

        return None

    def _fire_alerts(self) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        hours_per_day = self.settings.effort_working_hours_per_day
        group_channel = self._resolve_group_channel_id()

        for row in self.store.in_dev_rows():
            elapsed = elapsed_working_hours(row["tracking_started_at"], now, hours_per_day)
            estimate = float(row.get("estimate_hours") or 8.0)
            pct = percentage_used(elapsed, estimate)
            self.store.upsert(row["jira_ticket_id"], elapsed_hours=elapsed)

            level = alert_level(pct, row)
            if not level:
                continue

            text = self._alert_text(row, elapsed, estimate, pct, level)
            targets = []
            if row.get("assignee_slack_id"):
                targets.append(row["assignee_slack_id"])
            if group_channel and (level == "BREACHED" or not targets):
                targets.append(group_channel)

            delivered = self._post(targets, text)
            self.store.upsert(row["jira_ticket_id"], **flags_for_level(level))
            sent.append(
                {
                    "jira_ticket_id": row["jira_ticket_id"],
                    "level": level,
                    "percentage_used": round(pct, 1),
                    "delivered_to": delivered,
                }
            )
        return sent

    def _alert_text(
        self, row: dict[str, Any], elapsed: float, estimate: float, pct: float, level: str
    ) -> str:
        remaining = max(0.0, estimate - elapsed)
        key = row["jira_ticket_id"]
        summary = row.get("summary") or ""
        headline = {
            "50_USED": f"*{key}* - half of your estimate is used",
            "25_REMAINING": f"*{key}* - 25% of your estimate remains",
            "BREACHED": f"*{key}* - your estimate is fully used",
        }[level]
        body = (
            f"{headline}\n"
            f"- Summary: {summary}\n"
            f"- Your estimate: {estimate:g} h\n"
            f"- Used: {elapsed:g} h ({pct:.0f}%)\n"
            f"- Remaining: {remaining:g} h"
        )
        if level == "BREACHED":
            body += "\n\nThis tracks timeline against your own estimate, not the quality of the work."
        return body

    # ── daily check-in ───────────────────────────────────────────────────────

    def daily_checkin(self) -> dict[str, Any]:
        """Post a consolidated team standup digest to Slack and individual DMs."""
        if not self.settings.effort_tracking_enabled:
            return {"skipped": "EFFORT_TRACKING_ENABLED is false", "messages_sent": 0}

        now = datetime.now(timezone.utc)
        hours_per_day = self.settings.effort_working_hours_per_day
        group_channel = self._resolve_group_channel_id()
        rows = self.store.in_dev_rows()

        if not rows:
            self._reconcile()
            rows = self.store.in_dev_rows()

        if not rows:
            return {"messages_sent": 0, "tickets": 0, "detail": "No tickets currently in dev status"}

        # 1. Post team-level standup digest to the team channel
        lines = [
            "*AI Governor Daily Dev Standup Check-in*",
            f"Active Tickets in Development ({len(rows)}):"
        ]
        for r in rows:
            elapsed = elapsed_working_hours(r["tracking_started_at"], now, hours_per_day)
            estimate = float(r.get("estimate_hours") or 8.0)
            remaining = max(0.0, estimate - elapsed)
            pct = percentage_used(elapsed, estimate)
            assignee = r.get("assignee_name") or "Unassigned"
            lines.append(
                f"- *{r['jira_ticket_id']}*: {r.get('summary') or ''}\n"
                f"  Assignee: *{assignee}* | Estimate: `{estimate:g}h` | Elapsed: `{elapsed:g}h` ({pct:.0f}%) | Remaining: `{remaining:g}h`"
            )
        lines.append("\nPlease reply in thread if there are any blockers or architectural hurdles.")

        sent = 0
        if group_channel and self._post([group_channel], "\n".join(lines)):
            sent += 1
            for r in rows:
                self.store.upsert(r["jira_ticket_id"], last_checkin_at=now)

        # 2. Also message individual developers if known
        by_dev: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            channel = r.get("assignee_slack_id")
            if channel and channel != group_channel:
                elapsed = elapsed_working_hours(r["tracking_started_at"], now, hours_per_day)
                by_dev.setdefault(channel, []).append({**r, "_elapsed": elapsed})

        for channel, dev_rows in by_dev.items():
            dm_lines = ["*Your Daily Effort Check-in*"]
            for r in dev_rows:
                estimate = float(r.get("estimate_hours") or 8.0)
                remaining = max(0.0, estimate - r["_elapsed"])
                dm_lines.append(
                    f"- *{r['jira_ticket_id']}*: {r['_elapsed']:g}h used of {estimate:g}h ({remaining:g}h remaining)"
                )
            dm_lines.append("\nAre you on track?")
            if self._post([channel], "\n".join(dm_lines)):
                sent += 1

        log.info("workflow3 daily check-in: %d messages sent across %d tickets", sent, len(rows))
        return {"messages_sent": sent, "tickets": len(rows), "group_channel": group_channel}

    # ── closure ──────────────────────────────────────────────────────────────

    def _close_completed(self) -> int:
        """Finalise tickets that have reached the closure status."""
        closed_candidates = [
            s.strip() for s in (self.settings.effort_closure_status or "").split(",") if s.strip()
        ]
        closed_candidates.extend(["DONE", "Done", "Closed", "Resolved"])
        closed_list = list({s.lower(): s for s in closed_candidates}.values())

        done = {
            str(r["ticket_key"]).upper()
            for r in self.store.cached_tickets_by_status(closed_list)
        }
        if not done:
            return 0

        now = datetime.now(timezone.utc)
        count = 0
        for row in self.store.in_dev_rows():
            key = str(row["jira_ticket_id"]).upper()
            if key not in done:
                continue
            actual = elapsed_working_hours(
                row["tracking_started_at"], now, self.settings.effort_working_hours_per_day
            )
            self.store.upsert(
                key, actual_hours=actual, closed_at=now, is_complete=True, elapsed_hours=actual
            )
            count += 1
            log.info(
                "workflow3: %s closed - estimate %s h, actual %s h",
                key, row.get("estimate_hours"), actual,
            )
        return count

    def _resolve_group_channel_id(self) -> str:
        """Resolve team/group Slack channel ID from env, DB app_settings, or channelid_table."""
        from app.routers.settings import get_all_settings
        db_conf = get_all_settings(self.settings) if self.settings.database_url else {}
        chan = (
            self.settings.effort_group_channel_id
            or db_conf.get("effort_group_channel_id")
            or self.settings.governor_notify_channel_id
            or db_conf.get("governor_notify_channel_id")
            or self.settings.slack_default_channel_id
            or db_conf.get("slack_default_channel_id")
            or ""
        )
        if chan.strip():
            return chan.strip()
        try:
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT channel_id FROM channelid_table WHERE role IN ('team_channel', 'eng_lead', 'general') ORDER BY CASE WHEN role = 'team_channel' THEN 1 WHEN role = 'eng_lead' THEN 2 ELSE 3 END LIMIT 1"
                ).fetchone()
                if row and row.get("channel_id"):
                    return str(row["channel_id"]).strip()
        except Exception:
            pass
        return ""

    def _post(self, channels: list[str], text: str) -> list[str]:
        from app.slack_client import SlackClient

        client = SlackClient(self.settings)
        delivered: list[str] = []
        for channel in [c for c in channels if c]:
            try:
                res = client.post_message(channel_id=channel, text=text)
                if res.sent:
                    delivered.append(channel)
            except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the rest
                log.warning("workflow3 Slack post to %s failed: %s", channel, exc)
        return delivered
