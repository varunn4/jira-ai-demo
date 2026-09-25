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

        # If any hard mandatory field is missing, reject immediately with structured guidance
        if missing_fields:
            missing_text = "\n".join(f"• {m}" for m in missing_fields)
            review_msg = (
                f"⚠️ *AI Governor Quality Check: REJECTED (Unsatisfied)*\n\n"
                f"The ticket `{request.issueKey}` cannot be approved because mandatory fields are missing:\n\n"
                f"{missing_text}\n\n"
                f"👉 *Action Required:* Please update these fields directly in Jira to proceed to **AI Approved**."
            )
            model_output = {
                "nature": "unsatisfied",
                "llm_review": review_msg,
                "priority": priority_val if priority_val in VALID_PRIORITIES else "P3",
            }
        else:
            # All mandatory fields present -> Call LLM for Acceptance Criteria & Codebase Quality Review
            try:
                LOGGER.info("workflow1 step started: build_llm_prompt prompt_name=workflow1_prompt")
                prompt = self._build_prompt(payload)
                LOGGER.info("workflow1 step completed: build_llm_prompt length=%s", len(prompt))
            except Exception:
                LOGGER.exception("workflow1 step failed: build_llm_prompt")
                raise

            try:
                LOGGER.info("workflow1 step started: call_claude_api model=claude-opus-4-5")
                raw_llm_output = self._call_claude(prompt)
                LOGGER.info("workflow1 step completed: call_claude_api length=%s", len(raw_llm_output))
            except Exception:
                LOGGER.exception("workflow1 step failed: call_claude_api")
                raise

            try:
                LOGGER.info("workflow1 step started: parse_llm_output")
                model_output = self._parse_llm_output(raw_llm_output)
                LOGGER.info("workflow1 step completed: parse_llm_output nature=%s", model_output.get("nature"))
            except Exception:
                LOGGER.exception("workflow1 step failed: parse_llm_output")
                raise

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
                        f"🤖 *AI Governor Validation*: **AI APPROVED**\n\n{model_output['llm_review']}",
                    )
                    trans_res = jc.transition_to_approved(request.issueKey)
                    LOGGER.info("workflow1 auto-transition for %s: %s", request.issueKey, trans_res)
                else:
                    jc.add_comment(
                        request.issueKey,
                        f"⚠️ *AI Governor Validation*: **UNSATISFIED / REJECTED**\n\n{model_output['llm_review']}",
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
                assignee_match.channel_id
                or reporter_match.channel_id
                or self.settings.governor_notify_channel_id
                or db_conf.get("governor_notify_channel_id")
                or self.settings.slack_default_channel_id
                or db_conf.get("slack_default_channel_id")
            )
            if target_channel:
                status_emoji = "✅ *AI APPROVED*" if model_output["nature"] == "satisfied" else "⚠️ *REJECTED (Unsatisfied)*"
                slack_msg = (
                    f"🤖 *AI Governor Ticket Review: `{request.issueKey}`*\n"
                    f"• *Status:* {status_emoji}\n"
                    f"• *Summary:* *{request.summary}*\n"
                    f"• *Assessed Priority:* `{model_output['priority']}`\n\n"
                    f"{model_output['llm_review']}"
                )
                sc.post_message(channel_id=target_channel, text=slack_msg)
                LOGGER.info("workflow1 posted Slack review to channel=%s", target_channel)
        except Exception as exc:
            LOGGER.warning("workflow1 direct Slack notification skipped: %s", exc)

        response = {
            "assignee_channel_id": assignee_match.channel_id,
            "reporter_channel_id": reporter_match.channel_id,
            "nature": model_output["nature"],
            "llm_review": model_output["llm_review"],
            "priority": model_output["priority"],
            "missing_fields": missing_fields,
            "github_repo": repo_url or "",
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
                clone_target = Path("workspace/repos") / repo_name
                clone_target.parent.mkdir(parents=True, exist_ok=True)
                if not clone_target.exists():
                    auth_url = repo_url
                    token = pat or self.settings.github_token
                    if token and "github.com" in repo_url and not ("@" in repo_url):
                        auth_url = repo_url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
                    LOGGER.info("Cloning new repository '%s' into workspace...", repo_name)
                    subprocess.run(["git", "clone", "--depth", "1", auth_url, str(clone_target)], check=True, capture_output=True, timeout=60)
                    LOGGER.info("Successfully cloned '%s' into workspace.", repo_name)
                return {"connected": True, "name": repo_name, "status": "cloned"}
            except Exception as exc:
                LOGGER.warning("Auto-clone for '%s' failed: %s", repo_url, exc)
                return {"connected": False, "name": repo_name, "error": str(exc)}

        return {"connected": False, "name": repo_name, "error": "Repository not found in workspace"}


    def _validate_input(self, request: Workflow1ReviewRequest) -> None:
        if not request.issueKey.strip():
            raise ValueError("issueKey is required")
        if not request.summary.strip():
            raise ValueError("summary is required")

    def _build_prompt(self, payload: dict[str, Any]) -> str:
        prompt_template = self.prompt_store.load("workflow1_prompt")
        return prompt_template.format(**payload)

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
