"""Small Jira Cloud REST client for comments and workflow transitions."""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from app.config import Settings

log = logging.getLogger(__name__)


class JiraClient:
    def __init__(self, settings: Settings, overrides: Optional[dict[str, Any]] = None) -> None:
        self.settings = settings
        self._overrides = overrides or {}

    @property
    def base_url(self) -> str:
        url = (self._overrides.get("jira_base_url") or self.settings.jira_base_url or "").strip().rstrip("/")
        if url and not (url.startswith("http://") or url.startswith("https://")):
            url = f"https://{url}"
        return url

    @property
    def email(self) -> str:
        return (self._overrides.get("jira_email") or self.settings.jira_email or "").strip()

    @property
    def api_token(self) -> str:
        return (self._overrides.get("jira_api_token") or self.settings.jira_api_token or "").strip()

    def is_configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)

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
        """Return the issue's comments (newest first by default)."""
        if not self.is_configured():
            log.warning("Jira not configured; returning no comments for %s (dry run)", issue_key)
            return []
        data = self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}/comment",
            params={"orderBy": order_by, "maxResults": max_results},
        )
        return data.get("comments", []) if isinstance(data, dict) else []

    def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        if not self.is_configured():
            return []
        data = self._request("GET", f"/rest/api/3/issue/{issue_key}/transitions")
        return data.get("transitions", []) if isinstance(data, dict) else []

    def transition_issue(self, issue_key: str, target_status_name: str) -> dict[str, Any]:
        """Transition an issue by target status or transition name (case-insensitive fuzzy match)."""
        if not self.is_configured():
            log.warning("Jira not configured; skipping transition for %s", issue_key)
            return {"skipped": True, "reason": "Jira credentials are not configured"}

        target_name = (target_status_name or "").strip().lower()
        if not target_name:
            return {"skipped": True, "reason": "No target transition name provided"}

        transitions = self.get_transitions(issue_key)
        matched = None
        for t in transitions:
            t_name = str(t.get("name") or "").lower()
            to_name = str((t.get("to") or {}).get("name") or "").lower()
            if t_name == target_name or to_name == target_name or target_name in t_name or target_name in to_name:
                matched = t
                break

        if not matched:
            available = [f"{t.get('name')} (to: {(t.get('to') or {}).get('name')})" for t in transitions]
            log.warning("Transition '%s' not found for %s. Available: %s", target_status_name, issue_key, available)
            return {
                "skipped": True,
                "reason": f"Transition '{target_status_name}' not available. Available: {available}",
                "available": available,
            }

        log.info("Transitioning issue %s via '%s' (id=%s)", issue_key, matched.get("name"), matched.get("id"))
        res = self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": matched["id"]}},
        )
        return {"success": True, "transition": matched.get("name"), "issue_key": issue_key, "raw": res}

    def transition_to_approved(self, issue_key: str) -> dict[str, Any]:
        """Try matching AI Approved, Approved, or configured approved transition name."""
        candidates = [
            self.settings.jira_approved_transition_name,
            "AI Approved",
            "AI APPROVED",
            "Approved",
            "APPROVED",
            "LLM APPROVED",
            "In Review",
            "In Progress",
        ]
        transitions = self.get_transitions(issue_key)
        for cand in candidates:
            if not cand:
                continue
            cand_clean = cand.strip().lower()
            for t in transitions:
                t_name = str(t.get("name") or "").lower()
                to_name = str((t.get("to") or {}).get("name") or "").lower()
                if t_name == cand_clean or to_name == cand_clean or cand_clean in t_name or cand_clean in to_name:
                    return self.transition_issue(issue_key, t.get("name"))
        return self.transition_issue(issue_key, candidates[0] or "AI Approved")

    def transition_to(self, issue_key: str, status_name: str) -> dict[str, Any]:
        """Transition an issue to any lifecycle status (Created, AI Approved, In Dev, Ready for QA, In QA, Closed)."""
        return self.transition_issue(issue_key, status_name)

    def create_ticket(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        assignee_id: str | None = None,
        priority_name: str | None = None,
        github_repo_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a top-level Jira issue in Jira Cloud."""
        if not self.is_configured():
            return {"dry_run": True, "reason": "Jira credentials are not configured"}

        full_desc = description.strip()
        if github_repo_url and github_repo_url.strip():
            repo_block = f"\n\n**Linked Repository:** {github_repo_url.strip()}"
            if repo_block not in full_desc:
                full_desc += repo_block

        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": project_key.strip().upper()},
                "summary": summary.strip()[:250],
                "issuetype": {"name": issue_type.strip() or "Task"},
            }
        }

        if full_desc:
            payload["fields"]["description"] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": full_desc}],
                    }
                ],
            }

        if assignee_id and assignee_id.strip():
            payload["fields"]["assignee"] = {"accountId": assignee_id.strip()}

        if priority_name and priority_name.strip():
            payload["fields"]["priority"] = {"name": priority_name.strip()}

        try:
            res = self._request("POST", "/rest/api/3/issue", json=payload)
            log.info("Created Jira ticket %s in project %s", res.get("key"), project_key)
            return res
        except Exception as exc:
            log.exception("Failed to create Jira ticket in %s: %s", project_key, exc)
            raise

    def create_subtask(
        self,
        project_key: str,
        parent_key: str,
        summary: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a Sub-task under a parent issue in Jira Cloud."""
        if not self.is_configured():
            return {"dry_run": True, "reason": "Jira credentials are not configured"}

        payload = {
            "fields": {
                "project": {"key": project_key},
                "parent": {"key": parent_key},
                "summary": summary[:250],
                "issuetype": {"name": "Sub-task"},
            }
        }
        if description:
            payload["fields"]["description"] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            }

        try:
            res = self._request("POST", "/rest/api/3/issue", json=payload)
            log.info("Created subtask %s under parent %s", res.get("key"), parent_key)
            return res
        except Exception as exc:
            log.warning("Failed to create subtask with 'Sub-task' type: %s. Trying with subtask ID fallback...", exc)
            # Fallback trying 'Subtask' name without hyphen
            payload["fields"]["issuetype"] = {"name": "Subtask"}
            return self._request("POST", "/rest/api/3/issue", json=payload)

    def create_subtasks(
        self,
        parent_key: str,
        subtasks: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Create multiple subtasks under a parent issue."""
        if not subtasks or not parent_key:
            return []
        project_key = parent_key.rsplit("-", 1)[0] if "-" in parent_key else parent_key
        created: list[dict[str, Any]] = []
        for item in subtasks:
            summary = item.get("summary") or item.get("title") or "Subtask"
            desc = item.get("description") or item.get("details") or ""
            try:
                sub_res = self.create_subtask(project_key, parent_key, summary, desc)
                if sub_res and sub_res.get("key"):
                    created.append({"key": sub_res["key"], "summary": summary})
            except Exception as exc:
                log.warning("Failed creating subtask '%s' for %s: %s", summary, parent_key, exc)
        return created

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.is_configured():
            log.warning("Jira not configured; returning dry_run for %s %s", method, path)
            return {"dry_run": True, "reason": "Jira credentials are not configured"}

        log.debug("Jira API %s %s", method, path)
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            auth=HTTPBasicAuth(self.email, self.api_token),
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
