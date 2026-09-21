"""GitHub helpers for the developer test-case flow.

At Code Review the code under review lives in a PR branch that RepoTree has
never indexed (it nightly-syncs the main branch of each checkout on the server).
So for `audience="dev"` we read a PR URL the developer left in the Jira comments
and pull the PR's changed files + unified diffs via the GitHub API. That diff is
injected into `/testcases/generate` as authoritative "latest code" context so the
generated cases exercise what actually changed, not stale main-branch code.

Only two things are exported for callers:
  - ``find_pr_url_in_comments`` / ``parse_pr_url`` — locate + parse the PR URL.
  - ``GitHubClient.fetch_pr_context`` — pull the PR diff as a text blob.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import requests

from app.config import Settings

log = logging.getLogger(__name__)

# https://github.com/<owner>/<repo>/pull/<number>  (tolerates trailing /files, #discussion, query string)
_PR_URL_RE = re.compile(
    r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


def parse_pr_url(text: str) -> Optional[PullRequestRef]:
    """Return the first GitHub PR reference found in ``text``, or None."""
    if not text:
        return None
    m = _PR_URL_RE.search(text)
    if not m:
        return None
    return PullRequestRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


def comment_plain_text(comment: dict[str, Any]) -> str:
    """Flatten a Jira Cloud ADF (Atlassian Document Format) comment body to text.

    Comments come back as nested `{type, content, text}` nodes; we only need the
    concatenated leaf `text` values to scan for a URL. Also picks up link marks,
    whose href is where a pasted URL often ends up.
    """
    body = comment.get("body")
    if isinstance(body, str):  # older REST v2 shape
        return body
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                parts.append(node["text"])
            for mark in node.get("marks") or []:
                href = (mark.get("attrs") or {}).get("href")
                if isinstance(href, str):
                    parts.append(href)
            walk(node.get("content"))
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(body)
    return " ".join(parts)


def find_pr_url_in_comments(comments: list[dict[str, Any]]) -> Optional[PullRequestRef]:
    """Scan comments (assumed newest-first) and return the most recent PR ref."""
    for comment in comments:
        ref = parse_pr_url(comment_plain_text(comment))
        if ref:
            return ref
    return None


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.github_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        resp = requests.get(
            f"{self.settings.github_api_base}{path}",
            headers=self._headers(),
            timeout=self.settings.external_request_timeout_seconds,
            **kwargs,
        )
        resp.raise_for_status()
        return resp

    def fetch_pr_context(self, ref: PullRequestRef) -> dict[str, Any]:
        """Fetch PR metadata + changed-file diffs and build a grounding blob.

        Returns a dict with the mapped repo name, head sha/ref, the list of
        changed file paths, and ``context_text`` — the diff blob to inject into
        generation. Raises RuntimeError if the token is not configured.
        """
        if not self.is_configured():
            raise RuntimeError(
                "GITHUB_TOKEN is not configured; cannot fetch PR context for dev test cases."
            )

        meta = self._get(f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}").json()
        title = meta.get("title") or ""
        head = meta.get("head") or {}
        base = meta.get("base") or {}
        head_ref = head.get("ref") or ""
        head_sha = (head.get("sha") or "")[:12]
        base_ref = base.get("ref") or ""

        files = self._fetch_pr_files(ref)
        context_text = self._build_context_text(ref, title, head_ref, head_sha, base_ref, files)

        return {
            "repo": ref.repo,
            "owner": ref.owner,
            "number": ref.number,
            "pr_url": ref.url,
            "title": title,
            "head_ref": head_ref,
            "head_sha": head_sha,
            "base_ref": base_ref,
            "changed_files": [f.get("filename") for f in files if f.get("filename")],
            "context_text": context_text,
        }

    def _fetch_pr_files(self, ref: PullRequestRef) -> list[dict[str, Any]]:
        """Page through the PR's changed files up to the configured cap."""
        files: list[dict[str, Any]] = []
        page = 1
        cap = self.settings.github_pr_max_files
        while len(files) < cap and page <= 10:  # 10 pages * 100 = 1000 hard ceiling
            batch = self._get(
                f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/files",
                params={"per_page": 100, "page": page},
            ).json()
            if not isinstance(batch, list) or not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files[:cap]

    def _build_context_text(
        self,
        ref: PullRequestRef,
        title: str,
        head_ref: str,
        head_sha: str,
        base_ref: str,
        files: list[dict[str, Any]],
    ) -> str:
        per_file_cap = self.settings.github_pr_per_file_patch_chars
        total_cap = self.settings.github_pr_context_max_chars

        lines: list[str] = [
            f"PR #{ref.number} in {ref.owner}/{ref.repo}: {title}".strip(),
            f"Branch: {head_ref}@{head_sha}  (base: {base_ref})",
            f"Changed files: {len(files)}",
            "",
        ]
        for f in files:
            name = f.get("filename")
            if not name:
                continue
            status = f.get("status", "modified")
            adds = f.get("additions", 0)
            dels = f.get("deletions", 0)
            lines.append(f"### {status}: {name} (+{adds}/-{dels})")
            patch = f.get("patch")
            if patch:
                truncated = patch[:per_file_cap]
                if len(patch) > per_file_cap:
                    truncated += "\n... (patch truncated)"
                lines.append("```diff")
                lines.append(truncated)
                lines.append("```")
            else:
                # No patch: binary, or too large for GitHub to inline.
                lines.append("(no textual diff available — binary or oversized file)")
            lines.append("")

            if sum(len(x) for x in lines) >= total_cap:
                lines.append("... (PR context truncated at size cap)")
                break

        return "\n".join(lines)[:total_cap]
