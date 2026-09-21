"""Small Jira Cloud REST client for comments and workflow transitions."""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from app.config import Settings

log = logging.getLogger(__name__)


class JiraClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.jira_base_url and self.settings.jira_email and self.settings.jira_api_token)

    def add_comment(self, issue_key: str, text: str) -> dict[str, Any]:
        if not self.is_configured():
            log.warning("Jira not configured; skipping add_comment for %s (dry run)", issue_key)
            return {"dry_run": True, "reason": "Jira credentials are not configured"}

        log.info("Adding comment to Jira issue %s", issue_key)
        return self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/comment",
            json={
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": text}],
                        }
                    ],
                }
            },
        )

    def get_comments(self, issue_key: str, order_by: str = "-created", max_results: int = 50) -> list[dict[str, Any]]:
        """Return the issue's comments (newest first by default).

        Each item is the raw Jira comment object; the plain-text body is
        reconstructed by callers via :func:`app.github_client.comment_plain_text`.
        """
        if not self.is_configured():
            log.warning("Jira not configured; returning no comments for %s (dry run)", issue_key)
            return []
        data = self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}/comment",
            params={"orderBy": order_by, "maxResults": max_results},
        )
        return data.get("comments", []) if isinstance(data, dict) else []

    def transition_to_approved(self, issue_key: str) -> dict[str, Any]:
        if not self.settings.jira_approved_transition_name:
            log.warning("JIRA_APPROVED_TRANSITION_NAME not set; skipping transition for %s", issue_key)
            return {"skipped": True, "reason": "JIRA_APPROVED_TRANSITION_NAME is not configured"}

        log.info("Looking up transitions for Jira issue %s", issue_key)
        transitions = self._request("GET", f"/rest/api/3/issue/{issue_key}/transitions")
        target = None
        for transition in transitions.get("transitions", []):
            if transition.get("name", "").lower() == self.settings.jira_approved_transition_name.lower():
                target = transition
                break

        if not target:
            log.warning(
                "Transition '%s' not found for issue %s; available: %s",
                self.settings.jira_approved_transition_name,
                issue_key,
                [t.get("name") for t in transitions.get("transitions", [])],
            )
            return {
                "skipped": True,
                "reason": f"Transition '{self.settings.jira_approved_transition_name}' not found",
            }

        log.info(
            "Transitioning issue %s via '%s' (id=%s)",
            issue_key,
            target.get("name"),
            target.get("id"),
        )
        return self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": target["id"]}},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.is_configured():
            log.warning("Jira not configured; returning dry_run for %s %s", method, path)
            return {"dry_run": True, "reason": "Jira credentials are not configured"}

        log.debug("Jira API %s %s", method, path)
        response = requests.request(
            method,
            f"{self.settings.jira_base_url}{path}",
            auth=HTTPBasicAuth(self.settings.jira_email, self.settings.jira_api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.settings.external_request_timeout_seconds,
            **kwargs,
        )
        if response.status_code == 204:
            log.debug("Jira API %s %s → 204 No Content", method, path)
            return {"ok": True}
        response.raise_for_status()
        log.debug("Jira API %s %s → %d", method, path, response.status_code)
        return response.json() if response.content else {"ok": True}
