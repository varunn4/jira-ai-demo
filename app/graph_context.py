"""Fetch lightweight codebase context from the local repograph HTTP service."""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from app.config import Settings

log = logging.getLogger(__name__)


STOP_WORDS = {
    "about",
    "after",
    "again",
    "before",
    "description",
    "issue",
    "jira",
    "missing",
    "please",
    "reply",
    "should",
    "ticket",
    "update",
    "user",
    "with",
}


class GraphContextClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def fetch_context(self, *, ticket_data: dict[str, Any], user_reply: str = "") -> dict[str, Any]:
        query_text = self._query_text(ticket_data, user_reply)
        log.info("Fetching graph context (query_chars=%d)", len(query_text))
        context: dict[str, Any] = {"query": query_text, "items": [], "errors": []}

        ask_result = self._post_ask(query_text)
        if ask_result:
            context["items"].append({"source": "repograph.ask", "data": ask_result})
            log.debug("repograph.ask returned a result")

        keywords = self._keywords(query_text)[:5]
        log.debug("Querying files_touched_recently for keywords: %s", keywords)
        for keyword in keywords:
            touched = self._get_files_touched(keyword)
            if touched:
                context["items"].append(
                    {
                        "source": "repograph.files_touched_recently",
                        "keyword": keyword,
                        "data": touched[:10],
                    }
                )

        log.info("Graph context: %d items collected", len(context["items"]))
        return context

    def _post_ask(self, question: str) -> dict[str, Any] | None:
        try:
            response = requests.post(
                f"{self.settings.repograph_base_url}/ask",
                json={"question": question},
                timeout=self.settings.external_request_timeout_seconds,
            )
            if response.status_code == 501:
                log.debug("repograph /ask returned 501 (not implemented); skipping")
                return None
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            log.warning("repograph /ask request failed: %s", exc)
            return {"unavailable": True, "error": str(exc)}

    def _get_files_touched(self, keyword: str) -> list[dict[str, Any]]:
        try:
            response = requests.get(
                f"{self.settings.repograph_base_url}/files/touched_recently",
                params={"keyword": keyword, "days": 180, "limit": 10},
                timeout=self.settings.external_request_timeout_seconds,
            )
            if response.status_code in {404, 501}:
                return []
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []
        except requests.RequestException:
            return []

    def _query_text(self, ticket_data: dict[str, Any], user_reply: str) -> str:
        summary = ticket_data.get("summary") or ticket_data.get("title") or ""
        description = ticket_data.get("description") or ticket_data.get("description_text") or ""
        components = ticket_data.get("components") or []
        labels = ticket_data.get("labels") or []

        return "\n".join(
            [
                f"summary: {summary}",
                f"description: {description}",
                f"components: {components}",
                f"labels: {labels}",
                f"user_reply: {user_reply}",
            ]
        )

    def _keywords(self, text: str) -> list[str]:
        words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
        seen: set[str] = set()
        keywords = []
        for word in words:
            if word in STOP_WORDS or word in seen:
                continue
            seen.add(word)
            keywords.append(word)
        return keywords
