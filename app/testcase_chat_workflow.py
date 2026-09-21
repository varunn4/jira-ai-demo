"""Slack Q&A and edit handling for Jira ticket threads.

The n8n closing flow posts Slack thread replies to /workflow2. This module maps
the Slack thread back to its Jira ticket, loads live ticket data and stored test
cases, asks Claude whether the message is about the ticket, the test cases, or a
test-case edit, then optionally syncs edited test cases back to Jira and
Postgres.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from app.config import Settings
from app.exceptions import LLMConfigurationError

log = logging.getLogger(__name__)

TC_COMMENT_MARKER = "AI-GOVERNOR-TESTCASES-V1"
TC_COMMENT_MARKER_DEV = "AI-GOVERNOR-TESTCASES-DEV-V1"


def _phase(value: Any) -> str:
    """Normalise any incoming phase value to 'qa' (default) or 'dev'."""
    return "dev" if str(value or "").lower() == "dev" else "qa"


def _comment_marker(phase: str) -> str:
    return TC_COMMENT_MARKER_DEV if _phase(phase) == "dev" else TC_COMMENT_MARKER


ROUTE_TOOL = {
    "name": "respond_to_user",
    "description": (
        "Classify the user's Slack thread message and respond. The thread is "
        "attached to a Jira ticket. Decide which kind of help they need: "
        "questions about the ticket (status, assignee, priority, summary, "
        "similar tickets) vs questions about the test cases vs requests to "
        "edit the test cases. Use the ticket and test-case context provided. "
        "For an edit, return the COMPLETE new test-case list, preserving every "
        "case the user didn't explicitly ask to change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["ticket_question", "testcase_question", "testcase_edit"],
                "description": "Which kind of message this is.",
            },
            "reply": {
                "type": "string",
                "description": "Short Slack-friendly answer (or edit confirmation).",
            },
            "updated_test_cases": {
                "type": "array",
                "description": "Required ONLY when intent=testcase_edit. Full new test-case list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "expected": {"type": "string"},
                    },
                    "required": ["title", "steps", "expected"],
                },
            },
        },
        "required": ["intent", "reply"],
    },
}

SYSTEM_PROMPT = (
    "You are the AI Governor assistant inside a Slack thread attached to a Jira "
    "ticket. You receive the ticket details, the ticket's Jira comments "
    "(including AI-Governor doc-review and test-case comments plus any human "
    "discussion), the test cases, and recent thread history. Use the Jira "
    "comments as authoritative context when answering questions about the "
    "ticket, its PRD/Tech-doc review, or prior decisions. Always call the "
    "respond_to_user tool. Classify the user's "
    "message as ticket_question (about the ticket itself), testcase_question "
    "(about the test cases), or testcase_edit (modify the test cases). For edits, "
    "return the COMPLETE new test-case list. Keep replies concise and friendly "
    "for Slack. If a question is ambiguous, lean toward the more specific topic "
    "the user named; only ask for clarification when truly unclear."
)

_DEV_PROMPT_SUFFIX = (
    " These are DEVELOPER test cases for a ticket at the Code Review stage: "
    "implementation-level cases (unit-test scenarios, code paths, edge/boundary "
    "and error handling, integration points) that a developer runs to verify "
    "their own work before QA. Frame answers and edits for a developer audience."
)


def _phase_prompt_suffix(phase: str) -> str:
    return _DEV_PROMPT_SUFFIX if _phase(phase) == "dev" else ""


class TestCaseChatWorkflow:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is required for /workflow2")

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary] to use /workflow2") from exc

        self._psycopg = psycopg
        self._dict_row = dict_row
        self._phase_schema_ready = False

    def handle(
        self,
        *,
        slack_channel_id: str,
        slack_thread_ts: str,
        user_message: str,
        phase: str = "qa",
    ) -> str:
        started_at = time.perf_counter()
        phase = "dev" if str(phase).lower() == "dev" else "qa"

        def finish(reply: str, outcome: str, extra: dict[str, Any] | None = None) -> str:
            output = {
                "outcome": outcome,
                "reply_chars": len(reply),
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            }
            if extra:
                output.update(extra)
            self._log_step("workflow2", "completed", output=output)
            return reply

        self._log_step(
            "api_request",
            "received",
            output={
                "slack_channel_id": slack_channel_id,
                "slack_thread_ts": slack_thread_ts,
                "user_message_chars": len(user_message),
                "user_message_preview": self._preview(user_message),
            },
        )

        self._log_step(
            "1_resolve_ticket",
            "started",
            input_data={"slack_channel_id": slack_channel_id, "slack_thread_ts": slack_thread_ts},
        )
        ticket_row = self.resolve_ticket_from_thread(slack_channel_id, slack_thread_ts)
        if not ticket_row:
            self._log_step(
                "1_resolve_ticket",
                "completed",
                output={"ticket_found": False},
            )
            return finish(
                "I couldn't link this thread to a Jira ticket, so I can't help here.",
                "ticket_not_found",
            )

        ticket_id = str(ticket_row["jira_ticket_id"])
        # The thread's own phase (recorded by the closing flow) is authoritative:
        # WF2 sends no phase, so without this a dev thread would load QA cases.
        resolved_phase = ticket_row.get("_phase")
        if resolved_phase:
            phase = _phase(resolved_phase)
        self._log_step(
            "1_resolve_ticket",
            "completed",
            output={
                "ticket_found": True,
                "ticket_db_id": ticket_row.get("id"),
                "jira_ticket_id": ticket_id,
                "ticket_status": ticket_row.get("status") or "",
                "source": ticket_row.get("_source") or "unknown",
                "phase": phase,
            },
        )

        self._log_step(
            "2_fetch_ticket_context",
            "started",
            input_data={"jira_ticket_id": ticket_id, "jira_configured": self._jira_configured()},
        )
        ticket_live = self.fetch_jira_ticket(ticket_id)
        ticket_context = ticket_live or self._ticket_context_from_row(ticket_row)
        ticket_context["comments"] = self.fetch_jira_comments(ticket_id)
        self._log_step(
            "2_fetch_ticket_context",
            "completed",
            output={
                "jira_ticket_id": ticket_id,
                "source": "jira_live" if ticket_live else "db_fallback",
                "summary_chars": len(str(ticket_context.get("summary") or "")),
                "description_chars": len(str(ticket_context.get("description") or "")),
                "status": ticket_context.get("status") or "",
                "priority": ticket_context.get("priority") or "",
                "assignee_present": bool(ticket_context.get("assignee")),
                "labels_count": len(ticket_context.get("labels") or []),
                "comments_count": len(ticket_context.get("comments") or []),
            },
        )

        self._log_step("3_load_test_cases", "started", input_data={"jira_ticket_id": ticket_id, "phase": phase})
        test_cases = self.load_test_cases(ticket_id, phase)
        self._log_step(
            "3_load_test_cases",
            "completed",
            output={
                "jira_ticket_id": ticket_id,
                "test_cases_count": len(test_cases),
                "tc_indexes": [tc.get("tc_index") for tc in test_cases[:20]],
            },
        )

        self._log_step(
            "4_load_recent_messages",
            "started",
            input_data={"ticket_db_id": ticket_row.get("id"), "limit": 6},
        )
        history = self.load_recent_messages(ticket_row.get("id"))
        self._log_step(
            "4_load_recent_messages",
            "completed",
            output={
                "ticket_db_id": ticket_row.get("id"),
                "messages_count": len(history),
                "senders": [row.get("sender") for row in history],
            },
        )

        try:
            self._log_step(
                "5_route_with_claude",
                "started",
                input_data={
                    "jira_ticket_id": ticket_id,
                    "model": self.settings.testcase_chat_model,
                    "test_cases_count": len(test_cases),
                    "history_count": len(history),
                    "user_message_chars": len(user_message),
                },
            )
            result = self.reason(user_message, ticket_context, test_cases, history, phase)
        except Exception:
            log.exception("Test-case chat reasoning failed for %s", ticket_id)
            self._log_step(
                "5_route_with_claude",
                "failed",
                input_data={"jira_ticket_id": ticket_id, "model": self.settings.testcase_chat_model},
            )
            return finish(
                "Something went wrong while thinking about that. Try again in a moment.",
                "reasoning_failed",
                {"jira_ticket_id": ticket_id},
            )

        intent = result.get("intent") or "ticket_question"
        base_reply = result.get("reply") or ""
        updated_test_cases = result.get("updated_test_cases")
        self._log_step(
            "5_route_with_claude",
            "completed",
            output={
                "jira_ticket_id": ticket_id,
                "intent": intent,
                "reply_chars": len(base_reply),
                "has_updated_test_cases": isinstance(updated_test_cases, list),
                "updated_test_cases_count": len(updated_test_cases) if isinstance(updated_test_cases, list) else 0,
            },
        )

        if intent == "testcase_edit" and isinstance(result.get("updated_test_cases"), list):
            self._log_step(
                "6_normalize_edit",
                "started",
                input_data={"jira_ticket_id": ticket_id, "incoming_test_cases_count": len(result["updated_test_cases"])},
            )
            new_tcs = self._normalize_test_cases(result["updated_test_cases"])
            self._log_step(
                "6_normalize_edit",
                "completed",
                output={"jira_ticket_id": ticket_id, "normalized_test_cases_count": len(new_tcs)},
            )
            if not test_cases:
                reply = (
                    base_reply
                    + f"\n\nWarning: I don't see any existing test cases stored for {ticket_id}, "
                    "so I can't apply this edit. Please regenerate them first."
                ).strip()
                self._log_step(
                    "7_apply_edit",
                    "blocked",
                    output={"jira_ticket_id": ticket_id, "reason": "no_existing_test_cases"},
                )
                self.record_exchange(ticket_row.get("id"), user_message, reply)
                return finish(
                    reply,
                    "edit_blocked_no_existing_test_cases",
                    {"jira_ticket_id": ticket_id, "intent": intent},
                )

            self._log_step(
                "7_update_jira_comment",
                "started",
                input_data={"jira_ticket_id": ticket_id, "test_cases_count": len(new_tcs)},
            )
            jira_ok = self.update_jira_comment(ticket_id, new_tcs, phase)
            self._log_step(
                "7_update_jira_comment",
                "completed",
                output={"jira_ticket_id": ticket_id, "jira_ok": jira_ok, "test_cases_count": len(new_tcs)},
            )
            self._log_step(
                "8_sync_database",
                "started",
                input_data={"jira_ticket_id": ticket_id, "test_cases_count": len(new_tcs)},
            )
            db_ok = self._sync_database(ticket_id, new_tcs, phase)
            self._log_step(
                "8_sync_database",
                "completed",
                output={"jira_ticket_id": ticket_id, "db_ok": db_ok, "test_cases_count": len(new_tcs)},
            )
            confirm = base_reply or "Updated the test cases."

            if jira_ok and db_ok:
                reply = (
                    f"{confirm}\n\n"
                    f"_Updated {ticket_id}'s test cases ({len(new_tcs)} total) and synced the database._"
                )
            elif jira_ok and not db_ok:
                reply = (
                    f"{confirm}\n\n"
                    "Warning: Jira comment updated, but the DB sync failed. Please check logs."
                )
            elif db_ok and not jira_ok:
                reply = (
                    f"{confirm}\n\n"
                    "Warning: DB saved, but the Jira comment update failed. Please check the ticket."
                )
            else:
                reply = "Warning: Tried to apply that edit but both Jira and DB updates failed. Please check the logs."

            self.record_exchange(ticket_row.get("id"), user_message, reply)
            return finish(
                reply,
                "edit_processed",
                {
                    "jira_ticket_id": ticket_id,
                    "intent": intent,
                    "jira_ok": jira_ok,
                    "db_ok": db_ok,
                    "updated_test_cases_count": len(new_tcs),
                },
            )

        reply = base_reply or "Here's what I found."
        if intent == "testcase_question" and not test_cases:
            reply = f"I don't see any test cases stored for {ticket_id} yet."
        self._log_step(
            "6_answer_question",
            "completed",
            output={
                "jira_ticket_id": ticket_id,
                "intent": intent,
                "reply_chars": len(reply),
                "test_cases_count": len(test_cases),
            },
        )
        self.record_exchange(ticket_row.get("id"), user_message, reply)
        return finish(
            reply,
            "question_answered",
            {"jira_ticket_id": ticket_id, "intent": intent},
        )

    def resolve_ticket_from_thread(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        """Map a Slack thread to the best available ticket row.

        The n8n closing flows (WF5 for QA, WF5b for the Code-Review/dev flow)
        record each Slack thread with its phase in `testcase_threads`, so a
        ticket can have BOTH a QA thread and a dev thread and each resolves to
        the correct phase — that mapping is checked first and sets `_phase`.
        The `tickets` table only holds one thread per ticket (whichever flow ran
        last), so it and channelid_table / jira_slack_conversations are kept as
        phase-agnostic fallbacks.
        """
        with self._connect() as conn:
            if self._table_exists(conn, "testcase_threads"):
                row = conn.execute(
                    """
                    SELECT jira_ticket_id, phase
                    FROM testcase_threads
                    WHERE slack_channel_id = %s AND slack_thread_ts = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (channel_id, thread_ts),
                ).fetchone()
                if row and row.get("jira_ticket_id"):
                    ticket = self._ticket_row_for_issue_key(conn, str(row["jira_ticket_id"]))
                    ticket["_source"] = "testcase_threads"
                    ticket["_phase"] = _phase(row.get("phase"))
                    return ticket

            if (
                self._table_exists(conn, "tickets")
                and self._column_type(conn, "tickets", "slack_channel_id")
                and self._column_type(conn, "tickets", "slack_thread_ts")
            ):
                row = conn.execute(
                    f"""
                    SELECT {self._ticket_select_columns(conn)}
                    FROM tickets
                    WHERE slack_channel_id = %s AND slack_thread_ts = %s
                    ORDER BY {self._ticket_order_column(conn)} DESC
                    LIMIT 1
                    """,
                    (channel_id, thread_ts),
                ).fetchone()
                if row and row.get("jira_ticket_id"):
                    ticket = self._ticket_row(row)
                    ticket["_source"] = "tickets"
                    return ticket

            if self._table_exists(conn, "channelid_table"):
                channel_order_column = (
                    "created_at" if self._column_type(conn, "channelid_table", "created_at") else "slack_thread_ts"
                )
                row = conn.execute(
                    f"""
                    SELECT jira_payload
                    FROM channelid_table
                    WHERE slack_channel_id = %s AND slack_thread_ts = %s
                    ORDER BY {channel_order_column} DESC
                    LIMIT 1
                    """,
                    (channel_id, thread_ts),
                ).fetchone()
                issue_key = self._issue_key_from_channel_row(row)
                if issue_key:
                    ticket = self._ticket_row_for_issue_key(
                        conn,
                        issue_key,
                        fallback_payload=(row or {}).get("jira_payload"),
                    )
                    ticket["_source"] = "channelid_table"
                    return ticket

            if self._table_exists(conn, "jira_slack_conversations"):
                row = conn.execute(
                    """
                    SELECT jira_issue_key, original_ticket_data, status
                    FROM jira_slack_conversations
                    WHERE slack_channel_id = %s AND slack_thread_ts = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (channel_id, thread_ts),
                ).fetchone()
                if row and row.get("jira_issue_key"):
                    ticket = self._ticket_row_for_issue_key(
                        conn,
                        str(row["jira_issue_key"]),
                        fallback_payload=row.get("original_ticket_data"),
                        fallback_status=row.get("status"),
                    )
                    ticket["_source"] = "jira_slack_conversations"
                    return ticket

        return None

    def load_test_cases(self, jira_ticket_id: str, phase: str = "qa") -> list[dict[str, Any]]:
        phase = _phase(phase)
        self._ensure_phase_schema()
        with self._connect() as conn:
            if not self._table_exists(conn, "test_cases"):
                return []
            rows = conn.execute(
                """
                SELECT tc_index, title, steps, expected, status
                FROM test_cases
                WHERE jira_ticket_id = %s AND phase = %s
                ORDER BY tc_index ASC
                """,
                (jira_ticket_id, phase),
            ).fetchall()

        test_cases: list[dict[str, Any]] = []
        for row in rows:
            steps = self._steps(row.get("steps"))
            test_cases.append(
                {
                    "tc_index": row["tc_index"],
                    "title": row["title"] or f"Test Case {row['tc_index']}",
                    "steps": steps,
                    "expected": row["expected"] or "",
                    "status": row["status"] or "pending",
                }
            )
        return test_cases

    def load_recent_messages(self, ticket_db_id: Any, limit: int = 6) -> list[dict[str, Any]]:
        """Pull recent thread messages so Claude has conversational context."""
        if not ticket_db_id:
            return []
        with self._connect() as conn:
            if not self._table_exists(conn, "messages"):
                return []
            order_column = self._messages_order_column(conn)
            rows = conn.execute(
                f"""
                SELECT sender, message
                FROM messages
                WHERE ticket_id = %s
                ORDER BY {order_column} DESC
                LIMIT %s
                """,
                (ticket_db_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_exchange(self, ticket_db_id: Any, user_message: str, bot_reply: str) -> None:
        if not ticket_db_id:
            self._log_step(
                "9_record_exchange",
                "skipped",
                output={"reason": "missing_ticket_db_id"},
            )
            return

        self._log_step(
            "9_record_exchange",
            "started",
            input_data={
                "ticket_db_id": ticket_db_id,
                "user_message_chars": len(user_message),
                "bot_reply_chars": len(bot_reply),
            },
        )
        try:
            user_saved = self.record_message(ticket_db_id, "user", user_message)
            bot_saved = self.record_message(ticket_db_id, "bot", bot_reply)
            self._log_step(
                "9_record_exchange",
                "completed",
                output={
                    "ticket_db_id": ticket_db_id,
                    "user_message_saved": user_saved,
                    "bot_message_saved": bot_saved,
                    "messages_saved": int(user_saved) + int(bot_saved),
                },
            )
        except Exception:
            self._log_step(
                "9_record_exchange",
                "failed",
                input_data={"ticket_db_id": ticket_db_id},
            )
            log.exception("Could not record /workflow2 message exchange for ticket_id=%s", ticket_db_id)

    def record_message(self, ticket_db_id: Any, sender: str, message: str) -> bool:
        if not ticket_db_id:
            return False
        with self._connect() as conn:
            if not self._table_exists(conn, "messages"):
                log.warning("messages table is missing; skipping /workflow2 message persistence")
                return False
            conn.execute(
                "INSERT INTO messages (ticket_id, sender, message) VALUES (%s, %s, %s)",
                (ticket_db_id, sender, message),
            )
        return True

    def upsert_test_cases(
        self, ticket_id: str, test_cases: list[dict[str, Any]], phase: str = "qa"
    ) -> None:
        phase = _phase(phase)
        self._ensure_phase_schema()
        with self._connect() as conn:
            with conn.transaction():
                self._ensure_ticket_row(conn, ticket_id)
                steps_expression = self._steps_insert_expression(conn)
                for index, test_case in enumerate(test_cases, start=1):
                    conn.execute(
                        f"""
                        INSERT INTO test_cases (
                            ticket_id,
                            jira_ticket_id,
                            phase,
                            tc_index,
                            subtask_key,
                            title,
                            steps,
                            expected,
                            status
                        )
                        VALUES (
                            (SELECT id FROM tickets WHERE jira_ticket_id = %s LIMIT 1),
                            %s,
                            %s,
                            %s,
                            NULL,
                            %s,
                            {steps_expression},
                            %s,
                            'pending'
                        )
                        ON CONFLICT (jira_ticket_id, phase, tc_index)
                        DO UPDATE SET
                            title = EXCLUDED.title,
                            steps = EXCLUDED.steps,
                            expected = EXCLUDED.expected,
                            updated_at = NOW()
                        """,
                        (
                            ticket_id,
                            ticket_id,
                            phase,
                            index,
                            test_case.get("title") or f"Test Case {index}",
                            json.dumps(test_case.get("steps") or []),
                            test_case.get("expected") or "",
                        ),
                    )
                conn.execute(
                    "DELETE FROM test_cases WHERE jira_ticket_id = %s AND phase = %s AND tc_index > %s",
                    (ticket_id, phase, len(test_cases)),
                )

        # Best-effort: (re)embed this ticket's test cases so the regression flag
        # in Workflow 1 can match new tickets against them. Never blocks the write.
        self._embed_test_cases_best_effort(ticket_id, phase)

    def _embed_test_cases_best_effort(self, ticket_id: str, phase: str) -> None:
        try:
            from app.testcase_embeddings import embed_ticket_testcases
            embed_ticket_testcases(self.settings, ticket_id, phase)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("test-case embedding skipped for %s: %s", ticket_id, exc)

    def update_jira_comment(
        self, ticket_id: str, test_cases: list[dict[str, Any]], phase: str = "qa"
    ) -> bool:
        if not self._jira_configured():
            self._log_step(
                "7_update_jira_comment",
                "skipped",
                output={"jira_ticket_id": ticket_id, "reason": "jira_not_configured"},
            )
            log.warning("Jira is not configured; skipping test-case comment update for %s", ticket_id)
            return False

        body = render_comment_body(ticket_id, test_cases, phase)
        comment_id = self.find_existing_tc_comment(ticket_id, phase)
        if comment_id:
            method = requests.put
            url = f"{self.settings.jira_base_url}/rest/api/2/issue/{ticket_id}/comment/{comment_id}"
        else:
            method = requests.post
            url = f"{self.settings.jira_base_url}/rest/api/2/issue/{ticket_id}/comment"

        action = "update" if comment_id else "create"
        self._log_step(
            "7_jira_comment_request",
            "started",
            input_data={
                "jira_ticket_id": ticket_id,
                "action": action,
                "comment_found": bool(comment_id),
                "comment_id": comment_id,
                "body_chars": len(body),
                "test_cases_count": len(test_cases),
            },
        )
        try:
            response = method(
                url,
                auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"body": body},
                timeout=self.settings.external_request_timeout_seconds,
            )
            if response.status_code >= 400:
                self._log_step(
                    "7_jira_comment_request",
                    "failed",
                    output={
                        "jira_ticket_id": ticket_id,
                        "action": action,
                        "status_code": response.status_code,
                        "response_preview": self._preview(response.text, limit=300),
                    },
                )
                log.warning("Jira test-case comment update failed for %s: %s", ticket_id, response.text[:500])
                return False
            self._log_step(
                "7_jira_comment_request",
                "completed",
                output={
                    "jira_ticket_id": ticket_id,
                    "action": action,
                    "status_code": response.status_code,
                    "comment_id": comment_id,
                },
            )
            return True
        except requests.RequestException:
            self._log_step(
                "7_jira_comment_request",
                "failed",
                input_data={"jira_ticket_id": ticket_id, "action": action},
            )
            log.exception("Jira test-case comment update failed for %s", ticket_id)
            return False

    def find_existing_tc_comment(self, ticket_id: str, phase: str = "qa") -> str | None:
        marker = _comment_marker(phase)
        self._log_step(
            "7_fetch_jira_comments",
            "started",
            input_data={"jira_ticket_id": ticket_id, "marker": marker},
        )
        try:
            response = requests.get(
                f"{self.settings.jira_base_url}/rest/api/2/issue/{ticket_id}/comment?maxResults=100",
                auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
                headers={"Accept": "application/json"},
                timeout=self.settings.external_request_timeout_seconds,
            )
            if response.status_code >= 400:
                self._log_step(
                    "7_fetch_jira_comments",
                    "failed",
                    output={
                        "jira_ticket_id": ticket_id,
                        "status_code": response.status_code,
                        "response_preview": self._preview(response.text, limit=300),
                    },
                )
                log.warning("Could not fetch Jira comments for %s: %s", ticket_id, response.text[:500])
                return None
        except requests.RequestException:
            self._log_step(
                "7_fetch_jira_comments",
                "failed",
                input_data={"jira_ticket_id": ticket_id},
            )
            log.exception("Could not fetch Jira comments for %s", ticket_id)
            return None

        comments = response.json().get("comments", [])
        for comment in comments:
            body = comment.get("body") or ""
            if marker in self._comment_text(body):
                self._log_step(
                    "7_fetch_jira_comments",
                    "completed",
                    output={
                        "jira_ticket_id": ticket_id,
                        "status_code": response.status_code,
                        "comments_count": len(comments),
                        "comment_found": True,
                        "comment_id": str(comment["id"]),
                    },
                )
                return str(comment["id"])
        self._log_step(
            "7_fetch_jira_comments",
            "completed",
            output={
                "jira_ticket_id": ticket_id,
                "status_code": response.status_code,
                "comments_count": len(comments),
                "comment_found": False,
            },
        )
        return None

    def fetch_jira_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        """Live-fetch the ticket from Jira so ticket questions use current data."""
        if not self._jira_configured():
            self._log_step(
                "2_jira_ticket_request",
                "skipped",
                output={"jira_ticket_id": ticket_id, "reason": "jira_not_configured"},
            )
            return None

        self._log_step(
            "2_jira_ticket_request",
            "started",
            input_data={"jira_ticket_id": ticket_id},
        )
        try:
            response = requests.get(
                f"{self.settings.jira_base_url}/rest/api/3/issue/{ticket_id}",
                params={"fields": "summary,status,priority,assignee,reporter,duedate,description,labels"},
                auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
                headers={"Accept": "application/json"},
                timeout=self.settings.external_request_timeout_seconds,
            )
            if response.status_code >= 400:
                self._log_step(
                    "2_jira_ticket_request",
                    "failed",
                    output={
                        "jira_ticket_id": ticket_id,
                        "status_code": response.status_code,
                        "response_preview": self._preview(response.text, limit=300),
                    },
                )
                log.warning("Could not fetch Jira ticket %s: %s", ticket_id, response.text[:500])
                return None
        except requests.RequestException:
            self._log_step(
                "2_jira_ticket_request",
                "failed",
                input_data={"jira_ticket_id": ticket_id},
            )
            log.exception("Could not fetch Jira ticket %s", ticket_id)
            return None

        fields = (response.json() or {}).get("fields", {})
        self._log_step(
            "2_jira_ticket_request",
            "completed",
            output={
                "jira_ticket_id": ticket_id,
                "status_code": response.status_code,
                "fields_count": len(fields),
            },
        )
        return {
            "issueKey": ticket_id,
            "summary": fields.get("summary") or "",
            "status": ((fields.get("status") or {}).get("name") or ""),
            "priority": ((fields.get("priority") or {}).get("name") or ""),
            "assignee": ((fields.get("assignee") or {}).get("displayName") or ""),
            "reporter": ((fields.get("reporter") or {}).get("displayName") or ""),
            "dueDate": fields.get("duedate") or "",
            "labels": fields.get("labels") or [],
            "description": self._jira_description_text(fields.get("description")),
            "url": f"{self.settings.jira_base_url}/browse/{ticket_id}",
        }

    def fetch_jira_comments(self, ticket_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch the ticket's Jira comments (newest first) so the bot can use
        them as context — including the AI-Governor doc-review / test-case
        comments and any human discussion on the ticket."""
        if not self._jira_configured():
            self._log_step(
                "2b_jira_comments_request",
                "skipped",
                output={"jira_ticket_id": ticket_id, "reason": "jira_not_configured"},
            )
            return []

        self._log_step("2b_jira_comments_request", "started", input_data={"jira_ticket_id": ticket_id})
        try:
            response = requests.get(
                f"{self.settings.jira_base_url}/rest/api/2/issue/{ticket_id}/comment",
                params={"maxResults": limit, "orderBy": "-created"},
                auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
                headers={"Accept": "application/json"},
                timeout=self.settings.external_request_timeout_seconds,
            )
            if response.status_code >= 400:
                self._log_step(
                    "2b_jira_comments_request",
                    "failed",
                    output={
                        "jira_ticket_id": ticket_id,
                        "status_code": response.status_code,
                        "response_preview": self._preview(response.text, limit=300),
                    },
                )
                log.warning("Could not fetch Jira comments for %s: %s", ticket_id, response.text[:300])
                return []
        except requests.RequestException:
            self._log_step("2b_jira_comments_request", "failed", input_data={"jira_ticket_id": ticket_id})
            log.exception("Could not fetch Jira comments for %s", ticket_id)
            return []

        raw = (response.json() or {}).get("comments", [])
        comments: list[dict[str, Any]] = []
        for comment in raw:
            comments.append(
                {
                    "author": ((comment.get("author") or {}).get("displayName") or ""),
                    "created": comment.get("created") or "",
                    "body": self._jira_description_text(comment.get("body"))[:1500],
                }
            )
        # Present oldest-first for a natural reading order in the prompt.
        comments.reverse()
        self._log_step(
            "2b_jira_comments_request",
            "completed",
            output={"jira_ticket_id": ticket_id, "comments_count": len(comments)},
        )
        return comments

    def reason(
        self,
        user_message: str,
        ticket: dict[str, Any],
        test_cases: list[dict[str, Any]],
        history: list[dict[str, Any]],
        phase: str = "qa",
    ) -> dict[str, Any]:
        from app.llm_client import build_llm_client

        client = build_llm_client(self.settings)
        tc_context = json.dumps(
            [
                {
                    "tc_index": tc.get("tc_index"),
                    "title": tc.get("title"),
                    "steps": tc.get("steps") or [],
                    "expected": tc.get("expected") or "",
                }
                for tc in test_cases
            ],
            indent=2,
            ensure_ascii=False,
        )
        ticket_context = {
            key: value for key, value in ticket.items() if key not in ("description", "comments")
        }
        if ticket.get("description"):
            ticket_context["description"] = str(ticket["description"])[:4000]
        ticket_blob = json.dumps(ticket_context, indent=2, ensure_ascii=False)
        comments = ticket.get("comments") or []
        comments_blob = "\n".join(
            f"  [{c.get('created') or ''}] {c.get('author') or 'unknown'}: {str(c.get('body') or '')[:800]}"
            for c in comments
        ) or "  (no comments)"
        history_blob = "\n".join(
            f"  {row.get('sender')}: {str(row.get('message') or '')[:300]}" for row in history
        ) or "  (no prior messages)"
        user_block = (
            f"Ticket details:\n{ticket_blob}\n\n"
            f"Jira comments ({len(comments)}, oldest first):\n{comments_blob}\n\n"
            f"Test cases ({len(test_cases)}):\n{tc_context}\n\n"
            f"Recent thread history (oldest first):\n{history_blob}\n\n"
            f"User message:\n{user_message}"
        )
        self._log_step(
            "5_build_claude_context",
            "completed",
            output={
                "jira_ticket_id": ticket.get("issueKey") or "",
                "ticket_context_chars": len(ticket_blob),
                "comments_count": len(comments),
                "comments_context_chars": len(comments_blob),
                "test_case_context_chars": len(tc_context),
                "history_context_chars": len(history_blob),
                "user_block_chars": len(user_block),
                "has_description": bool(ticket.get("description")),
            },
        )

        # Check if native Anthropic tool calling is available
        if getattr(self.settings, "anthropic_api_key", None) and str(self.settings.llm_provider).lower() == "anthropic":
            try:
                from anthropic import Anthropic
                ant_client = Anthropic(
                    api_key=self.settings.anthropic_api_key,
                    timeout=self.settings.llm_timeout_seconds,
                )
                message = ant_client.messages.create(
                    model=self.settings.testcase_chat_model,
                    max_tokens=2000,
                    system=SYSTEM_PROMPT + _phase_prompt_suffix(phase),
                    tools=[ROUTE_TOOL],
                    tool_choice={"type": "tool", "name": "respond_to_user"},
                    messages=[{"role": "user", "content": user_block}],
                )
                for block in message.content:
                    if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "respond_to_user":
                        return dict(block.input)
            except Exception as exc:
                log.warning("Anthropic tool call failed (%s), falling back to universal completion", exc)

        # Universal completion fallback for OpenAI / Groq / Gemini / Mock
        sys_prompt = SYSTEM_PROMPT + _phase_prompt_suffix(phase) + "\n\nReturn ONLY a JSON object with 'intent' and 'reply' keys."
        try:
            raw_text = client.complete(system_prompt=sys_prompt, user_message=user_block, max_tokens=2000)
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict) and "reply" in parsed:
                return parsed
        except Exception:
            pass

        return {
            "intent": "ticket_question",
            "reply": f"Processed query regarding ticket {ticket.get('issueKey', '')}.",
        }

    def _log_step(
        self,
        step: str,
        status: str,
        *,
        input_data: Any | None = None,
        output: Any | None = None,
    ) -> None:
        payload = {
            "step": step,
            "status": status,
            "input": input_data,
            "output": output,
        }
        log.info("workflow2 structured log: %s", json.dumps(payload, ensure_ascii=False, default=str))

    def _preview(self, value: Any, limit: int = 160) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    def _sync_database(
        self, ticket_id: str, test_cases: list[dict[str, Any]], phase: str = "qa"
    ) -> bool:
        try:
            self.upsert_test_cases(ticket_id, test_cases, phase)
            return True
        except Exception:
            log.exception("Postgres test-case sync failed for %s", ticket_id)
            return False

    def _connect(self):
        return self._psycopg.connect(self.settings.database_url, row_factory=self._dict_row)

    def _table_exists(self, conn: Any, table_name: str) -> bool:
        row = conn.execute("SELECT to_regclass(%s) AS table_name", (f"public.{table_name}",)).fetchone()
        return bool(row and row.get("table_name"))

    def _column_type(self, conn: Any, table_name: str, column_name: str) -> str:
        row = conn.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = %s
            LIMIT 1
            """,
            (table_name, column_name),
        ).fetchone()
        return str(row["data_type"]) if row and row.get("data_type") else ""

    def _ensure_phase_schema(self) -> None:
        """Self-heal test_cases so QA and dev cases coexist per ticket.

        Adds a `phase` column and swaps the unique key from
        (jira_ticket_id, tc_index) to (jira_ticket_id, phase, tc_index) — the
        target of the upsert's ON CONFLICT. Idempotent and guarded so the work
        runs at most once per process; runs in its own connection so a DDL
        failure never poisons the caller's write transaction. The canonical
        migration is scripts/sql/2026-07-27_test_cases_phase.sql."""
        if self._phase_schema_ready:
            return
        try:
            with self._connect() as conn:
                if not self._table_exists(conn, "test_cases"):
                    self._phase_schema_ready = True
                    return
                conn.execute(
                    "ALTER TABLE test_cases ADD COLUMN IF NOT EXISTS phase "
                    "VARCHAR(8) NOT NULL DEFAULT 'qa'"
                )
                target = conn.execute(
                    "SELECT to_regclass('public.test_cases_jira_phase_tc_uidx') AS idx"
                ).fetchone()
                if not (target and target.get("idx")):
                    # Drop any legacy unique CONSTRAINT on exactly (jira_ticket_id, tc_index):
                    # it would reject a dev row that shares (ticket, index) with a QA row.
                    for row in conn.execute(
                        """
                        SELECT conname FROM pg_constraint
                        WHERE conrelid = 'public.test_cases'::regclass AND contype = 'u'
                          AND (SELECT array_agg(attname ORDER BY attname)
                               FROM pg_attribute
                               WHERE attrelid = conrelid AND attnum = ANY(conkey))
                              = ARRAY['jira_ticket_id', 'tc_index']::name[]
                        """
                    ).fetchall():
                        conn.execute(f'ALTER TABLE test_cases DROP CONSTRAINT "{row["conname"]}"')
                    # Drop any stray unique INDEX (not backing a constraint) on the same pair.
                    for row in conn.execute(
                        """
                        SELECT i.relname FROM pg_index x
                        JOIN pg_class i ON i.oid = x.indexrelid
                        JOIN pg_class t ON t.oid = x.indrelid
                        WHERE t.relname = 'test_cases' AND x.indisunique AND NOT x.indisprimary
                          AND NOT EXISTS (
                              SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)
                          AND (SELECT array_agg(a.attname ORDER BY a.attname)
                               FROM pg_attribute a
                               WHERE a.attrelid = t.oid AND a.attnum = ANY(x.indkey))
                              = ARRAY['jira_ticket_id', 'tc_index']::name[]
                        """
                    ).fetchall():
                        conn.execute(f'DROP INDEX IF EXISTS "{row["relname"]}"')
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS test_cases_jira_phase_tc_uidx "
                        "ON test_cases (jira_ticket_id, phase, tc_index)"
                    )
            self._phase_schema_ready = True
        except Exception:
            log.exception("Could not ensure test_cases.phase schema; will retry on next write")

    def _ticket_select_columns(self, conn: Any) -> str:
        columns = [
            column
            for column in ("id", "jira_ticket_id", "status", "slack_channel_id", "slack_thread_ts", "jira_payload")
            if self._column_type(conn, "tickets", column)
        ]
        return ", ".join(columns or ["id", "jira_ticket_id"])

    def _ticket_order_column(self, conn: Any) -> str:
        for column in ("id", "created_at", "jira_ticket_id"):
            if self._column_type(conn, "tickets", column):
                return column
        return "jira_ticket_id"

    def _ticket_row(
        self,
        row: dict[str, Any],
        *,
        fallback_payload: Any | None = None,
        fallback_status: Any | None = None,
    ) -> dict[str, Any]:
        ticket = dict(row)
        ticket.setdefault("id", None)
        ticket.setdefault("status", fallback_status or "")
        ticket.setdefault("jira_payload", fallback_payload)
        if fallback_payload is not None and not ticket.get("jira_payload"):
            ticket["jira_payload"] = fallback_payload
        if fallback_status is not None and not ticket.get("status"):
            ticket["status"] = fallback_status
        return ticket

    def _ticket_row_for_issue_key(
        self,
        conn: Any,
        issue_key: str,
        *,
        fallback_payload: Any | None = None,
        fallback_status: Any | None = None,
    ) -> dict[str, Any]:
        if not self._table_exists(conn, "tickets") or not self._column_type(conn, "tickets", "jira_ticket_id"):
            return {
                "id": None,
                "jira_ticket_id": issue_key,
                "status": fallback_status or "",
                "jira_payload": fallback_payload,
            }

        row = conn.execute(
            f"""
            SELECT {self._ticket_select_columns(conn)}
            FROM tickets
            WHERE jira_ticket_id = %s
            ORDER BY {self._ticket_order_column(conn)} DESC
            LIMIT 1
            """,
            (issue_key,),
        ).fetchone()
        if row:
            return self._ticket_row(row, fallback_payload=fallback_payload, fallback_status=fallback_status)

        ticket_db_id = None
        try:
            ticket_db_id = self._ensure_ticket_row(conn, issue_key)
        except Exception:
            log.exception("Could not create fallback ticket row for %s", issue_key)

        return {
            "id": ticket_db_id,
            "jira_ticket_id": issue_key,
            "status": fallback_status or "",
            "jira_payload": fallback_payload,
        }

    def _messages_order_column(self, conn: Any) -> str:
        for column in ("created_at", "sent_at", "id"):
            if self._column_type(conn, "messages", column):
                return column
        return "ticket_id"

    def _steps_insert_expression(self, conn: Any) -> str:
        column_type = self._column_type(conn, "test_cases", "steps")
        if column_type == "jsonb":
            return "%s::jsonb"
        if column_type == "json":
            return "%s::json"
        return "%s"

    def _ensure_ticket_row(self, conn: Any, ticket_id: str) -> Any:
        if not self._table_exists(conn, "tickets"):
            raise RuntimeError("tickets table is missing")
        row = conn.execute(
            """
            INSERT INTO tickets (jira_ticket_id)
            VALUES (%s)
            ON CONFLICT (jira_ticket_id)
            DO UPDATE SET jira_ticket_id = EXCLUDED.jira_ticket_id
            RETURNING id
            """,
            (ticket_id,),
        ).fetchone()
        return row.get("id") if row else None

    def _issue_key_from_channel_row(self, row: dict[str, Any] | None) -> str | None:
        if not row:
            return None
        for key in ("issue_key", "key"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        payload = row.get("jira_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if isinstance(payload, dict):
            for key in ("issueKey", "key", "issue_key", "jira_issue_key"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _jira_configured(self) -> bool:
        return bool(self.settings.jira_base_url and self.settings.jira_email and self.settings.jira_api_token)

    def _ticket_context_from_row(self, ticket_row: dict[str, Any]) -> dict[str, Any]:
        payload = self._json_object(ticket_row.get("jira_payload"))
        fields = self._json_object(payload.get("fields")) if payload else {}

        status = payload.get("status") or fields.get("status") or ticket_row.get("status") or ""
        priority = payload.get("priority") or fields.get("priority") or ""
        assignee = payload.get("assignee") or fields.get("assignee") or ""
        reporter = payload.get("reporter") or fields.get("reporter") or ""
        description = (
            payload.get("description")
            or fields.get("description")
            or payload.get("descriptionText")
            or payload.get("body")
            or ""
        )

        ticket_id = str(
            ticket_row.get("jira_ticket_id")
            or payload.get("issueKey")
            or payload.get("key")
            or payload.get("jira_issue_key")
            or ""
        )
        return {
            "issueKey": ticket_id,
            "summary": payload.get("summary") or fields.get("summary") or "",
            "status": self._named_value(status),
            "priority": self._named_value(priority),
            "assignee": self._named_value(assignee),
            "reporter": self._named_value(reporter),
            "dueDate": payload.get("dueDate") or payload.get("duedate") or fields.get("duedate") or "",
            "labels": payload.get("labels") or fields.get("labels") or [],
            "description": self._jira_description_text(description),
            "url": f"{self.settings.jira_base_url}/browse/{ticket_id}" if self.settings.jira_base_url else "",
        }

    def _json_object(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _named_value(self, value: Any) -> str:
        if isinstance(value, dict):
            for key in ("displayName", "name", "value", "key"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
            return ""
        return str(value or "")

    def _jira_description_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value

        parts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                text = node.get("text")
                if isinstance(text, str):
                    parts.append(text)
                for child in node.get("content") or []:
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        if parts:
            return " ".join(part.strip() for part in parts if part.strip())
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)

    def _steps(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return [value]
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
            return [str(parsed)]
        return [str(value)]

    def _normalize_test_cases(self, test_cases: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(test_cases, list):
            return normalized
        for index, item in enumerate(test_cases, start=1):
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "title": str(item.get("title") or f"Test Case {index}"),
                    "steps": self._steps(item.get("steps")),
                    "expected": str(item.get("expected") or ""),
                }
            )
        return normalized

    def _comment_text(self, body: Any) -> str:
        if isinstance(body, str):
            return body
        if isinstance(body, dict):
            return json.dumps(body, ensure_ascii=False)
        return str(body)


def render_comment_body(
    ticket_id: str, test_cases: list[dict[str, Any]], phase: str = "qa"
) -> str:
    """Render Jira wiki markup compatible with the existing n8n comment shape."""
    blocks = []
    for index, test_case in enumerate(test_cases, start=1):
        title = test_case.get("title") or f"Test Case {index}"
        block = f"*[TC {index}] {title}*"
        steps = test_case.get("steps") or []
        if steps:
            numbered = "\n".join(f"  {step_index + 1}. {step}" for step_index, step in enumerate(steps))
            block += f"\n_Steps:_\n{numbered}"
        expected = test_case.get("expected")
        if expected:
            block += f"\n_Expected:_ {expected}"
        blocks.append(block)

    body = "\n\n----\n\n".join(blocks)
    heading = "Developer Test Cases" if _phase(phase) == "dev" else "Test Cases"
    return (
        f"h3. Auto-generated {heading} ({len(test_cases)})\n"
        f"_Generated by AI Governor RepoTree for {ticket_id}._\n"
        f"{{anchor:{_comment_marker(phase)}}}\n\n"
        f"{body}"
    )
