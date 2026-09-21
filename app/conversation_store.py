"""Postgres persistence for Slack thread review conversations."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


class ConversationStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConversationRecord:
    id: int
    slack_thread_ts: str
    slack_channel_id: str
    jira_issue_key: str
    original_ticket_data: dict[str, Any]
    previous_review: dict[str, Any]
    status: str
    messages: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class PostgresConversationStore:
    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise ConversationStoreError("DATABASE_URL is required for Slack/Jira conversation workflows")

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ConversationStoreError("Install psycopg[binary] to use Postgres conversation storage") from exc

        self.settings = settings
        self._psycopg = psycopg
        self._dict_row = dict_row

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jira_slack_conversations (
                    id BIGSERIAL PRIMARY KEY,
                    slack_thread_ts TEXT NOT NULL UNIQUE,
                    slack_channel_id TEXT NOT NULL,
                    jira_issue_key TEXT NOT NULL,
                    original_ticket_data JSONB NOT NULL,
                    previous_review JSONB NOT NULL,
                    status TEXT NOT NULL,
                    messages JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jira_slack_conversations_issue_key
                ON jira_slack_conversations (jira_issue_key)
                """
            )

    def upsert_initial(
        self,
        *,
        slack_thread_ts: str,
        slack_channel_id: str,
        jira_issue_key: str,
        original_ticket_data: dict[str, Any],
        previous_review: dict[str, Any],
        status: str,
        bot_message: str,
    ) -> ConversationRecord:
        log.info(
            "Upserting conversation for issue=%s thread=%s status=%s",
            jira_issue_key, slack_thread_ts, status,
        )
        self.init_schema()
        messages = [
            {
                "role": "assistant",
                "text": bot_message,
                "ts": slack_thread_ts,
                "created_at": self._now_iso(),
            }
        ]
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO jira_slack_conversations (
                    slack_thread_ts,
                    slack_channel_id,
                    jira_issue_key,
                    original_ticket_data,
                    previous_review,
                    status,
                    messages
                )
                VALUES (
                    %(slack_thread_ts)s,
                    %(slack_channel_id)s,
                    %(jira_issue_key)s,
                    %(original_ticket_data)s::jsonb,
                    %(previous_review)s::jsonb,
                    %(status)s,
                    %(messages)s::jsonb
                )
                ON CONFLICT (slack_thread_ts)
                DO UPDATE SET
                    slack_channel_id = EXCLUDED.slack_channel_id,
                    jira_issue_key = EXCLUDED.jira_issue_key,
                    original_ticket_data = EXCLUDED.original_ticket_data,
                    previous_review = EXCLUDED.previous_review,
                    status = EXCLUDED.status,
                    messages = EXCLUDED.messages,
                    updated_at = NOW()
                RETURNING *
                """,
                {
                    "slack_thread_ts": slack_thread_ts,
                    "slack_channel_id": slack_channel_id,
                    "jira_issue_key": jira_issue_key,
                    "original_ticket_data": self._json(original_ticket_data),
                    "previous_review": self._json(previous_review),
                    "status": status,
                    "messages": self._json(messages),
                },
            ).fetchone()

        return self._record(row)

    def get_by_thread(self, slack_thread_ts: str) -> ConversationRecord | None:
        log.debug("Looking up conversation for thread=%s", slack_thread_ts)
        self.init_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jira_slack_conversations WHERE slack_thread_ts = %s",
                (slack_thread_ts,),
            ).fetchone()
        if row:
            log.debug("Found conversation for thread=%s issue=%s", slack_thread_ts, row["jira_issue_key"])
        else:
            log.warning("No conversation found for thread=%s", slack_thread_ts)
        return self._record(row) if row else None

    def append_message_and_update(
        self,
        *,
        slack_thread_ts: str,
        user_message: dict[str, Any],
        assistant_message: dict[str, Any],
        previous_review: dict[str, Any],
        status: str,
    ) -> ConversationRecord:
        log.info("Appending messages and updating conversation thread=%s status=%s", slack_thread_ts, status)
        self.init_schema()
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE jira_slack_conversations
                SET
                    messages = messages || %(messages)s::jsonb,
                    previous_review = %(previous_review)s::jsonb,
                    status = %(status)s,
                    updated_at = NOW()
                WHERE slack_thread_ts = %(slack_thread_ts)s
                RETURNING *
                """,
                {
                    "messages": self._json([user_message, assistant_message]),
                    "previous_review": self._json(previous_review),
                    "status": status,
                    "slack_thread_ts": slack_thread_ts,
                },
            ).fetchone()

        if not row:
            raise ConversationStoreError(f"No conversation found for Slack thread {slack_thread_ts}")

        return self._record(row)

    def _connect(self):
        return self._psycopg.connect(self.settings.database_url, row_factory=self._dict_row)

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _record(self, row: dict[str, Any]) -> ConversationRecord:
        return ConversationRecord(
            id=row["id"],
            slack_thread_ts=row["slack_thread_ts"],
            slack_channel_id=row["slack_channel_id"],
            jira_issue_key=row["jira_issue_key"],
            original_ticket_data=row["original_ticket_data"],
            previous_review=row["previous_review"],
            status=row["status"],
            messages=row["messages"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()
