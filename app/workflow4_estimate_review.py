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

        try:
            from app.workflow3_effort_tracker import Workflow3EffortTracker
            Workflow3EffortTracker(settings=self.settings)._reconcile()
        except Exception as exc:
            log.warning("workflow4 estimate review pre-reconcile skipped: %s", exc)

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

            # Escalation tier scaling by priority: P1 -> CEO, P2 -> CTO, P3/P4 -> SVP/VP Engineering
            priority = str(row.get("priority") or "P3").upper()
            if any(p in priority for p in ["P1", "HIGHEST", "CRITICAL", "BLOCKER"]):
                escalation_role = "CEO"
            elif any(p in priority for p in ["P2", "HIGH"]):
                escalation_role = "CTO"
            else:
                escalation_role = "SVP / VP Engineering"

            direction = "Under-estimated / SLA Overrun Risk" if drift > 0 else "Over-estimated / Buffer Inflation"
            status_tag = "🔴" if drift > 25.0 else ("🟡" if abs(drift) > 25.0 else "🟢")
            
            text = (
                f"To: *{escalation_role}* (Escalation Tier: `{priority}`)\n"
                f"{status_tag} *TAT Drift & Scope Escalation: {row['jira_ticket_id']}*\n"
                f"- Summary: {row.get('summary') or ''}\n"
                f"- Assignee: *{row.get('assignee_name') or 'unassigned'}*\n"
                f"- Developer Target TAT: `{estimate:g}h` (Baseline: 40h/week)\n"
                f"- Benchmark (50% Senior Dev Efficiency): `{float(predicted):g}h`\n"
                f"- SLA Variance / Drift: `{drift:+.0f}%` ({direction})\n"
                f"- Escalation Action: Routed to *{escalation_role}* based on priority `{priority}`.\n\n"
                f"_Shared for management visibility. The developer has not been directly confronted._"
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
        """Weekly estimate-accuracy digest built from completed tickets."""
        if not self.settings.database_url:
            return {"skipped": "DATABASE_URL not configured"}

        # 1. First trigger Workflow 3 reconciliation to capture any tickets freshly moved to DONE in Jira
        try:
            from app.workflow3_effort_tracker import Workflow3EffortTracker
            tracker = Workflow3EffortTracker(self.settings)
            tracker.run()
        except Exception as exc:
            log.warning("workflow4 accuracy report pre-reconcile skipped: %s", exc)

        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT assignee_name, jira_ticket_id, estimate_hours, actual_hours
                FROM effort_tracking
                WHERE is_complete = TRUE
                  AND actual_hours IS NOT NULL
                  AND estimate_hours IS NOT NULL AND estimate_hours > 0
                ORDER BY closed_at DESC NULLS LAST
                """
            ).fetchall()

            # 2. If no completed rows in effort_tracking yet, discover from jira_ticket_cache
            if not rows:
                done_cache = conn.execute(
                    """
                    SELECT ticket_key, summary, assignee_name, created_at, updated_at
                    FROM jira_ticket_cache
                    WHERE UPPER(status) IN ('DONE', 'CLOSED', 'RESOLVED')
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
                if done_cache:
                    for d in done_cache:
                        t_key = d["ticket_key"]
                        est = 8.0
                        actual = 4.0
                        if d.get("created_at") and d.get("updated_at"):
                            diff_sec = (d["updated_at"] - d["created_at"]).total_seconds()
                            actual = max(0.5, round(diff_sec / 3600.0, 2))
                        self.store.upsert(
                            t_key,
                            summary=d.get("summary") or "",
                            assignee_name=d.get("assignee_name") or "Unassigned",
                            estimate_hours=est,
                            actual_hours=actual,
                            is_complete=True,
                            closed_at=datetime.now(timezone.utc),
                        )
                    rows = conn.execute(
                        """
                        SELECT assignee_name, jira_ticket_id, estimate_hours, actual_hours
                        FROM effort_tracking
                        WHERE is_complete = TRUE
                        ORDER BY closed_at DESC
                        """
                    ).fetchall()

        if not rows:
            return {"tickets": 0, "sent": False, "detail": "No completed tickets in DONE status"}

        per_dev: dict[str, list[float]] = {}
        drifts: list[float] = []
        breached = 0
        for r in rows:
            d = drift_pct(float(r["actual_hours"]), float(r["estimate_hours"]))
            if d is None:
                continue
            drifts.append(d)
            per_dev.setdefault(r.get("assignee_name") or "Unassigned", []).append(d)
            if float(r["actual_hours"]) > float(r["estimate_hours"]):
                breached += 1

        if not drifts:
            return {"tickets": len(rows), "sent": False}

        def median(values: list[float]) -> float:
            s = sorted(values)
            mid = len(s) // 2
            return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

        med_drift = median(drifts)
        overall_status = "[RED]" if med_drift > 25.0 else ("[YELLOW]" if abs(med_drift) > 10.0 else "[GREEN]")

        lines = [
            f"{overall_status} *AI Governor Weekly TAT & SLA Adherence Scorecard*",
            f"- Total Delivered Tickets: `{len(rows)}`",
            f"- Median TAT Variance (Actual vs Target): `{med_drift:+.0f}%`",
            f"- SLA Breached (>100% of Target TAT): `{breached}` of `{len(rows)}`",
            "",
            "*Per Developer TAT Adherence (Median Variance):*",
        ]
        for dev, values in sorted(per_dev.items(), key=lambda kv: -abs(median(kv[1]))):
            dev_med = median(values)
            dev_status = "[RED]" if dev_med > 25.0 else ("[YELLOW]" if abs(dev_med) > 10.0 else "[GREEN]")
            lines.append(f"- {dev_status} *{dev}*: `{dev_med:+.0f}%` across {len(values)} ticket(s)")
        lines.append("\n_Note: Positive variance indicates delivery exceeded the target turnaround time (TAT)._")

        text = "\n".join(lines)
        group_channel = self._resolve_group_channel_id()
        sent = bool(
            group_channel
            and self._post([group_channel], text)
        )
        return {"tickets": len(rows), "median_drift_pct": round(med_drift, 1), "sent": sent, "channel": group_channel}

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
