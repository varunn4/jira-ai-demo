"""Core orchestration service for analyzing Jira ticket metadata.

This file connects prompt loading with the configured LLM client. It chooses
the requested or default system prompt, sends the ticket JSON separately as a
user message, calls the model, and returns a consistent result object.
"""

import json
import logging
from typing import Any, Dict, Optional

from app.config import Settings
from app.llm_client import LLMClient
from app.prompt_store import PromptStore

log = logging.getLogger(__name__)


class TicketAnalyzer:
    def __init__(
        self,
        settings: Settings,
        prompt_store: PromptStore,
        llm_client: LLMClient,
    ) -> None:
        self.settings = settings
        self.prompt_store = prompt_store
        self.llm_client = llm_client

    def analyze(self, ticket_data: Dict[str, Any], prompt_name: Optional[str] = None) -> Dict[str, Any]:
        selected_prompt = prompt_name or self.settings.default_prompt
        ticket_key = ticket_data.get("key") or ticket_data.get("issue_key") or "unknown"
        log.info("Analyzing ticket %s with prompt '%s'", ticket_key, selected_prompt)
        system_prompt = self.prompt_store.load(selected_prompt)
        user_message = self._build_user_message(ticket_data)
        model_output = self.llm_client.complete(system_prompt, user_message)
        log.info("LLM analysis complete for ticket %s: output_chars=%d", ticket_key, len(model_output))

        return {
            "prompt_name": selected_prompt,
            "model_output": model_output,
        }

    def _build_user_message(self, ticket_data: Dict[str, Any]) -> str:
        ticket_json = json.dumps(ticket_data, indent=2, ensure_ascii=False)

        return f"Validate this Jira ticket JSON.\n\nTICKET_JSON:\n{ticket_json}"
