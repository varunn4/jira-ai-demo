"""Workflow2 Slack thread follow-up flow."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
from app.prompt_store import PromptStore
from app.schemas import Workflow2ReplyRequest


LOGGER = logging.getLogger(__name__)


class Workflow2Replier:
    def __init__(self, *, settings: Settings, prompt_store: PromptStore) -> None:
        self.settings = settings
        self.prompt_store = prompt_store

    def reply(self, request: Workflow2ReplyRequest) -> dict[str, str]:
        request_payload = request.model_dump()
        self._log_step(
            step="api_request",
            status="received",
            output=request_payload,
        )

        try:
            self._log_step(
                step="1_validate_input",
                status="started",
                input_data=request_payload,
            )
            self._validate_input(request)
            self._log_step(
                step="1_validate_input",
                status="completed",
                input_data=request_payload,
                output={"valid": True},
            )
        except Exception:
            self._log_step(
                step="1_validate_input",
                status="failed",
                input_data=request_payload,
            )
            LOGGER.exception("workflow2 step 1 failed: validate input")
            raise

        LOGGER.info("workflow2 api received request: %s", request.slack_thread_ts)

        try:
            self._log_step(
                step="2_find_ticket",
                status="started",
                input_data={"slack_thread_ts": request.slack_thread_ts},
            )
            ticket = self._find_ticket(request.slack_thread_ts)
            self._log_step(
                step="2_find_ticket",
                status="completed",
                input_data={"slack_thread_ts": request.slack_thread_ts},
                output=ticket,
            )
            LOGGER.info("workflow2 ticket found: %s", ticket["jira_ticket_id"])
        except Exception:
            self._log_step(
                step="2_find_ticket",
                status="failed",
                input_data={"slack_thread_ts": request.slack_thread_ts},
            )
            LOGGER.exception("workflow2 step 2 failed: find ticket in DB")
            raise

        try:
            llm_reply = self._save_messages_and_get_reply(
                ticket_id=str(ticket["id"]),
                user_message=request.user_message,
            )
        except Exception:
            LOGGER.exception("workflow2 steps 3-7 failed: save messages and call Claude")
            raise

        response = {
            "reply": llm_reply,
            "slack_thread_ts": request.slack_thread_ts,
            "slack_channel_id": request.slack_channel_id,
        }
        self._log_step(
            step="8_return_response",
            status="completed",
            input_data={"llm_reply": llm_reply},
            output=response,
        )
        LOGGER.info("workflow2 api sending response: %s", llm_reply[:50])
        return response

    def _validate_input(self, request: Workflow2ReplyRequest) -> None:
        for field_name in ("slack_thread_ts", "slack_channel_id", "user_message", "user_id"):
            value = getattr(request, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")

    def _find_ticket(self, slack_thread_ts: str) -> dict[str, Any]:
        if not self.settings.database_url:
            raise RuntimeError("DATABASE_URL is required for workflow2")

        row = None
        try:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT id, jira_ticket_id, llm_review
                    FROM tickets
                    WHERE slack_thread_ts = %s
                    """,
                    (slack_thread_ts,),
                ).fetchone()
        except Exception:
            try:
                import psycopg2
                from psycopg2.extras import RealDictCursor
                with psycopg2.connect(self.settings.database_url) as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                        cursor.execute(
                            """
                            SELECT id, jira_ticket_id, llm_review
                            FROM tickets
                            WHERE slack_thread_ts = %s
                            """,
                            (slack_thread_ts,),
                        )
                        row = cursor.fetchone()
            except Exception as exc:
                LOGGER.warning("workflow2 _find_ticket fallback: %s", exc)

        if not row:
            self._log_step(
                step="2_find_ticket_db_query",
                status="completed",
                input_data={"slack_thread_ts": slack_thread_ts},
                output={"ticket_found": False},
            )
            raise LookupError("Ticket not found for this thread")

        ticket = dict(row)
        self._log_step(
            step="2_find_ticket_db_query",
            status="completed",
            input_data={"slack_thread_ts": slack_thread_ts},
            output=ticket,
        )
        return ticket

    def _save_messages_and_get_reply(self, *, ticket_id: str, user_message: str) -> str:
        if not self.settings.database_url:
            raise RuntimeError("DATABASE_URL is required for workflow2")

        try:
            import psycopg
            from psycopg.rows import dict_row
            use_psycopg3 = True
        except ImportError:
            use_psycopg3 = False

        if use_psycopg3:
            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                conn.execute(
                    "INSERT INTO messages (ticket_id, sender, message) VALUES (%s, 'user', %s)",
                    (ticket_id, user_message),
                )
                rows = conn.execute(
                    "SELECT sender, message FROM messages WHERE ticket_id = %s ORDER BY sent_at ASC",
                    (ticket_id,),
                ).fetchall()
                chat_history = [dict(r) for r in rows]
                messages = self._build_claude_messages(chat_history)
                llm_reply = self._call_claude(messages)
                conn.execute(
                    "INSERT INTO messages (ticket_id, sender, message) VALUES (%s, 'bot', %s)",
                    (ticket_id, llm_reply),
                )
                conn.commit()
                return llm_reply

        with psycopg2.connect(self.settings.database_url) as conn:
            try:
                self._log_step(
                    step="transaction_steps_3_to_7",
                    status="started",
                    input_data={"ticket_id": ticket_id},
                )
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    try:
                        step_input = {
                            "ticket_id": ticket_id,
                            "sender": "user",
                            "message": user_message,
                        }
                        self._log_step(
                            step="3_save_user_message",
                            status="started",
                            input_data=step_input,
                        )
                        cursor.execute(
                            """
                            INSERT INTO messages (ticket_id, sender, message)
                            VALUES (%s, 'user', %s)
                            """,
                            (ticket_id, user_message),
                        )
                        self._log_step(
                            step="3_save_user_message",
                            status="completed",
                            input_data=step_input,
                            output={"saved": True, "rowcount": cursor.rowcount},
                        )
                        LOGGER.info("workflow2 user message saved")
                    except Exception:
                        self._log_step(
                            step="3_save_user_message",
                            status="failed",
                            input_data={"ticket_id": ticket_id, "sender": "user"},
                        )
                        LOGGER.exception("workflow2 step 3 failed: save user message to DB")
                        raise

                    try:
                        self._log_step(
                            step="4_fetch_chat_history",
                            status="started",
                            input_data={"ticket_id": ticket_id},
                        )
                        cursor.execute(
                            """
                            SELECT sender, message
                            FROM messages
                            WHERE ticket_id = %s
                            ORDER BY sent_at ASC
                            """,
                            (ticket_id,),
                        )
                        chat_history = [dict(row) for row in cursor.fetchall()]
                        self._log_step(
                            step="4_fetch_chat_history",
                            status="completed",
                            input_data={"ticket_id": ticket_id},
                            output={
                                "count": len(chat_history),
                                "messages": chat_history,
                            },
                        )
                        LOGGER.info("workflow2 chat history fetched: %s messages", len(chat_history))
                    except Exception:
                        self._log_step(
                            step="4_fetch_chat_history",
                            status="failed",
                            input_data={"ticket_id": ticket_id},
                        )
                        LOGGER.exception("workflow2 step 4 failed: fetch full chat history")
                        raise

                    try:
                        self._log_step(
                            step="5_build_claude_messages",
                            status="started",
                            input_data={"chat_history": chat_history},
                        )
                        messages = self._build_claude_messages(chat_history)
                        self._log_step(
                            step="5_build_claude_messages",
                            status="completed",
                            input_data={"chat_history_count": len(chat_history)},
                            output={"messages": messages},
                        )
                    except Exception:
                        self._log_step(
                            step="5_build_claude_messages",
                            status="failed",
                            input_data={"chat_history_count": len(chat_history)},
                        )
                        LOGGER.exception("workflow2 step 5 failed: build LLM messages array")
                        raise

                    try:
                        self._log_step(
                            step="6_call_claude",
                            status="started",
                            input_data={
                                "model": "claude-opus-4-5",
                                "max_tokens": 1024,
                                "prompt_name": "workflow2_prompt",
                                "messages": messages,
                            },
                        )
                        llm_reply = self._call_claude(messages)
                        self._log_step(
                            step="6_call_claude",
                            status="completed",
                            output={"llm_reply": llm_reply},
                        )
                        LOGGER.info("workflow2 claude response received")
                    except Exception:
                        self._log_step(
                            step="6_call_claude",
                            status="failed",
                            input_data={"message_count": len(messages)},
                        )
                        LOGGER.exception("workflow2 step 6 failed: call Claude API")
                        raise

                    try:
                        step_input = {
                            "ticket_id": ticket_id,
                            "sender": "bot",
                            "message": llm_reply,
                        }
                        self._log_step(
                            step="7_save_bot_reply",
                            status="started",
                            input_data=step_input,
                        )
                        cursor.execute(
                            """
                            INSERT INTO messages (ticket_id, sender, message)
                            VALUES (%s, 'bot', %s)
                            """,
                            (ticket_id, llm_reply),
                        )
                        self._log_step(
                            step="7_save_bot_reply",
                            status="completed",
                            input_data=step_input,
                            output={"saved": True, "rowcount": cursor.rowcount},
                        )
                        LOGGER.info("workflow2 bot reply saved")
                    except Exception:
                        self._log_step(
                            step="7_save_bot_reply",
                            status="failed",
                            input_data={"ticket_id": ticket_id, "sender": "bot"},
                        )
                        LOGGER.exception("workflow2 step 7 failed: save bot reply to DB")
                        raise

                conn.commit()
                self._log_step(
                    step="transaction_steps_3_to_7",
                    status="committed",
                    output={"ticket_id": ticket_id},
                )
                return llm_reply
            except Exception:
                conn.rollback()
                self._log_step(
                    step="transaction_steps_3_to_7",
                    status="rolled_back",
                    output={"ticket_id": ticket_id},
                )
                raise

    def _build_claude_messages(self, chat_history: list[dict[str, Any]]) -> list[dict[str, str]]:
        messages = []
        for row in chat_history:
            role = "assistant" if row.get("sender") == "bot" else "user"
            messages.append(
                {
                    "role": role,
                    "content": str(row.get("message") or ""),
                }
            )
        return messages

    def _call_claude(self, messages: list[dict[str, str]]) -> str:
        from app.llm_client import build_llm_client

        system_message = self.prompt_store.load("workflow2_prompt")
        client = build_llm_client(self.settings)

        convo_lines = []
        for m in messages:
            sender = "Assistant" if m.get("role") == "assistant" else "User"
            convo_lines.append(f"{sender}: {m.get('content', '')}")
        user_message = "\n\n".join(convo_lines) if convo_lines else "Hello"

        return client.complete(system_prompt=system_message, user_message=user_message, max_tokens=1024).strip()

    def _log_step(
        self,
        *,
        step: str,
        status: str,
        input_data: Any | None = None,
        output: Any | None = None,
    ) -> None:
        log_payload = {
            "step": step,
            "status": status,
            "input": input_data,
            "output": output,
        }
        LOGGER.info("workflow2 structured log: %s", json.dumps(log_payload, ensure_ascii=False, default=str))
