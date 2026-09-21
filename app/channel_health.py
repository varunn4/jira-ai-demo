"""Slack channel health check for the admin panel.

WorkFlow 4 (and the other Slack digests) fan out to channel IDs drawn from two
places — the ``channelid_table`` role map and each ticket's
``assignee_slack_id`` in ``due_date_tracking`` — plus a few channels pinned in
the environment. When the bot was never invited to one of those channels/DMs,
``chat.postMessage`` returns ``not_in_channel`` and the whole n8n Slack node
errors, without telling you *which* ID is at fault.

This module discovers every channel ID the system might post to, annotates each
with the person/role behind it (from ``channelid_table``), and — on request —
sends a clearly-marked probe message to each one. Probe outcomes are persisted
in ``channel_health_status`` so the last-known health survives a page reload or
backend restart; the admin sees the previous result immediately and only re-runs
the sweep when they want a fresh check.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.slack_client import SlackClient

log = logging.getLogger(__name__)

DEFAULT_PROBE_MESSAGE = (
    ":wave: *Channel health check* — automated test from the Jira-AI admin panel "
    "to confirm the bot can post here. You can ignore this message."
)


class ChannelHealthError(RuntimeError):
    """Raised when the channel inventory cannot be loaded."""


def _connect(settings: Settings):
    import psycopg2
    from psycopg2.extras import RealDictCursor

    return psycopg2.connect(settings.database_url), RealDictCursor


def ensure_channel_health_schema(settings: Settings) -> None:
    """Create the persisted-status table if it does not exist."""
    if not settings.database_url:
        return
    conn, _ = _connect(settings)
    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_health_status (
                        channel_id      TEXT PRIMARY KEY,
                        ok              BOOLEAN NOT NULL,
                        error           TEXT,
                        message_ts      TEXT,
                        last_checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
    finally:
        conn.close()


class ChannelHealthChecker:
    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self.slack = SlackClient(settings)

    # ── Discovery ────────────────────────────────────────────────────────────
    def discover(self) -> list[dict[str, Any]]:
        """Return every known channel ID annotated with sources, names and the
        last persisted probe result.

        Each entry::

            {
              "channel_id": str,
              "sources": [str, ...],          # role:developer, assignee_slack_id, env:...
              "names": [str, ...],            # slack_user_name(s) behind the channel
              "emails": [str, ...],
              "last_status": "ok"|"failed"|None,
              "last_error": str|None,
              "last_checked_at": str|None,     # ISO timestamp
            }
        """
        sources: dict[str, list[str]] = {}
        names: dict[str, list[str]] = {}
        emails: dict[str, list[str]] = {}

        def add(channel_id: Any, label: str, name: Any = None, email: Any = None) -> None:
            cid = str(channel_id or "").strip()
            if not cid:
                return
            src = sources.setdefault(cid, [])
            if label not in src:
                src.append(label)
            nm = str(name or "").strip()
            if nm:
                bucket = names.setdefault(cid, [])
                if nm not in bucket:
                    bucket.append(nm)
            em = str(email or "").strip()
            if em:
                bucket = emails.setdefault(cid, [])
                if em not in bucket:
                    bucket.append(em)

        # Config-pinned channels (governor digest, WF7 report, default).
        add(self.settings.governor_notify_channel_id, "env:GOVERNOR_NOTIFY_CHANNEL_ID")
        add(self.settings.rft_estimate_channel_id, "env:RFT_ESTIMATE_CHANNEL_ID")
        add(self.settings.slack_default_channel_id, "env:SLACK_DEFAULT_CHANNEL_ID")

        # Database-backed channels (role map + per-assignee DM IDs).
        for row in self._load_db_channels():
            add(row["channel_id"], row["source"], row.get("name"), row.get("email"))

        status = self._load_status()

        result: list[dict[str, Any]] = []
        for cid in sorted(sources):
            st = status.get(cid, {})
            result.append(
                {
                    "channel_id": cid,
                    "sources": sources[cid],
                    "names": names.get(cid, []),
                    "emails": emails.get(cid, []),
                    "last_status": st.get("status"),
                    "last_error": st.get("error"),
                    "last_checked_at": st.get("last_checked_at"),
                }
            )
        return result

    def _load_db_channels(self) -> list[dict[str, Any]]:
        if not self.settings.database_url:
            log.warning("DATABASE_URL not set; skipping DB channel discovery")
            return []
        try:
            conn, dict_cursor = _connect(self.settings)
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise ChannelHealthError("The 'psycopg2-binary' package is required") from exc

        found: list[dict[str, Any]] = []
        try:
            with conn.cursor(cursor_factory=dict_cursor) as cursor:
                # Role map — grab every row (with the person behind it) so newly
                # added roles/people are covered automatically.
                try:
                    cursor.execute(
                        "SELECT role, channel_id, slack_user_name, email_id "
                        "FROM channelid_table"
                    )
                    for row in cursor.fetchall():
                        role = str(row.get("role") or "?")
                        found.append(
                            {
                                "channel_id": row.get("channel_id"),
                                "source": f"role:{role}",
                                "name": row.get("slack_user_name"),
                                "email": row.get("email_id"),
                            }
                        )
                except Exception as exc:  # noqa: BLE001 - table may be absent
                    log.warning("channelid_table read failed: %s", exc)

                # Per-assignee DM IDs used by the due-date digests.
                try:
                    cursor.execute(
                        "SELECT DISTINCT assignee_slack_id FROM due_date_tracking "
                        "WHERE assignee_slack_id IS NOT NULL AND assignee_slack_id <> ''"
                    )
                    for row in cursor.fetchall():
                        found.append(
                            {
                                "channel_id": row.get("assignee_slack_id"),
                                "source": "assignee_slack_id",
                            }
                        )
                except Exception as exc:  # noqa: BLE001 - table may be absent
                    log.warning("due_date_tracking read failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - connection-level failure
            raise ChannelHealthError(f"channel discovery DB error: {exc}") from exc
        finally:
            conn.close()
        return found

    def _load_status(self) -> dict[str, dict[str, Any]]:
        """Read the last persisted probe result for every channel."""
        if not self.settings.database_url:
            return {}
        try:
            ensure_channel_health_schema(self.settings)
            conn, dict_cursor = _connect(self.settings)
        except Exception as exc:  # noqa: BLE001 - status is best-effort
            log.warning("channel_health_status read skipped: %s", exc)
            return {}

        out: dict[str, dict[str, Any]] = {}
        try:
            with conn.cursor(cursor_factory=dict_cursor) as cursor:
                cursor.execute(
                    "SELECT channel_id, ok, error, last_checked_at "
                    "FROM channel_health_status"
                )
                for row in cursor.fetchall():
                    checked = row.get("last_checked_at")
                    out[row["channel_id"]] = {
                        "status": "ok" if row.get("ok") else "failed",
                        "error": row.get("error"),
                        "last_checked_at": checked.isoformat() if checked else None,
                    }
        except Exception as exc:  # noqa: BLE001
            log.warning("channel_health_status query failed: %s", exc)
        finally:
            conn.close()
        return out

    def _persist(self, results: list[dict[str, Any]]) -> None:
        """Upsert probe outcomes so they survive reloads/restarts."""
        if not self.settings.database_url or not results:
            return
        try:
            ensure_channel_health_schema(self.settings)
            conn, _ = _connect(self.settings)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("channel_health_status write skipped: %s", exc)
            return
        try:
            with conn:
                with conn.cursor() as cursor:
                    for r in results:
                        cursor.execute(
                            """
                            INSERT INTO channel_health_status
                                (channel_id, ok, error, message_ts, last_checked_at)
                            VALUES (%s, %s, %s, %s, NOW())
                            ON CONFLICT (channel_id) DO UPDATE SET
                                ok = EXCLUDED.ok,
                                error = EXCLUDED.error,
                                message_ts = EXCLUDED.message_ts,
                                last_checked_at = NOW()
                            """,
                            (r["channel_id"], r["ok"], r["error"], r.get("message_ts")),
                        )
        except Exception as exc:  # noqa: BLE001
            log.warning("channel_health_status upsert failed: %s", exc)
        finally:
            conn.close()

    # ── Probe ────────────────────────────────────────────────────────────────
    def check(
        self,
        *,
        channel_ids: list[str] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Send a probe to each channel, persist the outcome, and flag failures.

        If ``channel_ids`` is given, only those are probed (still annotated with
        their discovered sources/names where known); otherwise the full
        inventory is swept.
        """
        inventory = self.discover()
        by_id = {item["channel_id"]: item for item in inventory}

        if channel_ids:
            targets = [str(c).strip() for c in channel_ids if str(c).strip()]
        else:
            targets = [item["channel_id"] for item in inventory]

        text = (message or DEFAULT_PROBE_MESSAGE).strip() or DEFAULT_PROBE_MESSAGE
        configured = bool(self.settings.slack_bot_token)

        results: list[dict[str, Any]] = []
        for cid in targets:
            meta = by_id.get(cid, {})
            probe = self.slack.probe_channel(channel_id=cid, text=text)
            results.append(
                {
                    "channel_id": cid,
                    "sources": meta.get("sources", ["adhoc"]),
                    "names": meta.get("names", []),
                    "emails": meta.get("emails", []),
                    "ok": probe.ok,
                    "error": probe.error,
                    "message_ts": probe.message_ts,
                }
            )

        self._persist(results)

        failed = [r for r in results if not r["ok"]]
        return {
            "configured": configured,
            "checked": len(results),
            "ok_count": len(results) - len(failed),
            "failed_count": len(failed),
            "probe_message": text,
            "results": results,
        }
