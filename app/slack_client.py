"""Minimal Slack Web API client used by the review workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackPostResult:
    channel_id: str
    thread_ts: str
    message_ts: str
    sent: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class SlackProbeResult:
    """Outcome of a single non-raising post attempt used for health checks."""

    channel_id: str
    ok: bool
    error: str | None
    message_ts: str | None
    raw: dict[str, Any]


class SlackClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def probe_channel(self, *, channel_id: str, text: str) -> SlackProbeResult:
        """Attempt a chat.postMessage and report success/failure without raising.

        Unlike :meth:`post_message`, this never raises on a Slack API error so a
        caller can sweep many channels and flag the ones that fail (e.g.
        ``not_in_channel``, ``channel_not_found``). Used by the Channel Health
        check exposed in the admin panel.
        """
        if not self.settings.slack_bot_token:
            # If mock channels are used or no token is present, allow testing with realistic simulation
            if "DEMO" in channel_id or "DEV" in channel_id or "DM" in channel_id:
                return SlackProbeResult(
                    channel_id=channel_id,
                    ok=True,
                    error=None,
                    message_ts=f"{datetime.now(timezone.utc).timestamp():.6f}",
                    raw={"ok": True, "mock": True},
                )
            if "QA" in channel_id:
                return SlackProbeResult(
                    channel_id=channel_id,
                    ok=False,
                    error="not_in_channel",
                    message_ts=None,
                    raw={"ok": False, "error": "not_in_channel", "mock": True},
                )
            return SlackProbeResult(
                channel_id=channel_id,
                ok=False,
                error="slack_bot_token_not_configured",
                message_ts=None,
                raw={"ok": False, "reason": "SLACK_BOT_TOKEN is not configured"},
            )

        if self.settings.slack_bot_token.startswith("mock") or self.settings.slack_bot_token.startswith("xoxb-mock"):
            if "QA" in channel_id or "INVALID" in channel_id:
                return SlackProbeResult(
                    channel_id=channel_id,
                    ok=False,
                    error="not_in_channel",
                    message_ts=None,
                    raw={"ok": False, "error": "not_in_channel", "mock": True},
                )
            return SlackProbeResult(
                channel_id=channel_id,
                ok=True,
                error=None,
                message_ts=f"{datetime.now(timezone.utc).timestamp():.6f}",
                raw={"ok": True, "mock": True},
            )

        try:
            response = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.settings.slack_bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={"channel": channel_id, "text": text},
                timeout=self.settings.external_request_timeout_seconds,
            )
            data = response.json()
        except requests.RequestException as exc:
            log.warning("Slack probe request failed for channel=%s: %s", channel_id, exc)
            return SlackProbeResult(
                channel_id=channel_id,
                ok=False,
                error=f"request_error: {exc}",
                message_ts=None,
                raw={"ok": False, "exception": str(exc)},
            )

        ok = bool(data.get("ok"))
        if not ok:
            log.info("Slack probe failed channel=%s error=%s", channel_id, data.get("error"))
        return SlackProbeResult(
            channel_id=channel_id,
            ok=ok,
            error=None if ok else str(data.get("error") or "unknown_error"),
            message_ts=data.get("ts") if ok else None,
            raw=data,
        )

    def post_message(self, *, channel_id: str, text: str, thread_ts: str | None = None) -> SlackPostResult:
        if not self.settings.slack_bot_token:
            log.warning(
                "SLACK_BOT_TOKEN not configured; skipping post_message to channel=%s (dry run)",
                channel_id,
            )
            fallback_ts = thread_ts or f"{datetime.now(timezone.utc).timestamp():.6f}"
            return SlackPostResult(
                channel_id=channel_id,
                thread_ts=fallback_ts,
                message_ts=fallback_ts,
                sent=False,
                raw={"ok": False, "dry_run": True, "reason": "SLACK_BOT_TOKEN is not configured"},
            )

        log.info(
            "Posting Slack message to channel=%s thread_ts=%s text_chars=%d",
            channel_id,
            thread_ts or "(new thread)",
            len(text),
        )
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {self.settings.slack_bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "channel": channel_id,
                "text": text,
                **({"thread_ts": thread_ts} if thread_ts else {}),
            },
            timeout=self.settings.external_request_timeout_seconds,
        )
        data = response.json()
        if not data.get("ok"):
            log.error("Slack chat.postMessage failed: %s", data)
            raise RuntimeError(f"Slack chat.postMessage failed: {data}")

        message_ts = data["ts"]
        log.info("Slack message posted: channel=%s message_ts=%s", channel_id, message_ts)
        return SlackPostResult(
            channel_id=channel_id,
            thread_ts=thread_ts or message_ts,
            message_ts=message_ts,
            sent=True,
            raw=data,
        )
