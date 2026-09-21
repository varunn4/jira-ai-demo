"""End-to-end Jira review and Slack thread follow-up orchestration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

from app.config import Settings
from app.conversation_store import ConversationRecord, PostgresConversationStore
from app.graph_context import GraphContextClient
from app.jira_client import JiraClient
from app.json_utils import parse_model_json, review_status, review_text
from app.llm_client import LLMClient
from app.prompt_store import PromptStore
from app.slack_client import SlackClient
from app.ticket_analyzer import TicketAnalyzer


FOLLOW_UP_SYSTEM_PROMPT = """You are a Jira AI reviewer continuing a Slack thread.

You receive:
- original Jira ticket JSON
- previous review JSON
- Slack chat history for this thread
- the user's latest Slack reply
- optional codebase context from repograph

Decide whether the user's reply fixes the missing or weak Jira ticket details.
Use the codebase context only as supporting evidence. Do not invent fields that
the user did not provide. If details are still missing, ask for the exact next
information needed. If the ticket is now good, approve it.

Return only valid JSON:
{
  "status": "good | bad",
  "review": "short Slack-ready review"
}
"""


class SlackReviewWorkflow:
    def __init__(
        self,
        *,
        settings: Settings,
        prompt_store: PromptStore,
        llm_client: LLMClient,
        store: PostgresConversationStore,
        slack_client: SlackClient,
        jira_client: JiraClient,
        graph_client: GraphContextClient,
    ) -> None:
        self.settings = settings
        self.prompt_store = prompt_store
        self.llm_client = llm_client
        self.store = store
        self.slack_client = slack_client
        self.jira_client = jira_client
        self.graph_client = graph_client

    def handle_jira_review(
        self,
        *,
        ticket_data: dict[str, Any],
        slack_channel_id: str | None,
    ) -> dict[str, Any]:
        issue_key = self._ticket_key(ticket_data)
        log.info("Starting Jira review for issue %s", issue_key)
        analyzer = TicketAnalyzer(
            settings=self.settings,
            prompt_store=self.prompt_store,
            llm_client=self.llm_client,
        )
        result = analyzer.analyze(ticket_data=ticket_data, prompt_name=self.settings.default_prompt)
        model_json = parse_model_json(result["model_output"])
        status = review_status(model_json)
        review = review_text(model_json)
        log.info("Review result for %s: status=%s", issue_key, status)

        if status == "good":
            log.info("Issue %s approved; writing Jira comment and transitioning", issue_key)
            jira_update = self._approve_jira(issue_key, review)
            return {
                "jira_issue_key": issue_key,
                "status": status,
                "review": review,
                "slack_thread_ts": None,
                "slack_sent": False,
                "jira_update": jira_update,
                "model_output": model_json,
            }

        channel_id = slack_channel_id or self.settings.slack_default_channel_id
        if not channel_id:
            raise ValueError("A Slack channel id is required when the review needs user follow-up")

        log.info("Issue %s needs follow-up; posting to Slack channel %s", issue_key, channel_id)
        message = self._initial_slack_message(issue_key, review)
        slack_result = self.slack_client.post_message(channel_id=channel_id, text=message)
        self.store.upsert_initial(
            slack_thread_ts=slack_result.thread_ts,
            slack_channel_id=channel_id,
            jira_issue_key=issue_key,
            original_ticket_data=ticket_data,
            previous_review=model_json,
            status="awaiting_reply",
            bot_message=message,
        )
        log.info(
            "Conversation stored for issue %s; thread_ts=%s slack_sent=%s",
            issue_key, slack_result.thread_ts, slack_result.sent,
        )

        return {
            "jira_issue_key": issue_key,
            "status": status,
            "review": review,
            "slack_thread_ts": slack_result.thread_ts,
            "slack_sent": slack_result.sent,
            "jira_update": None,
            "model_output": model_json,
        }

    def handle_slack_reply(
        self,
        *,
        user_id: str,
        channel_id: str,
        thread_ts: str,
        text: str,
        event_ts: str | None = None,
    ) -> dict[str, Any]:
        log.info("Handling Slack reply from user=%s thread=%s", user_id, thread_ts)
        record = self.store.get_by_thread(thread_ts)
        if record is None:
            log.warning("No conversation found for thread=%s; ignoring reply", thread_ts)
            raise ValueError(f"No Jira review conversation found for Slack thread {thread_ts}")

        log.info("Follow-up review for issue=%s", record.jira_issue_key)
        graph_context = self.graph_client.fetch_context(
            ticket_data=record.original_ticket_data,
            user_reply=text,
        )
        model_json = self._review_follow_up(record=record, user_reply=text, graph_context=graph_context)
        status = review_status(model_json)
        review = review_text(model_json)
        log.info("Follow-up review result for issue=%s: status=%s", record.jira_issue_key, status)

        slack_result = self.slack_client.post_message(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=review,
        )

        workflow_status = "completed" if status == "good" else "awaiting_reply"
        self.store.append_message_and_update(
            slack_thread_ts=thread_ts,
            user_message={
                "role": "user",
                "user_id": user_id,
                "text": text,
                "ts": event_ts,
                "created_at": self._now_iso(),
            },
            assistant_message={
                "role": "assistant",
                "text": review,
                "ts": slack_result.message_ts,
                "created_at": self._now_iso(),
            },
            previous_review=model_json,
            status=workflow_status,
        )

        jira_update = None
        if status == "good":
            log.info("Follow-up approved issue=%s; writing Jira comment and transitioning", record.jira_issue_key)
            jira_update = self._approve_jira(record.jira_issue_key, review)

        return {
            "jira_issue_key": record.jira_issue_key,
            "status": status,
            "review": review,
            "slack_thread_ts": thread_ts,
            "slack_sent": slack_result.sent,
            "jira_update": jira_update,
            "model_output": model_json,
        }

    def _review_follow_up(
        self,
        *,
        record: ConversationRecord,
        user_reply: str,
        graph_context: dict[str, Any],
    ) -> dict[str, Any]:
        log.debug("Calling LLM for follow-up review of issue=%s", record.jira_issue_key)
        user_message = {
            "original_ticket_data": record.original_ticket_data,
            "previous_review": record.previous_review,
            "chat_history": record.messages,
            "latest_user_reply": user_reply,
            "graph_context": graph_context,
        }
        raw_output = self.llm_client.complete(
            FOLLOW_UP_SYSTEM_PROMPT,
            json.dumps(user_message, indent=2, ensure_ascii=False),
        )
        return parse_model_json(raw_output)

    def _approve_jira(self, issue_key: str, review: str) -> dict[str, Any]:
        comment_result = self.jira_client.add_comment(issue_key, f"LLM approved: {review}")
        transition_result = self.jira_client.transition_to_approved(issue_key)
        return {
            "comment": comment_result,
            "transition": transition_result,
        }

    def _initial_slack_message(self, issue_key: str, review: str) -> str:
        return f"Jira ticket {issue_key} needs updates before approval: {review}"

    def _ticket_key(self, ticket_data: dict[str, Any]) -> str:
        for key in ("key", "issue_key", "jira_issue_key"):
            value = ticket_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        ticket_identity = ticket_data.get("ticket_identity")
        if isinstance(ticket_identity, dict):
            value = ticket_identity.get("key")
            if isinstance(value, str) and value.strip():
                return value.strip()

        return "UNKNOWN"

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()
