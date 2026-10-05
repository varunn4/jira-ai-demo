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

        # 2b. Check for Direct Ticket Listing / Overview Queries
        ticket_list_reply = self._handle_direct_ticket_list_query(request.user_message)
        if ticket_list_reply:
            if ticket and ticket.get("id"):
                self._save_single_message(str(ticket["id"]), "user", request.user_message)
                self._save_single_message(str(ticket["id"]), "bot", ticket_list_reply)
            return {
                "reply": ticket_list_reply,
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

        # Require an explicit transition verb before touching Jira.
        intent_re = r"(?i)\b(move|transition|change|set|update|mark|shift|put|make|drag)\b"
        if not re.search(intent_re, msg_clean):
            return None
        if re.search(r"(?i)\b(make sure|makes sure|how do i|how to|can you explain|what|generate)\b", msg_clean):
            return None

        # Extract issue key from message or ticket context
        issue_key = None
        match = re.search(r"(?i)\b([a-zA-Z0-9]+-\d+)\b", msg_clean)
        if match:
            issue_key = match.group(1).upper()
        elif ticket and ticket.get("jira_ticket_id"):
            issue_key = ticket["jira_ticket_id"].upper()

        if not issue_key or issue_key in {"GENERAL", "GENERAL TICKET", "N/A"}:
            return None

        # Match specific workflow statuses
        status_map = {
            "in progress - dev": "IN PROGRESS - DEV",
            "in-progress - dev": "IN PROGRESS - DEV",
            "in progress": "IN PROGRESS - DEV",
            "in dev": "IN PROGRESS - DEV",
            "in development": "IN PROGRESS - DEV",
            "in review - qa": "IN REVIEW - QA",
            "in-review - qa": "IN REVIEW - QA",
            "in review": "IN REVIEW - QA",
            "in qa": "IN REVIEW - QA",
            "qa ready": "QA READY",
            "ready for qa": "QA READY",
            "governor approved": "GOVERNOR APPROVED",
            "ai approved": "GOVERNOR APPROVED",
            "approved": "GOVERNOR APPROVED",
            "to do": "TO DO",
            "todo": "TO DO",
            "done": "DONE",
            "closed": "DONE",
            "resolved": "DONE",
        }

        target_status = None
        # 1. Check explicit "status - <target>" pattern
        status_phrase_m = re.search(r"(?i)\b(?:status\s*[-:]?\s*|into\s+|to\s+)([a-zA-Z0-9\s\-]+?)(?:\s+in\s+jira|\s*[\.\!]|\s*$)", msg_clean)
        if status_phrase_m:
            candidate_raw = status_phrase_m.group(1).strip().lower()
            if candidate_raw in status_map:
                target_status = status_map[candidate_raw]
            else:
                for k, v in status_map.items():
                    if k in candidate_raw:
                        target_status = v
                        break
                if not target_status and len(candidate_raw) >= 2:
                    target_status = candidate_raw.title()

        # 2. Check keyword mentions in message
        if not target_status:
            for sk, real_status in status_map.items():
                if re.search(rf"(?i)\b{re.escape(sk)}\b", msg_clean):
                    target_status = real_status
                    break

        if not target_status:
            return None

        # Scope clarification: Direct status updates from Slack back to Jira are disabled.
        # Jira remains the single source of truth for ticket status changes.
        # jc = JiraClient(self.settings)
        # if not jc.is_configured():
        #     return f"Jira credentials are not configured to transition ticket *{issue_key}*."
        # try:
        #     res = jc.transition_to(issue_key, target_status)
        #     ...
        return (
            f"Direct ticket status updates via Slack are disabled. "
            f"Jira remains the single source of truth for ticket status changes. "
            f"Please update the status for *{issue_key}* (to *{target_status}*) directly in Jira Cloud."
        )

    def _fetch_active_tickets_summary(self) -> list[dict[str, Any]]:
        """Fetch real Jira tickets from the local PostgreSQL cache or live sync."""
        if not self.settings.database_url:
            return []
        import psycopg
        from psycopg.rows import dict_row
        try:
            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    """
                    SELECT ticket_key, summary, status, assignee_name, priority
                    FROM jira_ticket_cache
                    ORDER BY updated_at DESC
                    LIMIT 30
                    """
                ).fetchall()
                if not rows:
                    try:
                        from app import jira_fetcher
                        jira_fetcher.fetch_all_tickets(force_refresh=True)
                        rows = conn.execute(
                            """
                            SELECT ticket_key, summary, status, assignee_name, priority
                            FROM jira_ticket_cache
                            ORDER BY updated_at DESC
                            LIMIT 30
                            """
                        ).fetchall()
                    except Exception as sync_exc:
                        LOGGER.warning("workflow2 live fetch fallback failed: %s", sync_exc)
                return [dict(r) for r in rows]
        except Exception as exc:
            LOGGER.warning("Could not fetch active tickets: %s", exc)
            return []

    def _handle_direct_ticket_list_query(self, user_message: str) -> str | None:
        """Instantly answers general queries like 'what are the current tickets' with real Jira data."""
        import re
        clean_msg = user_message.strip().lower()

        # Check for list/overview triggers
        list_triggers = [
            "current ticket", "current tickekt", "available ticket", "open ticket",
            "list ticket", "show ticket", "what are the ticket", "what tickets",
            "all ticket", "active ticket", "my ticket", "check and tell me what are the"
        ]
        if any(t in clean_msg for t in list_triggers):
            tickets = self._fetch_active_tickets_summary()
            if not tickets:
                return "There are currently no tickets found or cached from your connected Jira projects."

            lines = ["*Current Available Jira Tickets:*"]
            for t in tickets[:15]:
                key = t.get("ticket_key", "N/A")
                summary = t.get("summary", "No summary")
                status = t.get("status", "To Do")
                assignee = t.get("assignee_name") or "Unassigned"
                priority = t.get("priority") or "Medium"
                lines.append(f"- *{key}*: {summary}\n  Status: `{status}` | Priority: `{priority}` | Assignee: *{assignee}*")

            if len(tickets) > 15:
                lines.append(f"\n_...and {len(tickets) - 15} more tickets in Jira Cloud._")
            return "\n".join(lines)
        return None

    def _fetch_single_jira_ticket(self, issue_key: str) -> dict[str, Any] | None:
        """Fetch full ticket details directly from Jira Cloud REST API."""
        try:
            import re
            from app.jira_client import JiraClient
            from app.jira_graph import _adf_to_text

            jc = JiraClient(self.settings)
            if not jc.is_configured():
                return None
            issue = jc._request("GET", f"/rest/api/3/issue/{issue_key.strip().upper()}")
            if not issue or not issue.get("fields"):
                return None
            fields = issue["fields"]
            desc_raw = fields.get("description")
            desc_str = _adf_to_text(desc_raw) if isinstance(desc_raw, dict) else str(desc_raw or "")
            summary_str = str(fields.get("summary") or "")
            status_str = str((fields.get("status") or {}).get("name") or "To Do")
            assignee_str = str((fields.get("assignee") or {}).get("displayName") or "Unassigned")
            priority_str = str((fields.get("priority") or {}).get("name") or "Medium")

            repo_str = ""
            repo_m = re.search(r"(?:Linked Repository|Repository):\s*(\S+)", desc_str, re.I)
            if repo_m:
                repo_str = repo_m.group(1).strip()

            ticket_dict = {
                "id": issue_key.upper(),
                "jira_ticket_id": issue_key.upper(),
                "ticket_key": issue_key.upper(),
                "summary": summary_str,
                "description": desc_str,
                "status": status_str,
                "priority": priority_str,
                "assignee_name": assignee_str,
                "github_repo": repo_str,
                "llm_review": "",
            }
            # Cache it into PostgreSQL
            if self.settings.database_url:
                try:
                    import psycopg
                    with psycopg.connect(self.settings.database_url) as conn:
                        conn.execute(
                            """
                            INSERT INTO jira_ticket_cache (ticket_key, project_key, summary, description, status, priority, updated_at, fetched_at)
                            VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                            ON CONFLICT (ticket_key) DO UPDATE SET summary = EXCLUDED.summary, description = EXCLUDED.description, status = EXCLUDED.status, priority = EXCLUDED.priority;
                            """,
                            (issue_key.upper(), issue_key.split("-")[0].upper(), summary_str, desc_str, status_str, priority_str),
                        )
                        conn.commit()
                except Exception:
                    pass
            return ticket_dict
        except Exception as exc:
            LOGGER.warning("workflow2 live fetch for %s failed: %s", issue_key, exc)
            return None

    def _hydrate_ticket_context(self, ticket_key: str, conn: Any = None) -> dict[str, Any]:
        """Load and merge complete ticket metadata from DB cache, tickets table, or live Jira Cloud."""
        import re
        t_key = ticket_key.strip().upper()
        summary = ""
        description = ""
        status = "To Do"
        priority = "Medium"
        assignee_name = "Unassigned"
        github_repo = ""
        llm_review = ""
        codebase_ctx = ""

        if not conn and self.settings.database_url:
            import psycopg
            from psycopg.rows import dict_row
            try:
                with psycopg.connect(self.settings.database_url, row_factory=dict_row) as db_conn:
                    return self._hydrate_ticket_context(t_key, conn=db_conn)
            except Exception as exc:
                LOGGER.warning("workflow2 db hydration connection error: %s", exc)

        if conn:
            try:
                # 1. Look up tickets table
                t_row = conn.execute(
                    "SELECT id, jira_ticket_id, llm_review, status, jira_payload FROM tickets WHERE UPPER(jira_ticket_id) = %s",
                    (t_key,),
                ).fetchone()
                if t_row:
                    llm_review = t_row.get("llm_review") or ""
                    status = t_row.get("status") or status
                    payload = t_row.get("jira_payload") or {}
                    if isinstance(payload, dict):
                        summary = payload.get("summary") or summary
                        description = payload.get("description") or description
                        github_repo = payload.get("github_repo") or github_repo

                # 2. Look up jira_ticket_cache table
                cache_row = conn.execute(
                    """
                    SELECT ticket_key, summary, description, status, priority, assignee_name, data
                    FROM jira_ticket_cache WHERE UPPER(ticket_key) = %s
                    """,
                    (t_key,),
                ).fetchone()
                if cache_row:
                    summary = cache_row.get("summary") or summary
                    description = cache_row.get("description") or description
                    status = cache_row.get("status") or status
                    priority = cache_row.get("priority") or priority
                    assignee_name = cache_row.get("assignee_name") or assignee_name
                    c_data = cache_row.get("data") or {}
                    if isinstance(c_data, dict) and not github_repo:
                        github_repo = c_data.get("github_repo") or ""
            except Exception as exc:
                LOGGER.warning("workflow2 db query error during hydration for %s: %s", t_key, exc)

        # 3. If summary or description is missing, live query Jira Cloud REST API
        if not summary or not description:
            live_t = self._fetch_single_jira_ticket(t_key)
            if live_t:
                summary = live_t.get("summary") or summary
                description = live_t.get("description") or description
                status = live_t.get("status") or status
                priority = live_t.get("priority") or priority
                assignee_name = live_t.get("assignee_name") or assignee_name
                if not github_repo:
                    github_repo = live_t.get("github_repo") or ""

        # 4. Extract repository from description if present
        if not github_repo and description:
            repo_m = re.search(r"(?:Linked Repository|Repository):\s*(\S+)", description, re.I)
            if repo_m:
                github_repo = repo_m.group(1).strip()

        # 5. Extract Codebase Context for deep technical grounding and QA testcase generation
        if github_repo and (summary or description):
            try:
                from app.workflow1_reviewer import Workflow1Reviewer
                reviewer = Workflow1Reviewer(settings=self.settings, prompt_store=self.prompt_store)
                codebase_ctx = reviewer._get_codebase_context(github_repo, summary, description)
            except Exception as cb_exc:
                LOGGER.warning("workflow2 codebase extraction skipped: %s", cb_exc)

        return {
            "id": t_key,
            "jira_ticket_id": t_key,
            "ticket_key": t_key,
            "summary": summary,
            "description": description,
            "status": status,
            "priority": priority,
            "assignee_name": assignee_name,
            "github_repo": github_repo,
            "llm_review": llm_review,
            "codebase_context": codebase_ctx,
        }

    def _find_ticket(self, slack_thread_ts: str, user_message: str = "", user_id: str = "") -> dict[str, Any]:
        if not self.settings.database_url:
            raise RuntimeError("DATABASE_URL is required for workflow2")

        import re
        import psycopg
        from psycopg.rows import dict_row

        try:
            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                # 1. Search for explicit ticket key in user message (e.g. GOV-5, SCRUM-27)
                match = re.search(r"(?i)\b([a-zA-Z0-9]+-\d+)\b", user_message)
                if match:
                    t_key = match.group(1).upper()
                    return self._hydrate_ticket_context(t_key, conn=conn)

                # 2. Search by slack_thread_ts in tickets table
                row = conn.execute(
                    """
                    SELECT jira_ticket_id
                    FROM tickets
                    WHERE slack_thread_ts = %s
                    """,
                    (slack_thread_ts,),
                ).fetchone()
                if row and row.get("jira_ticket_id"):
                    return self._hydrate_ticket_context(row["jira_ticket_id"].upper(), conn=conn)

                # 3. Search by slack_thread_ts in jira_slack_conversations
                conv_row = conn.execute(
                    """
                    SELECT issue_key
                    FROM jira_slack_conversations
                    WHERE slack_thread_ts = %s
                    """,
                    (slack_thread_ts,),
                ).fetchone()
                if conv_row and conv_row.get("issue_key"):
                    return self._hydrate_ticket_context(conv_row["issue_key"].upper(), conn=conn)

                # 4. Fall back to this user's own most recent ticket.
                if not user_id:
                    return {"id": "general", "jira_ticket_id": "General Ticket", "llm_review": ""}

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
                    t_key = mem_row["data"]["last_ticket_key"].upper()
                    return self._hydrate_ticket_context(t_key, conn=conn)
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
            t_code = ticket_context.get("codebase_context") or ""
            t_status = ticket_context.get("status") or t_payload.get("status") or ""

            context_header = (
                f"\n\nContext of the Jira Ticket for this thread:\n"
                f"- Ticket Key: {t_key}\n"
                f"- Summary: {t_summary}\n"
                f"- Status: {t_status}\n"
                f"- Description: {t_desc}\n"
                f"- Connected Repository: {t_repo}\n"
                f"- Initial AI Review / Assessment:\n{t_rev}\n"
            )
            if t_code:
                context_header += f"\n- Relevant Codebase Files & Implementation Context:\n{t_code}\n"

            if t_key in {"N/A", "General Ticket", "general"}:
                active_tickets = self._fetch_active_tickets_summary()
                if active_tickets:
                    tickets_blob = "\n".join(
                        f"  * {t.get('ticket_key')}: {t.get('summary')} (Status: {t.get('status')}, Assignee: {t.get('assignee_name') or 'Unassigned'})"
                        for t in active_tickets[:15]
                    )
                    context_header += f"\nActive Jira Tickets in Workspace:\n{tickets_blob}\n"

        full_system_prompt = base_system_message + context_header
        client = build_llm_client(self.settings)

        convo_lines = []
        for m in messages:
            sender = "Assistant" if m.get("role") == "assistant" else "User"
            convo_lines.append(f"{sender}: {m.get('content', '')}")
        user_message = "\n\n".join(convo_lines) if convo_lines else "Hello"

        raw_reply = client.complete(system_prompt=full_system_prompt, user_message=user_message, max_tokens=2000).strip()
        # Post-processing: enforce zero-emoji rule by stripping emoji Unicode blocks
        import re
        clean_reply = re.sub(r"[\U00010000-\U0010ffff\u2600-\u26ff\u2700-\u27bf]", "", raw_reply).strip()
        return clean_reply

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
