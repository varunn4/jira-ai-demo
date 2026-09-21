"""Developer test-case PR gate.

Before generating developer (Code Review) test cases we must be sure we have the
code under review. RepoTree only knows the nightly-synced main branch, so the dev
flow requires the PR URL. This module implements two steps the n8n dev workflow
calls in order:

  1. ``pr_gate(issue_key)`` — look for a GitHub PR URL in the ticket's Jira
     comments. If found, return ``ready`` with the parsed ref. If not, post a
     comment asking the developer for it (idempotently — it won't re-ask if the
     ask is already the pending state) and return ``awaiting_pr`` so n8n stops.

  2. ``pr_context(pr_url)`` — pull the PR's changed files + diffs via the GitHub
     API and return the text blob to inject into ``/testcases/generate``.

The developer replies to the ask comment with the PR URL; a Jira comment-created
webhook re-triggers the dev flow, and the gate now returns ``ready``.
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.github_client import (
    GitHubClient,
    PullRequestRef,
    comment_plain_text,
    find_pr_url_in_comments,
    parse_pr_url,
)
from app.jira_client import JiraClient

log = logging.getLogger(__name__)

# Marker embedded in the ask comment so the gate can recognise its own prior
# request and avoid re-asking on every webhook re-trigger.
ASK_MARKER = "⟦dev-testcases:awaiting-pr⟧"

ASK_MESSAGE = (
    "🧪 Dev test cases: please reply to this ticket with the Pull Request URL "
    "(e.g. https://github.com/{org}/<repo>/pull/<number>) so we generate the cases "
    "against the exact code under review, not the indexed main branch. "
    + ASK_MARKER
)


def _already_asked_without_reply(comments: list[dict[str, Any]]) -> bool:
    """True if our ask is the pending state: the ask exists and no PR URL has
    been posted since (comments are newest-first, so scan until we hit either).
    """
    for comment in comments:
        text = comment_plain_text(comment)
        if parse_pr_url(text):
            return False  # a PR URL appears more recently than any ask
        if ASK_MARKER in text:
            return True
    return False


def pr_gate(issue_key: str, settings: Settings) -> dict[str, Any]:
    """Resolve the PR URL for a dev ticket, asking in Jira if it's missing."""
    jira = JiraClient(settings)
    comments = jira.get_comments(issue_key)

    ref = find_pr_url_in_comments(comments)
    if ref:
        log.info("PR gate ready for %s → %s", issue_key, ref.url)
        return {
            "status": "ready",
            "issueKey": issue_key,
            "prUrl": ref.url,
            "owner": ref.owner,
            "repo": ref.repo,
            "number": ref.number,
            "commentPosted": False,
        }

    if _already_asked_without_reply(comments):
        log.info("PR gate still awaiting PR for %s (already asked)", issue_key)
        return {
            "status": "awaiting_pr",
            "issueKey": issue_key,
            "commentPosted": False,
            "reason": "Already asked for the PR URL; still waiting on a reply.",
        }

    message = ASK_MESSAGE.format(org=settings.github_org)
    result = jira.add_comment(issue_key, message)
    posted = not result.get("dry_run", False)
    log.info("PR gate asked for PR on %s (posted=%s)", issue_key, posted)
    return {
        "status": "awaiting_pr",
        "issueKey": issue_key,
        "commentPosted": posted,
        "reason": "Posted a comment asking the developer for the PR URL.",
    }


def pr_context(pr_url: str, settings: Settings) -> dict[str, Any]:
    """Fetch the PR's changed files + diffs to ground dev test-case generation."""
    ref: PullRequestRef | None = parse_pr_url(pr_url)
    if not ref:
        raise ValueError(f"Not a recognised GitHub PR URL: {pr_url!r}")

    gh = GitHubClient(settings)
    ctx = gh.fetch_pr_context(ref)
    log.info(
        "Fetched PR context %s: %d changed files, %d chars",
        ref.url, len(ctx.get("changed_files", [])), len(ctx.get("context_text", "")),
    )
    return ctx
