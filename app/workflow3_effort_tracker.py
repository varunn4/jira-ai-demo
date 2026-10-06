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

        # 1. Direct Live Jira API query as ultimate source of truth
        in_dev: dict[str, dict[str, Any]] = {}
        try:
            from app.jira_client import JiraClient
            jc = JiraClient(self.settings)
            if jc.is_configured():
                jql = 'statusCategory = "In Progress" OR status in ("In Progress - Dev", "In Progress", "In Dev")'
                res = jc._request(
                    "GET",
                    "/rest/api/3/search/jql",
                    params={"jql": jql, "maxResults": 100, "fields": "summary,status,assignee,timetracking,project"},
                )
                if not res or not res.get("issues"):
                    res = jc._request(
                        "GET",
                        "/rest/api/3/search",
                        params={"jql": jql, "maxResults": 100, "fields": "summary,status,assignee,timetracking,project"},
                    )
                issues = (res or {}).get("issues", [])
                for issue in issues:
                    k = str(issue.get("key") or "").upper()
                    if not k:
                        continue
                    f = issue.get("fields", {}) or {}
                    st_name = ((f.get("status") or {}).get("name")) or ""
                    proj_k = ((f.get("project") or {}).get("key")) or (k.split("-")[0] if "-" in k else "")
                    ass_name = ((f.get("assignee") or {}).get("displayName")) or "Unassigned"
                    tt = (f.get("timetracking") or {})
                    est_sec = tt.get("originalEstimateSeconds")
                    est_h = round(float(est_sec) / 3600.0, 2) if est_sec else None
                    in_dev[k] = {
                        "ticket_key": k,
                        "project_key": proj_k,
                        "summary": f.get("summary") or "",
                        "status": st_name,
                        "assignee_name": ass_name,
                        "estimate_hours": est_h,
                    }
                log.info("workflow3: fetched %d live in-dev ticket(s) directly from Jira Cloud JQL", len(in_dev))
        except Exception as jc_exc:
            log.warning("workflow3 live JQL fetch error: %s", jc_exc)

        # 2. Fallback to cache if live JQL didn't populate
        if not in_dev:
            cached_rows = self.store.cached_tickets_by_status(status_list)
            in_dev = {
                str(r["ticket_key"]).upper(): r
                for r in cached_rows
            }

        # 3. Clean up non-existent / stale records dynamically from database
        try:
            with self.store._connect() as conn:
                if in_dev:
                    conn.execute(
                        "UPDATE effort_tracking SET tracking_started_at = NULL WHERE UPPER(jira_ticket_id) != ALL(%s) AND tracking_started_at IS NOT NULL",
                        (list(in_dev.keys()),),
                    )
                conn.execute(
                    """
                    DELETE FROM effort_tracking
                    WHERE UPPER(jira_ticket_id) NOT IN (SELECT UPPER(ticket_key) FROM jira_ticket_cache)
                    """
                )
                conn.commit()
        except Exception as cln_exc:
            log.warning("workflow3 cleanup stale records skipped: %s", cln_exc)

        # Reset any tracked ticket that is no longer in active in_dev
        for row in self.store.in_dev_rows():
            if str(row["jira_ticket_id"]).upper() not in in_dev:
                self.store.reset_clock(row["jira_ticket_id"])
                log.info("workflow3: %s left dev statuses, clock reset", row["jira_ticket_id"])

        for key, ticket in in_dev.items():
            existing = self.store.get(key)
            if existing and existing.get("tracking_started_at"):
                continue

            dev_started_at = self._jira_dev_transition_time(key) or datetime.now(timezone.utc)
            estimate = ticket.get("estimate_hours") or self._original_estimate_hours(key) or 8.0
            fields: dict[str, Any] = {
                "project_key": ticket.get("project_key"),
                "summary": ticket.get("summary"),
                "assignee_name": ticket.get("assignee_name"),
                "assignee_slack_id": self.store.slack_id_for(ticket.get("assignee_name")),
                "is_complete": False,
                "estimate_hours": estimate,
                "tracking_started_at": dev_started_at,
            }
            self.store.upsert(key, **fields)

        return len(in_dev)

    def _jira_dev_transition_time(self, issue_key: str) -> datetime | None:
        """Fetch the exact timestamp when the ticket was moved to In-Dev in Jira."""
        try:
            from app.jira_client import JiraClient

            jc = JiraClient(self.settings)
            if jc.is_configured():
                data = jc._request(
                    "GET",
                    f"/rest/api/3/issue/{issue_key}",
                    params={"expand": "changelog", "fields": "updated,created"},
                )
                changelog = (data or {}).get("changelog", {})
                histories = changelog.get("histories", [])
                for h in reversed(histories):
                    for item in h.get("items", []):
                        if item.get("field") == "status":
                            to_str = str(item.get("toString") or "").lower()
                            if any(w in to_str for w in ["dev", "in progress", "development", "active"]):
                                created_str = h.get("created")
                                if created_str:
                                    return datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                # Fallback to issue updated field
                f = (data or {}).get("fields", {}) or {}
                if f.get("updated"):
                    return datetime.fromisoformat(str(f["updated"]).replace("Z", "+00:00"))
        except Exception as exc:
            log.debug("Could not fetch changelog transition time for %s: %s", issue_key, exc)
        return None

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
        status_tag = "🔴" if pct >= 100.0 else ("🟡" if pct >= 75.0 else "🟢")
        
        headline = {
            "50_USED": f"{status_tag} *{key}* - 50% of Target TAT utilized",
            "25_REMAINING": f"{status_tag} *{key}* - 25% of Target TAT remaining (75% utilized)",
            "BREACHED": f"{status_tag} *{key}* - Target TAT SLA Breached (>100% elapsed)",
        }[level]
        
        body = (
            f"{headline}\n"
            f"- Summary: {summary}\n"
            f"- Target TAT: {estimate:g} h\n"
            f"- Elapsed Time: {elapsed:g} h ({pct:.0f}% of TAT)\n"
            f"- Remaining TAT: {remaining:g} h"
        )
        if level == "BREACHED":
            breached_by = max(0.0, elapsed - estimate)
            body += f"\n- SLA Overrun: +{breached_by:g} h over Target TAT"
            body += "\n\nNote: This tracks SLA adherence against the agreed Target TAT, not code quality."
        return body

    # ── 9:00 AM: Individual Developer Check-in ────────────────────────────────

    def daily_dev_checkin(self) -> dict[str, Any]:
        """9:00 AM: Send each developer an individual status update on their active tickets."""
        if not self.settings.effort_tracking_enabled:
            return {"skipped": "EFFORT_TRACKING_ENABLED is false", "messages_sent": 0}

        now = datetime.now(timezone.utc)
        hours_per_day = self.settings.effort_working_hours_per_day
        self._reconcile()
        rows = self.store.in_dev_rows()

        if not rows:
            return {"messages_sent": 0, "tickets": 0, "detail": "No tickets currently in dev status"}

        group_channel = self._resolve_group_channel_id()
        by_dev: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            channel = r.get("assignee_slack_id") or group_channel
            if channel:
                elapsed = elapsed_working_hours(r["tracking_started_at"], now, hours_per_day)
                by_dev.setdefault(channel, []).append({**r, "_elapsed": elapsed})

        sent = 0
        for channel, dev_rows in by_dev.items():
            dev_name = dev_rows[0].get("assignee_name") or "Developer"
            dm_lines = [
                f"To: *{dev_name}*",
                "*Daily Dev TAT & Ticket Status Update (9:00 AM IST)*"
            ]
            for r in dev_rows:
                estimate = float(r.get("estimate_hours") or 8.0)
                remaining = max(0.0, estimate - r["_elapsed"])
                pct = percentage_used(r["_elapsed"], estimate)
                status_tag = "🔴" if pct >= 100.0 else ("🟡" if pct >= 75.0 else "🟢")
                dm_lines.append(
                    f"- {status_tag} *{r['jira_ticket_id']}*: {r.get('summary') or ''}\n"
                    f"  Elapsed: `{r['_elapsed']:g}h` of `{estimate:g}h` Target TAT ({remaining:g}h remaining, {pct:.0f}% used)"
                )
            dm_lines.append("\nAre you on track to deliver within the Target TAT today, or is an SLA extension required?")
            if self._post([channel], "\n".join(dm_lines)):
                sent += 1
                for r in dev_rows:
                    self.store.upsert(r["jira_ticket_id"], last_checkin_at=now)

        log.info("workflow3 9AM dev check-in: %d messages sent across %d tickets", sent, len(rows))
        return {"messages_sent": sent, "tickets": len(rows), "scope": "developer_9am_dm"}

    # ── 3:00 PM: Combined TL & Management Summary ───────────────────────────

    def daily_tl_summary(self) -> dict[str, Any]:
        """3:00 PM: Send combined developer status digest to Team Lead & Management."""
        if not self.settings.effort_tracking_enabled:
            return {"skipped": "EFFORT_TRACKING_ENABLED is false", "messages_sent": 0}

        now = datetime.now(timezone.utc)
        hours_per_day = self.settings.effort_working_hours_per_day
        group_channel = self._resolve_group_channel_id()
        self._reconcile()
        rows = self.store.in_dev_rows()

        if not rows:
            return {"messages_sent": 0, "tickets": 0, "detail": "No tickets currently in dev status"}

        lines = [
            "To: Team Lead & Engineering Management",
            "*AI Governor Team Dev Status & TAT Adherence (3:00 PM IST)*",
            f"Active In-Dev Tickets across Developers ({len(rows)}):"
        ]
        for r in rows:
            elapsed = elapsed_working_hours(r["tracking_started_at"], now, hours_per_day)
            estimate = float(r.get("estimate_hours") or 8.0)
            remaining = max(0.0, estimate - elapsed)
            pct = percentage_used(elapsed, estimate)
            assignee = r.get("assignee_name") or "Unassigned"
            status_tag = "🔴" if pct >= 100.0 else ("🟡" if pct >= 75.0 else "🟢")
            lines.append(
                f"- {status_tag} *{r['jira_ticket_id']}*: {r.get('summary') or ''}\n"
                f"  Assignee: *{assignee}* | Target TAT: `{estimate:g}h` | Elapsed: `{elapsed:g}h` ({pct:.0f}% of TAT) | Remaining TAT: `{remaining:g}h`"
            )
        lines.append("\nPlease reply in thread if there are any team blockers or if management intervention is required.")

        sent = 0
        if group_channel and self._post([group_channel], "\n".join(lines)):
            sent += 1
            for r in rows:
                self.store.upsert(r["jira_ticket_id"], last_checkin_at=now)

        log.info("workflow3 3PM TL summary: %d messages sent across %d tickets", sent, len(rows))
        return {"messages_sent": sent, "tickets": len(rows), "group_channel": group_channel, "scope": "tl_3pm_summary"}

    def daily_checkin(self) -> dict[str, Any]:
        """Consolidated daily check-in (triggers both 9AM dev and 3PM TL formats)."""
        dev_res = self.daily_dev_checkin()
        tl_res = self.daily_tl_summary()
        return {
            "messages_sent": dev_res.get("messages_sent", 0) + tl_res.get("messages_sent", 0),
            "tickets": dev_res.get("tickets", 0),
            "dev_checkin": dev_res,
            "tl_summary": tl_res,
        }

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
