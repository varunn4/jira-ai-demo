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

        LOGGER.info("workflow2 api received request: thread_ts=%s msg_len=%d", request.slack_thread_ts, len(request.user_message))

        # 1. Discover Ticket Context
        ticket = None
        try:
            self._log_step(
                step="2_find_ticket",
                status="started",
                input_data={"slack_thread_ts": request.slack_thread_ts},
            )
            ticket = self._find_ticket(request.slack_thread_ts, request.user_message, request.user_id)
            self._log_step(
                step="2_find_ticket",
                status="completed",
                input_data={"slack_thread_ts": request.slack_thread_ts},
                output=ticket,
            )
            LOGGER.info("workflow2 ticket found: %s", ticket.get("jira_ticket_id"))
        except Exception as find_exc:
            LOGGER.warning("workflow2 ticket discovery fallback: %s", find_exc)

        if not ticket:
            ticket = {"id": "general", "jira_ticket_id": "General Ticket", "llm_review": ""}

        # 2. Check for Direct Jira Action Commands (e.g. status transition)
        status_action_reply = self._handle_direct_status_command(ticket, request.user_message)
        if status_action_reply:
            if ticket and ticket.get("id"):
                self._save_single_message(str(ticket["id"]), "user", request.user_message)
                self._save_single_message(str(ticket["id"]), "bot", status_action_reply)
            return {
                "reply": status_action_reply,
                "slack_thread_ts": request.slack_thread_ts,
                "slack_channel_id": request.slack_channel_id,
            }

        # 3. Process with LLM
        ticket_id = str(ticket.get("id") or ticket.get("jira_ticket_id") or "general")
        try:
            llm_reply = self._save_messages_and_get_reply(
                ticket_id=ticket_id,
                ticket_context=ticket or {},
                user_message=request.user_message,
            )
        except Exception:
            LOGGER.exception("workflow2 LLM generation failed")
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
        LOGGER.info("workflow2 api sending response: %s", llm_reply[:60])
        return response

    def _validate_input(self, request: Workflow2ReplyRequest) -> None:
        for field_name in ("slack_thread_ts", "slack_channel_id", "user_message", "user_id"):
            value = getattr(request, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")

    def _handle_direct_status_command(self, ticket: dict[str, Any] | None, user_message: str) -> str | None:
        """Detect and execute status change commands like 'make this gov-4 ticket into status - In Dev'."""
        import re
        from app.jira_client import JiraClient

        msg_clean = user_message.strip()

        # Require an explicit transition verb before touching Jira. Without this gate,
        # ordinary phrasing like "make sure this is done" matched the keyword scan and
        # silently transitioned a real ticket.
        intent_re = r"(?i)\b(move|transition|change|set|update|mark|shift|put|make)\b"
        if not re.search(intent_re, msg_clean):
            return None
        if re.search(r"(?i)\b(make sure|makes sure|how do i|how to|can you explain|what)\b", msg_clean):
            return None

        # Look for status change keywords
        status_keywords = [
            "in dev",
            "ai approved",
            "ready for qa",
            "in qa",
            "closed",
            "to do",
            "in progress",
            "done",
        ]

        target_status = None
        for sk in status_keywords:
            pattern = rf"(?i)\b(?:status\s*[-:]?\s*|into\s+|to\s+){re.escape(sk)}\b"
            if re.search(pattern, msg_clean) or re.search(rf"(?i)\bmake\s+.*?\b{re.escape(sk)}\b", msg_clean):
                # Standardize title case
                target_status = " ".join(word.capitalize() for word in sk.split())
                if target_status.lower() == "in dev":
                    target_status = "In Dev"
                elif target_status.lower() == "ai approved":
                    target_status = "AI Approved"
                elif target_status.lower() == "ready for qa":
                    target_status = "Ready for QA"
                elif target_status.lower() == "in qa":
                    target_status = "In QA"
                break

        if not target_status:
            return None

        # Extract issue key from ticket context or message
        issue_key = None
        if ticket and ticket.get("jira_ticket_id"):
            issue_key = ticket["jira_ticket_id"]
        else:
            match = re.search(r"(?i)\b([a-zA-Z0-9]+-\d+)\b", msg_clean)
            if match:
                issue_key = match.group(1).upper()

        if not issue_key:
            return None

        jc = JiraClient(self.settings)
        if not jc.is_configured():
            return f"Jira credentials are not configured to transition ticket *{issue_key}*."

        try:
            res = jc.transition_to(issue_key, target_status)
            if res.get("success"):
                # Update local cache
                if self.settings.database_url:
                    try:
                        import psycopg
                        with psycopg.connect(self.settings.database_url) as conn:
                            conn.execute(
                                "UPDATE jira_ticket_cache SET status = %s WHERE UPPER(ticket_key) = %s",
                                (target_status, issue_key.upper()),
                            )
                            conn.commit()
                    except Exception:
                        pass
                return f"Status for ticket *{issue_key}* has been updated to *{target_status}* in Jira Cloud."
            else:
                reason = res.get("reason") or "Transition not allowed in current workflow"
                return f"Could not transition *{issue_key}* to *{target_status}*: {reason}."
        except Exception as exc:
            LOGGER.warning("Direct status transition failed for %s: %s", issue_key, exc)
            return f"Failed to transition *{issue_key}* to *{target_status}*: {str(exc)}."

    def _find_ticket(self, slack_thread_ts: str, user_message: str = "", user_id: str = "") -> dict[str, Any]:
        if not self.settings.database_url:
            raise RuntimeError("DATABASE_URL is required for workflow2")

        import re
        import psycopg
        from psycopg.rows import dict_row

        # 1. Search by slack_thread_ts in tickets table
        try:
            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT id, jira_ticket_id, llm_review, jira_payload
                    FROM tickets
                    WHERE slack_thread_ts = %s
                    """,
                    (slack_thread_ts,),
                ).fetchone()
                if row:
                    return dict(row)

                # 2. Search by slack_thread_ts in jira_slack_conversations
                conv_row = conn.execute(
                    """
                    SELECT issue_key
                    FROM jira_slack_conversations
                    WHERE slack_thread_ts = %s
                    """,
                    (slack_thread_ts,),
                ).fetchone()
                if conv_row and conv_row.get("issue_key"):
                    t_key = conv_row["issue_key"].upper()
                    t_row = conn.execute(
                        "SELECT id, jira_ticket_id, llm_review, jira_payload FROM tickets WHERE UPPER(jira_ticket_id) = %s",
                        (t_key,),
                    ).fetchone()
                    if t_row:
                        return dict(t_row)
                    return {"id": t_key, "jira_ticket_id": t_key, "llm_review": ""}

                # 3. Search for explicit ticket key in user message (e.g. GOV-4, SCRUM-27)
                match = re.search(r"(?i)\b([a-zA-Z0-9]+-\d+)\b", user_message)
                if match:
                    t_key = match.group(1).upper()
                    t_row = conn.execute(
                        "SELECT id, jira_ticket_id, llm_review, jira_payload FROM tickets WHERE UPPER(jira_ticket_id) = %s",
                        (t_key,),
                    ).fetchone()
                    if t_row:
                        return dict(t_row)
                    # Check jira_ticket_cache
                    cache_row = conn.execute(
                        "SELECT ticket_key as jira_ticket_id, summary, description, status FROM jira_ticket_cache WHERE UPPER(ticket_key) = %s",
                        (t_key,),
                    ).fetchone()
                    if cache_row:
                        return dict(cache_row)
                    return {"id": t_key, "jira_ticket_id": t_key, "llm_review": ""}

                # 4. Fall back to this user's own most recent ticket.
                # Scoped to user_id: an unscoped "latest row" lookup handed one
                # person's thread the context of somebody else's ticket.
                if not user_id:
                    return {"id": "general", "jira_ticket_id": "General Ticket", "llm_review": ""}

                # Slack sends a user id (U…), while the writers key user_memory by
                # email. Accept either, resolving through channelid_table when it maps.
                identifiers = [user_id.strip()]
                try:
                    id_row = conn.execute(
                        """
                        SELECT email_id FROM channelid_table
                        WHERE slack_user_id = %s OR slack_user_name = %s
                        LIMIT 1
                        """,
                        (user_id.strip(), user_id.strip()),
                    ).fetchone()
                    if id_row and id_row.get("email_id"):
                        identifiers.append(id_row["email_id"].strip())
                except Exception as id_exc:
                    LOGGER.warning("workflow2 could not resolve slack user %s: %s", user_id, id_exc)

                mem_row = conn.execute(
                    """
                    SELECT data FROM user_memory
                    WHERE context_type = 'recent_ticket'
                      AND LOWER(user_identifier) = ANY(%s)
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    ([i.lower() for i in identifiers],),
                ).fetchone()
                if mem_row and mem_row.get("data") and mem_row["data"].get("last_ticket_key"):
                    t_key = mem_row["data"]["last_ticket_key"]
                    return {"id": t_key, "jira_ticket_id": t_key, "llm_review": ""}
        except Exception as exc:
            LOGGER.warning("workflow2 _find_ticket lookup error: %s", exc)

        return {"id": "general", "jira_ticket_id": "General Ticket", "llm_review": ""}

    def _save_single_message(self, ticket_id: str, sender: str, message: str) -> None:
        if not self.settings.database_url:
            return
        try:
            import psycopg
            with psycopg.connect(self.settings.database_url) as conn:
                conn.execute(
                    "INSERT INTO messages (ticket_id, sender, message) VALUES (%s, %s, %s)",
                    (ticket_id, sender, message),
                )
                conn.commit()
        except Exception as exc:
            LOGGER.warning("Could not save single message: %s", exc)

    def _save_messages_and_get_reply(
        self,
        *,
        ticket_id: str,
        ticket_context: dict[str, Any],
        user_message: str,
    ) -> str:
        if not self.settings.database_url:
            raise RuntimeError("DATABASE_URL is required for workflow2")

        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
            # 1. Save user message
            conn.execute(
                "INSERT INTO messages (ticket_id, sender, message) VALUES (%s, 'user', %s)",
                (ticket_id, user_message),
            )
            # 2. Fetch full conversation history
            rows = conn.execute(
                "SELECT sender, message FROM messages WHERE ticket_id = %s ORDER BY id ASC LIMIT 20",
                (ticket_id,),
            ).fetchall()
            chat_history = [dict(r) for r in rows]

            # 3. Build messages with ticket metadata context
            messages = self._build_claude_messages(chat_history)
            llm_reply = self._call_claude(messages, ticket_context)

            # 4. Save bot reply
            conn.execute(
                "INSERT INTO messages (ticket_id, sender, message) VALUES (%s, 'bot', %s)",
                (ticket_id, llm_reply),
            )
            conn.commit()
            return llm_reply

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

    def _call_claude(self, messages: list[dict[str, str]], ticket_context: dict[str, Any]) -> str:
        from app.llm_client import build_llm_client

        base_system_message = self.prompt_store.load("workflow2_prompt")

        # Context Header Injection
        context_header = ""
        if ticket_context:
            t_key = ticket_context.get("jira_ticket_id") or ticket_context.get("ticket_key") or "N/A"
            t_rev = ticket_context.get("llm_review") or ""
            t_payload = ticket_context.get("jira_payload") or {}
            t_summary = ticket_context.get("summary") or t_payload.get("summary") or ""
            t_desc = ticket_context.get("description") or t_payload.get("description") or ""
            t_repo = ticket_context.get("github_repo") or t_payload.get("github_repo") or ""

            context_header = (
                f"\n\nContext of the Jira Ticket for this thread:\n"
                f"- Ticket Key: {t_key}\n"
                f"- Summary: {t_summary}\n"
                f"- Description: {t_desc}\n"
                f"- Connected Repository: {t_repo}\n"
                f"- Initial AI Review / Assessment:\n{t_rev}\n"
            )

        full_system_prompt = base_system_message + context_header
        client = build_llm_client(self.settings)

        convo_lines = []
        for m in messages:
            sender = "Assistant" if m.get("role") == "assistant" else "User"
            convo_lines.append(f"{sender}: {m.get('content', '')}")
        user_message = "\n\n".join(convo_lines) if convo_lines else "Hello"

        return client.complete(system_prompt=full_system_prompt, user_message=user_message, max_tokens=1500).strip()

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
