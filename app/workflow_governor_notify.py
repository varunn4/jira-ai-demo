"""AI Governor scheduled notification.

A deliberately small workflow: a cron-triggered n8n workflow calls
``POST /workflow/governor-notify``; this builds a single Slack notification for
the configured channel and returns it in the shared ``AlertBatchResponse`` shape
(``{"alerts": [...], "alerts_sent": N}``) that the n8n Split-Alerts → Slack
nodes already fan out. No database or Jira access — the message text comes from
``GOVERNOR_NOTIFY_MESSAGE`` (with ``{date}`` substituted) and the destination
from ``GOVERNOR_NOTIFY_CHANNEL_ID``.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.config import Settings

LOGGER = logging.getLogger(__name__)


class GovernorNotifier:
    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    def build_message(self) -> str:
        template = self.settings.governor_notify_message or ""
        today = date.today().strftime("%A, %d %b %Y")
        try:
            return template.format(date=today)
        except (KeyError, IndexError, ValueError):
            # A stray brace in a custom message shouldn't break the send.
            return template

    def notify(self) -> dict[str, Any]:
        channel_id = (self.settings.governor_notify_channel_id or "").strip()
        if not channel_id:
            LOGGER.warning(
                "governor-notify: GOVERNOR_NOTIFY_CHANNEL_ID is not set; no alert produced"
            )
            return {"alerts": [], "alerts_sent": 0}

        message = self.build_message()
        LOGGER.info(
            "governor-notify: prepared message for channel=%s text_chars=%d",
            channel_id,
            len(message),
        )
        return {
            "alerts": [{"channel_id": channel_id, "message": message}],
            "alerts_sent": 1,
        }
