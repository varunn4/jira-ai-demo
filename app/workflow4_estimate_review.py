"""WF4 — Estimate review, closure feedback and accuracy reporting.

Answers one question per ticket: *is this estimate realistic?* The verdict goes
to the group channel only. The developer is never told their estimate looks off -
they are held to their own number by WF3's clock instead.

Reuses `rft_estimate_analysis` (team-calibrated, content-hash cached) but drops
its single-project scoping so it runs across every project.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.effort_tracking import EffortStore, drift_pct

log = logging.getLogger(__name__)


class Workflow4EstimateReview:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = EffortStore(settings)

    # ── estimate review ──────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not self.settings.effort_tracking_enabled:
            return {"skipped": "EFFORT_TRACKING_ENABLED is false", "flagged": 0}
        if not self.settings.database_url:
            return {"skipped": "DATABASE_URL not configured", "flagged": 0}

        pending = self._tickets_needing_review()
        if not pending:
            return {"reviewed": 0, "flagged": 0, "chased": 0}

        chased = self._chase_missing_estimates(pending)
        flagged = self._review_estimates(pending)

        log.info("workflow4 estimate review: flagged=%d chased=%d", len(flagged), chased)
        return {
            "reviewed": len(pending),
            "flagged": len(flagged),
            "flags": flagged,
            "chased": chased,
        }

    def _tickets_needing_review(self) -> list[dict[str, Any]]:
        """In-dev tracking rows that have not yet been drift-checked."""
        with self.store._connect() as conn:
            return conn.execute(
                """
                SELECT e.*, c.description
                FROM effort_tracking e
                LEFT JOIN jira_ticket_cache c ON UPPER(c.ticket_key) = UPPER(e.jira_ticket_id)
                WHERE e.is_complete = FALSE
                  AND e.drift_flagged_at IS NULL
                ORDER BY e.updated_at DESC
                LIMIT 50
                """
            ).fetchall()

    def _chase_missing_estimates(self, rows: list[dict[str, Any]]) -> int:
        """DM the developer once when a ticket enters dev with no Original Estimate."""
        chased = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            if row.get("estimate_hours") or row.get("estimate_chased_at"):
                continue
            channel = row.get("assignee_slack_id")
            if not channel:
                continue
            text = (
                f"*{row['jira_ticket_id']}* has no Original Estimate in Jira.\n"
                f"- Summary: {row.get('summary') or ''}\n\n"
                f"Please set one so progress can be tracked against your own estimate. "
                f"Nothing is tracked until it is set."
            )
            if self._post([channel], text):
                chased += 1
            # Stamped either way: one chase per ticket, never a nag loop.
            self.store.upsert(row["jira_ticket_id"], estimate_chased_at=now)
        return chased

    def _review_estimates(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = [r for r in rows if r.get("estimate_hours")]
        if not candidates:
            return []

        from app.rft_estimate_analysis import analyze_tickets

        tickets = [
            {
                "key": r["jira_ticket_id"],
                "summary": r.get("summary") or "",
                "description": r.get("description") or "",
                "issue_type": "Task",
                "estimate_seconds": int(float(r["estimate_hours"]) * 3600),
            }
            for r in candidates
        ]

        try:
            analyze_tickets(self.settings, tickets)
        except Exception as exc:  # noqa: BLE001 - never break the run on estimation failure
            log.warning("workflow4: estimate analysis unavailable: %s", exc)
            return []

        threshold = self.settings.effort_drift_threshold_pct
        by_key = {t["key"]: t for t in tickets}
        now = datetime.now(timezone.utc)
        flagged: list[dict[str, Any]] = []

        for row in candidates:
            analysed = by_key.get(row["jira_ticket_id"]) or {}
            predicted = analysed.get("predicted_hours")
            if not predicted:
                continue

            estimate = float(row["estimate_hours"])
            drift = drift_pct(estimate, float(predicted))
            if drift is None:
                continue

            self.store.upsert(
                row["jira_ticket_id"],
                predicted_hours=round(float(predicted), 2),
                drift_pct=drift,
            )

            if abs(drift) <= threshold:
                # Within tolerance: mark reviewed so it is not re-checked every run.
                self.store.upsert(row["jira_ticket_id"], drift_flagged_at=now)
                continue

            direction = "over-estimated" if drift > 0 else "under-estimated"
            text = (
                f"*Estimate review: {row['jira_ticket_id']}*\n"
                f"- Summary: {row.get('summary') or ''}\n"
                f"- Developer: {row.get('assignee_name') or 'unassigned'}\n"
                f"- Their estimate: {estimate:g} h\n"
                f"- Calibrated estimate: {float(predicted):g} h\n"
                f"- Drift: {drift:+.0f}% ({direction})\n\n"
                f"Shared for visibility. The developer has not been notified."
            )

            group_channel = self._resolve_group_channel_id()
            if self.settings.effort_drift_log_only:
                log.info("workflow4 drift (log-only) %s: %+.0f%%", row["jira_ticket_id"], drift)
            elif group_channel:
                self._post([group_channel], text)
            else:
                log.warning("workflow4: EFFORT_GROUP_CHANNEL_ID and channelid_table unset; drift not posted")

            self.store.upsert(row["jira_ticket_id"], drift_flagged_at=now)
            flagged.append(
                {
                    "jira_ticket_id": row["jira_ticket_id"],
                    "estimate_hours": estimate,
                    "predicted_hours": round(float(predicted), 2),
                    "drift_pct": drift,
                }
            )
        return flagged

    # ── accuracy report ──────────────────────────────────────────────────────

    def accuracy_report(self) -> dict[str, Any]:
        """Weekly estimate-accuracy digest built from completed tickets.

        Accuracy is measured against the hours WF3 actually tracked, not Jira's
        logged time, so it does not depend on the team logging work diligently.
        """
        if not self.settings.database_url:
            return {"skipped": "DATABASE_URL not configured"}

        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT assignee_name, jira_ticket_id, estimate_hours, actual_hours
                FROM effort_tracking
                WHERE is_complete = TRUE
                  AND actual_hours IS NOT NULL
                  AND estimate_hours IS NOT NULL AND estimate_hours > 0
                  AND closed_at > NOW() - INTERVAL '30 days'
                ORDER BY closed_at DESC
                """
            ).fetchall()

        if not rows:
            return {"tickets": 0, "sent": False}

        per_dev: dict[str, list[float]] = {}
        drifts: list[float] = []
        breached = 0
        for r in rows:
            d = drift_pct(float(r["actual_hours"]), float(r["estimate_hours"]))
            if d is None:
                continue
            drifts.append(d)
            per_dev.setdefault(r.get("assignee_name") or "unassigned", []).append(d)
            if float(r["actual_hours"]) > float(r["estimate_hours"]):
                breached += 1

        if not drifts:
            return {"tickets": len(rows), "sent": False}

        def median(values: list[float]) -> float:
            s = sorted(values)
            mid = len(s) // 2
            return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

        lines = [
            "*Estimate accuracy - last 30 days*",
            f"- Tickets completed: {len(rows)}",
            f"- Median drift (actual vs estimate): {median(drifts):+.0f}%",
            f"- Ran over estimate: {breached} of {len(rows)}",
            "",
            "*Per developer (median drift):*",
        ]
        for dev, values in sorted(per_dev.items(), key=lambda kv: -abs(median(kv[1]))):
            lines.append(f"- {dev}: {median(values):+.0f}% over {len(values)} ticket(s)")
        lines.append("\nPositive means the work took longer than estimated.")

        text = "\n".join(lines)
        group_channel = self._resolve_group_channel_id()
        sent = bool(
            group_channel
            and self._post([group_channel], text)
        )
        return {"tickets": len(rows), "median_drift_pct": round(median(drifts), 1), "sent": sent}

    # ── slack ────────────────────────────────────────────────────────────────

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
                if client.post_message(channel_id=channel, text=text).sent:
                    delivered.append(channel)
            except Exception as exc:  # noqa: BLE001
                log.warning("workflow4 Slack post to %s failed: %s", channel, exc)
        return delivered
