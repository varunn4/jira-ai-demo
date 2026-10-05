"""Workflow1 Jira review flow for n8n payloads."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from dateutil import parser as dateutil_parser

from app.config import Settings
from app.prompt_store import PromptStore
from app.schemas import Workflow1ReviewRequest


LOGGER = logging.getLogger(__name__)
VALID_NATURES = {"satisfied", "unsatisfied"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3", "P4"}
JIRA_PRIORITY_TO_P = {
    "HIGHEST": "P0", "CRITICAL": "P0", "BLOCKER": "P0",
    "HIGH": "P1", "MAJOR": "P1",
    "MEDIUM": "P2", "NORMAL": "P2",
    "LOW": "P3", "MINOR": "P3",
    "LOWEST": "P4", "TRIVIAL": "P4",
}


@dataclass(frozen=True)
class SlackUserMatch:
    channel_id: str
    email: str | None
    user_id: str | None


class Workflow1Reviewer:
    def __init__(self, *, settings: Settings, prompt_store: PromptStore) -> None:
        self.settings = settings
        self.prompt_store = prompt_store

    def review(self, request: Workflow1ReviewRequest) -> dict[str, Any]:
        payload = request.model_dump()
        LOGGER.info("workflow1 started with payload: %s", self._to_log_json(payload))

        try:
            LOGGER.info("workflow1 step started: validate_input")
            self._validate_input(request)
            LOGGER.info(
                "workflow1 step completed: validate_input issueKey=%s summary_length=%s",
                request.issueKey,
                len(request.summary),
            )
        except Exception:
            LOGGER.exception("workflow1 step failed: validate_input")
            raise

        # ── Mandatory Field & Repository Extraction ──
        repo_url, repo_pat = self._extract_repo_url(payload)
        payload["github_repo"] = repo_url or "None provided"

        missing_fields: list[str] = []

        # 1. Assignee Validation
        assignee_val = str(request.assignee or "").strip()
        if not assignee_val or assignee_val.lower() in {"unassigned", "none", "null", ""}:
            missing_fields.append("Assignee is required (currently Unassigned). Please assign a developer in Jira.")

        # 2. Priority Validation
        priority_val = str(request.priority or "").strip()
        if not priority_val or priority_val.lower() in {"none", "null", ""}:
            missing_fields.append("Priority is required (P0 - P4). Please set the ticket priority in Jira.")
        else:
            # Jira sends names (Highest/High/Medium/Low/Lowest); the governor works on the P0-P4 scale.
            priority_val = JIRA_PRIORITY_TO_P.get(priority_val.upper(), priority_val)
            payload["priority"] = priority_val

        # 2b. Target TAT (Turnaround Time) / Original Estimate Validation
        estimated_val = str(payload.get("estimatedTime") or payload.get("estimated_time") or "").strip()
        if not estimated_val:
            # Check if Jira timetracking or description contains an estimate or TAT pattern
            desc_text = request.description or ""
            if not any(k in desc_text for k in ["Target TAT", "TAT:", "Original Estimate", "Estimate:"]):
                missing_fields.append("Target TAT / Original Estimate is required (e.g. '8h', '16h', '2d') for SLA and effort tracking.")
        payload["estimatedTime"] = estimated_val or "Not provided"

        # 3. GitHub Repository Validation & Access Check
        if not repo_url:
            missing_fields.append("GitHub Repository URL is required. Please link the GitHub repository in Jira description.")
        else:
            conn_res = self._ensure_repository_connected(repo_url, repo_pat)
            if not conn_res.get("connected"):
                LOGGER.warning("Repository '%s' inaccessible: %s", repo_url, conn_res.get("error"))
                missing_fields.append(
                    f"GitHub Repository '{repo_url}' is inaccessible to the AI Governor ({conn_res.get('error', 'Access denied / invalid URL')}). Please check repository permissions or PAT token."
                )

        # 4. Codebase Context Extraction & Alignment
        codebase_ctx = self._get_codebase_context(repo_url, request.summary, request.description)
        payload["codebase_context"] = codebase_ctx

        # 5. User memory. Without this the LLM starts from zero on every call and its
        # judgement drifts between otherwise identical requests from the same person.
        from app import user_memory as _memory

        # email is not a declared field on the request model, so read it from the payload.
        identities = _memory.resolve_identities(
            self.settings, payload.get("email"), request.reporter, request.assignee
        )
        memory = _memory.load_memory(self.settings, identities)
        payload["user_context"] = _memory.render_for_prompt(memory)

        # 6. Duplicate detection, scoped to this repository. This always runs: a
        # reporter needs to know the work is already tracked even if the ticket also
        # has fields missing, not discover it on the next resubmission.
        from app.duplicate_detector import DuplicateVerdict

        # A reviewer can overrule a duplicate block by putting this marker in the
        # Jira description, which is the only channel available on the webhook path.
        allow_duplicate = bool(
            re.search(
                r"\[(not[-\s]?a[-\s]?duplicate|allow[-\s]?duplicate)\]",
                request.description or "",
                re.IGNORECASE,
            )
        )
        duplicate = self._check_duplicate(
            summary=request.summary,
            description=request.description,
            repo=repo_url,
            exclude_key=request.issueKey,
            allow_duplicate=allow_duplicate,
        )
        if duplicate.action == "block":
            missing_fields.append(
                f"Duplicate of {duplicate.ticket_key}: this work is already tracked "
                f"({duplicate.ticket_summary}). {duplicate.reasoning} "
                f"If this is genuinely different work, add [not-a-duplicate] to the "
                f"Jira description and it will be reviewed normally."
            )

        payload["duplicate_context"] = self._render_duplicate_context(duplicate)
        payload["mandatory_findings"] = (
            "\n".join(f"- {m}" for m in missing_fields)
            if missing_fields
            else "All mandatory fields are present and valid."
        )

        # 7. Quality review. This runs even when mandatory fields are already failing.
        # Returning early meant the reporter fixed one thing, resubmitted, and was then
        # told about the next thing - one defect per round trip. Every dimension is now
        # assessed in a single pass and reported together.
        llm_output: dict[str, str] | None = None
        try:
            LOGGER.info("workflow1 step started: build_llm_prompt prompt_name=workflow1_prompt")
            prompt = self._build_prompt(payload)
            LOGGER.info("workflow1 step completed: build_llm_prompt length=%s", len(prompt))
            raw_llm_output = self._call_claude(prompt)
            LOGGER.info("workflow1 step completed: call_claude_api length=%s", len(raw_llm_output))
            llm_output = self._parse_llm_output(raw_llm_output)
            LOGGER.info("workflow1 step completed: parse_llm_output nature=%s", llm_output.get("nature"))
        except Exception as review_exc:
            # A failed quality review must not discard the deterministic findings we
            # already have, so this degrades instead of raising.
            LOGGER.warning("workflow1 quality review unavailable for %s: %s", request.issueKey, review_exc)

        model_output = self._consolidate_review(
            issue_key=request.issueKey,
            missing_fields=missing_fields,
            llm_output=llm_output,
            duplicate=duplicate,
            fallback_priority=priority_val,
        )
        LOGGER.info(
            "workflow1 consolidated verdict for %s: nature=%s blocking_issues=%d",
            request.issueKey,
            model_output["nature"],
            len(missing_fields),
        )

        # Record this ticket so the next review of this user's work has continuity.
        try:
            _memory.record_ticket(
                self.settings,
                identifiers=identities,
                ticket_key=request.issueKey,
                summary=request.summary,
                repo=repo_url or "",
                review=model_output["llm_review"],
                nature=model_output["nature"],
            )
            if identities:
                _memory.update_summary(
                    self.settings,
                    identifier=identities[0],
                    memory=_memory.load_memory(self.settings, identities),
                )
        except Exception as mem_exc:
            LOGGER.warning("workflow1 could not update user memory for %s: %s", request.issueKey, mem_exc)

        try:
            LOGGER.info("workflow1 step started: find_slack_channel_ids")
            assignee_match = self._find_slack_channel_id(
                user_role="assignee",
                user_name=self._user_name_from_payload(
                    payload,
                    (
                        "assigneeSlackUserName",
                        "assignee_slack_user_name",
                        "assignee",
                    ),
                ),
                fallback_channel_id=request.assignee,
                email=self._email_from_payload(payload),
            )
            reporter_match = self._find_slack_channel_id(
                user_role="reporter",
                user_name=self._user_name_from_payload(
                    payload,
                    (
                        "reporterSlackUserName",
                        "reporter_slack_user_name",
                        "reporter",
                    ),
                ),
                fallback_channel_id=request.reporter,
                email=None,
            )
            LOGGER.info(
                "workflow1 step completed: find_slack_channel_ids assignee=%s reporter=%s",
                self._to_log_json(self._slack_match_to_log_dict(assignee_match)),
                self._to_log_json(self._slack_match_to_log_dict(reporter_match)),
            )
        except Exception:
            LOGGER.exception("workflow1 step failed: find_slack_channel_ids")
            raise

        try:
            LOGGER.info("workflow1 step started: save_to_db")
            ticket_id = self._save_to_db(
                request=request,
                slack_match=assignee_match,
                nature=model_output["nature"],
                llm_review=model_output["llm_review"],
                priority=model_output["priority"],
                payload=payload,
            )
            LOGGER.info("workflow1 step completed: save_to_db ticket_id=%s", ticket_id)
        except Exception:
            LOGGER.exception("workflow1 step failed: save_to_db")
            raise

        # ── Jira Reverse Sync: Add Comment & Auto-Transition ──
        try:
            from app.jira_client import JiraClient
            jc = JiraClient(self.settings)
            if jc.is_configured():
                if model_output["nature"] == "satisfied":
                    jc.add_comment(
                        request.issueKey,
                        f"[AI Governor Validation]: AI APPROVED\n\n{model_output['llm_review']}",
                    )
                    trans_res = jc.transition_to_approved(request.issueKey)
                    LOGGER.info("workflow1 auto-transition for %s: %s", request.issueKey, trans_res)
                    if trans_res.get("success"):
                        self._sync_local_status(request.issueKey, trans_res.get("to_status") or "Governor Approved")
                    else:
                        LOGGER.warning("workflow1 %s NOT moved to approved status: %s", request.issueKey, trans_res.get("reason"))
                else:
                    jc.add_comment(
                        request.issueKey,
                        f"[AI Governor Validation]: UNSATISFIED / REJECTED\n\n{model_output['llm_review']}",
                    )
        except Exception as exc:
            LOGGER.warning("workflow1 Jira comment/transition skipped/failed for %s: %s", request.issueKey, exc)

        # ── Dispatch Slack Governor Notification ──
        try:
            from app.slack_client import SlackClient
            from app.app_settings import get_all_settings
            sc = SlackClient(self.settings)
            db_conf = get_all_settings(self.settings)
            
            target_channel = (
                self._slack_id_or_none(assignee_match.channel_id)
                or self._slack_id_or_none(reporter_match.channel_id)
                or self.settings.governor_notify_channel_id
                or db_conf.get("governor_notify_channel_id")
                or self.settings.slack_default_channel_id
                or db_conf.get("slack_default_channel_id")
            )
            if target_channel:
                status_text = "AI APPROVED" if model_output["nature"] == "satisfied" else "REJECTED (Unsatisfied)"
                slack_msg = (
                    f"*AI Governor Ticket Review: `{request.issueKey}`*\n"
                    f"- Status: *{status_text}*\n"
                    f"- Summary: *{request.summary}*\n"
                    f"- Assessed Priority: `{model_output['priority']}`\n\n"
                    f"{model_output['llm_review']}"
                )
                # Re-reviews belong under the ticket's original Slack message, not as a
                # new top-level post. Otherwise each edit to a ticket starts a fresh
                # thread and the ticket's history is scattered across the channel.
                existing_channel, existing_thread = self._existing_slack_thread(request.issueKey)
                if existing_thread and existing_channel:
                    target_channel = existing_channel
                post_res = sc.post_message(
                    channel_id=target_channel,
                    text=slack_msg,
                    thread_ts=existing_thread or None,
                )
                if existing_thread:
                    LOGGER.info(
                        "workflow1 posted review for %s into existing thread %s",
                        request.issueKey,
                        existing_thread,
                    )
                LOGGER.info("workflow1 posted Slack review to channel=%s", target_channel)
                # The ts of this root message is the thread_ts of every follow-up
                # reply. Without persisting it, workflow2 cannot map a Slack thread
                # back to this ticket and answers with no context at all.
                if post_res.sent:
                    self._persist_thread_context(
                        issue_key=request.issueKey,
                        slack_channel_id=target_channel,
                        slack_thread_ts=post_res.message_ts or post_res.thread_ts,
                        llm_review=model_output["llm_review"],
                        user_identifier=(
                            reporter_match.email
                            or assignee_match.email
                            or request.reporter
                            or request.assignee
                        ),
                        summary=request.summary,
                        repo_url=repo_url or "",
                    )
            else:
                LOGGER.warning("workflow1 Slack notification skipped for %s: no Slack channel configured (set slack_channel_id in Settings)", request.issueKey)
        except Exception as exc:
            LOGGER.error("workflow1 Slack notification FAILED for %s: %s", request.issueKey, exc)

        response = {
            "assignee_channel_id": assignee_match.channel_id,
            "reporter_channel_id": reporter_match.channel_id,
            "nature": model_output["nature"],
            "llm_review": model_output["llm_review"],
            "priority": model_output["priority"],
            "missing_fields": missing_fields,
            "github_repo": repo_url or "",
            "duplicate": {
                "is_duplicate": duplicate.is_duplicate,
                "action": duplicate.action,
                "ticket_key": duplicate.ticket_key,
                "ticket_summary": duplicate.ticket_summary,
                "confidence": duplicate.confidence,
                "reasoning": duplicate.reasoning,
            },
        }
        LOGGER.info("workflow1 completed with response: %s", self._to_log_json(response))
        return response

    def _get_codebase_context(self, repo_url: str | None, summary: str, description: str) -> str:
        """Scans connected repository structure, readme, and relevant code files for context relevance."""
        if not repo_url:
            return "No repository linked or accessible."
        try:
            import os
            from pathlib import Path
            repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")

            candidates = [
                Path(self.settings.repo_clone_dir) / repo_name,
                Path("workspace/repos") / repo_name,
                Path(self.settings.repository_search_root or "/host-repos") / repo_name,
                Path(self.settings.repository_host_root or "/host-repos") / repo_name,
                Path(repo_name),
            ]
            repo_path = None
            for c in candidates:
                if c.exists() and c.is_dir():
                    repo_path = c
                    break

            if not repo_path:
                return f"Repository '{repo_name}' is declared ({repo_url}) but not yet cloned or indexed in workspace."

            files_list: list[str] = []
            key_snippets: list[str] = []
            keywords = [w.lower() for w in re.findall(r"\w{4,}", f"{summary} {description}")]

            for root, dirs, files in os.walk(repo_path):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "dist", "build", "target", "venv", "__pycache__"}]
                for f in files:
                    if f.startswith(".") or f.endswith((".png", ".jpg", ".zip", ".lock", ".svg", ".pyc")):
                        continue
                    rel_path = os.path.relpath(os.path.join(root, f), repo_path)
                    files_list.append(rel_path)

                    if any(kw in rel_path.lower() for kw in keywords) or f.lower() in {"readme.md", "package.json", "requirements.txt", "architecture.md"}:
                        if len(key_snippets) < 5:
                            try:
                                content = Path(root, f).read_text(encoding="utf-8", errors="ignore")[:600]
                                key_snippets.append(f"File `{rel_path}` snippet:\n{content}")
                            except Exception:
                                pass
                    if len(files_list) > 150:
                        break

            structure_summary = ", ".join(files_list[:40])
            snippets_text = "\n\n".join(key_snippets) if key_snippets else "No specific matching code files for keywords."

            return (
                f"Repository Name: {repo_name}\n"
                f"Key Files & Structure ({len(files_list)} files found):\n{structure_summary}\n\n"
                f"Relevant Code Snippets / Key Files:\n{snippets_text}"
            )
        except Exception as exc:
            LOGGER.warning("Could not extract codebase context: %s", exc)
            return f"Codebase context unavailable: {exc}"

    def _extract_repo_url(self, payload: dict[str, Any]) -> tuple[str | None, str | None]:
        """Extract GitHub Repo URL and optional PAT from payload or description text."""
        explicit_url = payload.get("github_repo_url") or payload.get("target_repo") or payload.get("repo")
        explicit_pat = payload.get("github_pat") or payload.get("pat")

        text = f"{payload.get('summary', '')} {payload.get('description', '')}"

        if not explicit_url:
            match = re.search(r"https?://github\.com/([\w\.\-]+)/([\w\.\-]+?)(?:\.git|/|\s|$)", text, re.IGNORECASE)
            if match:
                explicit_url = f"https://github.com/{match.group(1)}/{match.group(2)}"
            else:
                repo_match = re.search(r"(?:repo|repository):\s*([a-zA-Z0-9_\-\./]+)", text, re.IGNORECASE)
                if repo_match:
                    explicit_url = repo_match.group(1).strip()

        if not explicit_pat:
            pat_match = re.search(r"(?:PAT|token):\s*([a-zA-Z0-9_\-]{15,})", text, re.IGNORECASE)
            if pat_match:
                explicit_pat = pat_match.group(1).strip()

        return explicit_url, explicit_pat

    def _ensure_repository_connected(self, repo_url: str, pat: str | None = None) -> dict[str, Any]:
        """Checks if repo exists in workspace, or auto-clones it if new."""
        try:
            from app.rca import repos as rca_repos
            existing_repos = rca_repos.list_repos(self.settings)
        except Exception:
            existing_repos = []

        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")

        if repo_name in existing_repos:
            LOGGER.info("Repository '%s' is already connected in workspace.", repo_name)
            return {"connected": True, "name": repo_name, "status": "existing"}

        # Try auto-cloning if full URL is provided
        if repo_url.startswith("http://") or repo_url.startswith("https://") or "github.com" in repo_url:
            try:
                import subprocess
                from pathlib import Path
                clone_target = Path(self.settings.repo_clone_dir) / repo_name
                clone_target.parent.mkdir(parents=True, exist_ok=True)
                if not clone_target.exists():
                    auth_url = repo_url
                    token = pat or self.settings.github_token
                    if token and "github.com" in repo_url and not ("@" in repo_url):
                        auth_url = repo_url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
                    LOGGER.info("Cloning new repository '%s' into workspace...", repo_name)
                    # A 60s cap was rejecting large but perfectly valid repos (buzz,
                    # ~474 files) as "inaccessible". Blobless clone keeps this fast
                    # while the wider budget covers genuinely large histories.
                    clone_timeout = self.settings.repo_clone_timeout_seconds
                    subprocess.run(
                        [
                            "git", "clone", "--depth", "1", "--filter=blob:none",
                            "--single-branch", auth_url, str(clone_target),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=clone_timeout,
                    )
                    LOGGER.info("Successfully cloned '%s' into workspace.", repo_name)
                return {"connected": True, "name": repo_name, "status": "cloned"}
            except Exception as exc:
                err = re.sub(r"(https?://)[^@/\s]+@", r"\1***@", str(exc))
                LOGGER.warning("Auto-clone for '%s' failed: %s", repo_url, err)
                return {"connected": False, "name": repo_name, "error": err}

        return {"connected": False, "name": repo_name, "error": "Repository not found in workspace"}


    def _validate_input(self, request: Workflow1ReviewRequest) -> None:
        if not request.issueKey.strip():
            raise ValueError("issueKey is required")
        if not request.summary.strip():
            raise ValueError("summary is required")

    def _existing_slack_thread(self, issue_key: str) -> tuple[str, str]:
        """Channel and thread ts of this ticket's original Slack message, if any.

        Returns ("", "") when the ticket has not been posted before, so the caller
        starts a new thread for it.
        """
        if not self.settings.database_url or not issue_key:
            return "", ""
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT slack_channel_id, slack_thread_ts
                    FROM tickets
                    WHERE UPPER(jira_ticket_id) = UPPER(%s)
                      AND slack_thread_ts IS NOT NULL
                      AND TRIM(slack_thread_ts) <> ''
                    LIMIT 1
                    """,
                    (issue_key,),
                ).fetchone()
            if row:
                return str(row.get("slack_channel_id") or ""), str(row.get("slack_thread_ts") or "")
        except Exception as exc:  # noqa: BLE001 - never block the Slack post
            LOGGER.warning("Could not look up existing Slack thread for %s: %s", issue_key, exc)
        return "", ""

    def _consolidate_review(
        self,
        *,
        issue_key: str,
        missing_fields: list[str],
        llm_output: dict[str, str] | None,
        duplicate,
        fallback_priority: str,
    ) -> dict[str, str]:
        """Merge every check into one verdict and one report.

        The ticket is unsatisfied if ANY dimension fails - mandatory fields, the
        quality review, or a confirmed duplicate - and the report always lists the
        findings from every dimension. Previously the mandatory-field check returned
        early, so a reporter fixed one thing, resubmitted, and only then heard about
        the next: one defect per round trip. Everything is now surfaced at once.
        """
        llm_output = llm_output or {}
        llm_nature = str(llm_output.get("nature") or "").strip().lower()
        llm_review = str(llm_output.get("llm_review") or "").strip()
        priority = str(llm_output.get("priority") or "").strip().upper()
        if priority not in VALID_PRIORITIES:
            priority = fallback_priority if fallback_priority in VALID_PRIORITIES else "P3"

        failed = bool(missing_fields) or llm_nature == "unsatisfied" or duplicate.action == "block"
        nature = "unsatisfied" if failed else "satisfied"

        sections: list[str] = []
        if failed:
            sections.append("[AI Governor Quality Check: REJECTED (Unsatisfied)]")
            sections.append(
                f"Complete review of `{issue_key}`. Every issue found across all checks is "
                f"listed below - please address all of them in a single update."
            )
        else:
            sections.append("[AI Governor Quality Check: APPROVED (Satisfied)]")
            sections.append(f"Complete review of `{issue_key}`. All checks passed.")

        if missing_fields:
            listed = "\n".join(f"- {m}" for m in missing_fields)
            sections.append(f"Blocking issues ({len(missing_fields)}):\n{listed}")

        if llm_review:
            sections.append(f"Quality and codebase review:\n{llm_review}")
        elif not llm_output:
            sections.append(
                "Quality and codebase review: could not be completed on this run because "
                "the review service did not respond. The findings above still stand, and "
                "the next update will be reviewed in full."
            )

        if duplicate.is_duplicate and duplicate.action != "block":
            sections.append(duplicate.as_review_note())

        if failed:
            sections.append(
                "Action Required: update the ticket in Jira addressing every point above. "
                "The next review re-checks all of them together."
            )

        return {
            "nature": nature,
            "llm_review": "\n\n".join(s for s in sections if s),
            "priority": priority,
        }

    def _check_duplicate(
        self,
        *,
        summary: str,
        description: str,
        repo: str | None,
        exclude_key: str | None,
        allow_duplicate: bool = False,
    ):
        """Run duplicate detection, never letting a failure block a review."""
        from app.duplicate_detector import DuplicateDetector, DuplicateVerdict

        if not self.settings.duplicate_detection_enabled:
            return DuplicateVerdict()
        try:
            return DuplicateDetector(self.settings).check(
                summary=summary,
                description=description,
                repo=repo,
                exclude_key=exclude_key,
                allow_duplicate=allow_duplicate,
            )
        except Exception as exc:
            LOGGER.warning("Duplicate detection failed (continuing without it): %s", exc)
            return DuplicateVerdict()

    @staticmethod
    def _render_duplicate_context(verdict) -> str:
        if not verdict.checked:
            return "Duplicate check could not be completed. Do not draw any conclusion from this."
        if not verdict.is_duplicate:
            return (
                f"No duplicate found. {verdict.candidates_considered} existing ticket(s) in the "
                f"same repository were compared against this one."
            )
        label = "CONFIRMED DUPLICATE" if verdict.action == "block" else "POSSIBLE DUPLICATE"
        return (
            f"{label} of {verdict.ticket_key} (confidence {verdict.confidence:.0%}).\n"
            f"Existing ticket: {verdict.ticket_summary} (status: {verdict.ticket_status or 'unknown'})\n"
            f"Assessment: {verdict.reasoning}"
        )

    def _build_prompt(self, payload: dict[str, Any]) -> str:
        prompt_template = self.prompt_store.load("workflow1_prompt")
        # The template is formatted with str.format, so every placeholder must have a
        # value or the whole review dies with KeyError. Default the optional context
        # blocks here so a caller that does not supply them still works.
        filled = {
            "user_context": "No previous ticket history is on record for this user.",
            "duplicate_context": "No duplicate check was performed.",
            "mandatory_findings": "No automated field checks were run.",
            "codebase_context": "No repository linked or accessible.",
            "github_repo": "None provided",
            **payload,
        }
        return prompt_template.format(**filled)

    def _call_claude(self, prompt: str) -> str:
        from app.llm_client import build_llm_client

        client = build_llm_client(self.settings)
        system_prompt = "You are a Jira ticket QA reviewer. Return valid JSON only matching the requested schema."
        output = client.complete(system_prompt=system_prompt, user_message=prompt, max_tokens=1024)
        return (output or "").strip()

    def _parse_llm_output(self, raw_output: str) -> dict[str, str]:
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LLM returned invalid workflow1 JSON") from exc

        if not isinstance(parsed, dict):
            raise RuntimeError("LLM returned invalid workflow1 JSON")

        nature = parsed.get("nature")
        llm_review = parsed.get("llm_review")
        priority = parsed.get("priority")
        if not isinstance(nature, str) or nature.strip().lower() not in VALID_NATURES:
            raise RuntimeError("LLM workflow1 JSON must include nature as satisfied or unsatisfied")
        if not isinstance(llm_review, str) or not llm_review.strip():
            raise RuntimeError("LLM workflow1 JSON must include a non-empty llm_review")
        if not isinstance(priority, str) or priority.strip().upper() not in VALID_PRIORITIES:
            raise RuntimeError("LLM workflow1 JSON must include priority as P0, P1, P2, P3, or P4")

        return {
            "nature": nature.strip().lower(),
            "llm_review": llm_review.strip(),
            "priority": priority.strip().upper(),
        }

    def _find_slack_channel_id(
        self,
        *,
        user_role: str,
        user_name: str | None,
        fallback_channel_id: str,
        email: str | None,
    ) -> SlackUserMatch:
        LOGGER.info(
            "workflow1 db lookup input: user_role=%s slack_user_name=%s email=%s fallback_channel_id=%s",
            user_role,
            user_name,
            email,
            fallback_channel_id,
        )
        if not user_name:
            LOGGER.info("workflow1 db lookup skipped: no %s slack_user_name found in payload", user_role)
            return SlackUserMatch(channel_id=fallback_channel_id, email=email, user_id=None)

        if not self.settings.database_url:
            return SlackUserMatch(channel_id=fallback_channel_id, email=email, user_id=None)

        row = None
        try:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT email_id, slack_user_name, channel_id
                    FROM channelid_table
                    WHERE lower(trim(leading '@' from slack_user_name)) =
                          lower(trim(leading '@' from %s))
                    LIMIT 1
                    """,
                    (user_name,),
                ).fetchone()
        except Exception:
            try:
                import psycopg2
                from psycopg2.extras import RealDictCursor
                with psycopg2.connect(self.settings.database_url) as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                        cursor.execute(
                            """
                            SELECT email_id, slack_user_name, channel_id
                            FROM channelid_table
                            WHERE lower(trim(leading '@' from slack_user_name)) =
                                  lower(trim(leading '@' from %s))
                            LIMIT 1
                            """,
                            (user_name,),
                        )
                        row = cursor.fetchone()
            except Exception as exc:
                LOGGER.warning("channelid_table query fallback: %s", exc)

        if not row:
            LOGGER.info(
                "workflow1 db query result: no channelid_table row found for user_role=%s slack_user_name=%s",
                user_role,
                user_name,
            )
            return SlackUserMatch(channel_id=fallback_channel_id, email=email, user_id=None)

        LOGGER.info(
            "workflow1 db query result from channelid_table for user_role=%s: %s",
            user_role,
            self._to_log_json(dict(row)),
        )
        channel_id = str(row.get("channel_id") or "").strip()
        return SlackUserMatch(
            channel_id=channel_id or fallback_channel_id,
            email=str(row.get("email_id") or email),
            user_id=channel_id or None,
        )

    def _user_name_from_payload(self, payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip() and not self._looks_like_email(value):
                return value.strip()

        return None

    def _email_from_payload(self, payload: dict[str, Any]) -> str | None:
        for key in (
            "email",
            "jiraEmail",
            "jira_email",
            "assigneeEmail",
            "assignee_email",
            "reporterEmail",
            "reporter_email",
        ):
            value = payload.get(key)
            if isinstance(value, str) and self._looks_like_email(value):
                return value.strip()

        for key in ("assignee", "reporter"):
            value = payload.get(key)
            if isinstance(value, str) and self._looks_like_email(value):
                return value.strip()

        return None

    @staticmethod
    def _slack_id_or_none(value: str | None) -> str | None:
        """Return value only if it is a Slack conversation/user ID (C…, G…, D…, U…, W…)."""
        v = (value or "").strip()
        return v if re.fullmatch(r"[CGDUW][A-Z0-9]{6,}", v) else None

    def _sync_local_status(self, issue_key: str, status: str) -> None:
        """Mirror the Jira status change into our own tables so the UI shows it immediately."""
        if not self.settings.database_url:
            return
        try:
            import psycopg
            with psycopg.connect(self.settings.database_url) as conn:
                conn.execute("UPDATE tickets SET status = %s WHERE jira_ticket_id = %s", (status, issue_key))
                row = conn.execute(
                    "UPDATE jira_ticket_cache SET status = %s WHERE ticket_key = %s RETURNING project_key",
                    (status, issue_key),
                ).fetchone()
                LOGGER.info("workflow1 local status for %s set to '%s' (cache row: %s)", issue_key, status, bool(row))
        except Exception as exc:
            LOGGER.warning("workflow1 local status sync failed for %s: %s", issue_key, exc)

    def _looks_like_email(self, value: str) -> bool:
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))

    def _save_to_db(
        self,
        *,
        request: Workflow1ReviewRequest,
        slack_match: SlackUserMatch,
        nature: str,
        llm_review: str,
        priority: str,
        payload: dict[str, Any],
    ) -> str:
        if not self.settings.database_url:
            raise RuntimeError("DATABASE_URL is required for workflow1")

        try:
            import psycopg2
            from psycopg2.extras import Json
        except ImportError as exc:
            raise RuntimeError("The 'psycopg2-binary' package is required") from exc

        with psycopg2.connect(self.settings.database_url) as conn:
            with conn.cursor() as cursor:
                ticket_insert_values = {
                    "jira_ticket_id": request.issueKey,
                    "email": slack_match.email,
                    "assigned_user_id": slack_match.user_id,
                    "slack_channel_id": slack_match.channel_id,
                    "llm_review": llm_review,
                    "status": "open",
                    "jira_payload": payload,
                }
                LOGGER.info(
                    "workflow1 db upsert into tickets input: %s",
                    self._to_log_json(ticket_insert_values),
                )
                cursor.execute(
                    """
                    INSERT INTO tickets (
                        jira_ticket_id,
                        email,
                        assigned_user_id,
                        slack_channel_id,
                        llm_review,
                        status,
                        jira_payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (jira_ticket_id)
                    DO UPDATE SET
                        llm_review = EXCLUDED.llm_review,
                        status = 'open',
                        jira_payload = EXCLUDED.jira_payload,
                        created_at = NOW()
                    RETURNING id
                    """,
                    (
                        request.issueKey,
                        slack_match.email,
                        slack_match.user_id,
                        slack_match.channel_id,
                        llm_review,
                        "open",
                        Json(payload),
                    ),
                )
                row = cursor.fetchone()
                if not row:
                    raise RuntimeError("tickets upsert did not return an id")

                ticket_id = str(row[0])
                LOGGER.info("workflow1 db upsert into tickets returned id=%s", ticket_id)
                LOGGER.info(
                    "workflow1 db insert into messages input: %s",
                    self._to_log_json(
                        {
                            "ticket_id": ticket_id,
                            "sender": "bot",
                            "message": llm_review,
                        }
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO messages (ticket_id, sender, message)
                    VALUES (%s, 'bot', %s)
                    """,
                    (ticket_id, llm_review),
                )

                self._insert_due_date_tracking_if_needed(
                    cursor,
                    request=request,
                    slack_match=slack_match,
                    nature=nature,
                    priority=priority,
                )

                return ticket_id

    def _persist_thread_context(
        self,
        *,
        issue_key: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        llm_review: str,
        user_identifier: str | None,
        summary: str,
        repo_url: str,
    ) -> None:
        """Map the Slack root message to this ticket and record it as the user's latest ticket.

        Workflow2 resolves an incoming thread reply through tickets.slack_thread_ts,
        jira_slack_conversations, or user_memory, in that order. Writing all three here
        is what lets the bot answer follow-ups with the ticket and repo in context.
        """
        if not self.settings.database_url or not slack_thread_ts:
            return
        try:
            import psycopg2
            from psycopg2.extras import Json

            with psycopg2.connect(self.settings.database_url) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE tickets
                        SET slack_thread_ts = %s,
                            slack_channel_id = COALESCE(%s, slack_channel_id)
                        WHERE jira_ticket_id = %s
                        """,
                        (slack_thread_ts, slack_channel_id, issue_key),
                    )
                    cursor.execute(
                        """
                        INSERT INTO jira_slack_conversations (
                            slack_thread_ts, slack_channel_id, issue_key, jira_issue_key, history
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (slack_thread_ts) DO UPDATE SET
                            issue_key = EXCLUDED.issue_key,
                            jira_issue_key = EXCLUDED.jira_issue_key,
                            updated_at = NOW()
                        """,
                        (
                            slack_thread_ts,
                            slack_channel_id,
                            issue_key,
                            issue_key,
                            Json([{"role": "assistant", "text": llm_review}]),
                        ),
                    )
            # user_memory is written by app.user_memory.record_ticket during review,
            # so it is deliberately not written again here.
            LOGGER.info(
                "workflow1 persisted thread context for %s (thread_ts=%s)", issue_key, slack_thread_ts
            )
        except Exception as exc:
            LOGGER.warning("workflow1 could not persist thread context for %s: %s", issue_key, exc)

    def _insert_due_date_tracking_if_needed(
        self,
        cursor: Any,
        *,
        request: Workflow1ReviewRequest,
        slack_match: SlackUserMatch,
        nature: str,
        priority: str,
    ) -> None:
        if nature != "satisfied":
            LOGGER.info(
                "workflow1 %s: skipped due_date_tracking insert because nature=%s",
                request.issueKey,
                nature,
            )
            return

        due_date_value = request.dueDate.strip()
        if not due_date_value:
            LOGGER.info(
                "workflow1 %s: skipped due_date_tracking insert because dueDate is empty",
                request.issueKey,
            )
            return

        if not request.createdAt.strip():
            LOGGER.info(
                "workflow1 %s: skipped due_date_tracking insert because createdAt is empty",
                request.issueKey,
            )
            return

        tracking_start = self._parse_jira_datetime_to_date(request.createdAt)
        due_date_parsed = self._parse_jira_date(request.dueDate)
        total_working_days = self._count_working_days(tracking_start, due_date_parsed)
        LOGGER.info(
            "workflow1 %s: tracking_start=%s, due_date=%s, total_working_days=%s",
            request.issueKey,
            tracking_start,
            due_date_parsed,
            total_working_days,
        )

        cursor.execute(
            """
            INSERT INTO due_date_tracking (
                ticket_id,
                jira_ticket_id,
                priority,
                assignee_slack_id,
                due_date,
                tracking_start_date,
                total_working_days
            )
            SELECT
                t.id,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            FROM tickets t
            WHERE t.jira_ticket_id = %s
            ON CONFLICT (jira_ticket_id)
            DO UPDATE SET
                due_date = EXCLUDED.due_date,
                tracking_start_date = EXCLUDED.tracking_start_date,
                total_working_days = EXCLUDED.total_working_days,
                priority = EXCLUDED.priority,
                assignee_slack_id = EXCLUDED.assignee_slack_id,
                alert_75_sent = FALSE,
                alert_50_sent = FALSE,
                alert_25_sent = FALSE,
                alert_0_sent = FALSE,
                exceeded_alert_sent_at = NULL,
                is_completed = FALSE
            """,
            (
                request.issueKey,
                priority,
                slack_match.channel_id,
                due_date_parsed,
                tracking_start,
                total_working_days,
                request.issueKey,
            ),
        )
        LOGGER.info(
            "workflow1 %s: inserted due_date_tracking, start=%s, due=%s, working_days=%s",
            request.issueKey,
            tracking_start,
            due_date_parsed,
            total_working_days,
        )

    def _parse_jira_datetime_to_date(self, value: str) -> date:
        return dateutil_parser.parse(value.strip()).date()

    def _parse_jira_date(self, value: str) -> date:
        return date.fromisoformat(value.strip())

    def _count_working_days(self, start_date: date, end_date: date) -> int:
        if start_date > end_date:
            return 1
        count = 0
        current = start_date
        while current <= end_date:
            if current.weekday() < 5:  # 0=Mon, 4=Fri
                count += 1
            current += timedelta(days=1)
        return max(count, 1)

    def _slack_match_to_log_dict(self, slack_match: SlackUserMatch) -> dict[str, Any]:
        return {
            "channel_id": slack_match.channel_id,
            "email": slack_match.email,
            "user_id": slack_match.user_id,
        }

    def _to_log_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)
