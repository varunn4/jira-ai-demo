"""Discover top-level Git repositories and synchronize GitHub repositories for graph and analysis workflows."""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error
import json

from app.config import Settings

log = logging.getLogger(__name__)


def get_workspace_repos_dir() -> Path:
    """Return the managed workspace directory for cloned/synced repositories."""
    base = Path("/app/workspace/repos") if Path("/app").exists() else Path("./workspace/repos")
    base.mkdir(parents=True, exist_ok=True)
    return base


def discover_graph_repositories(
    settings: Settings,
    only_names: set[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Discover all Git repositories synchronized into the managed workspace."""
    workspace_dir = get_workspace_repos_dir()

    excluded_names = {
        name.strip().lower()
        for name in settings.excluded_repository_names.split(",")
        if name.strip()
    }
    wanted = set(only_names) if only_names else None

    log.info("Discovering repositories under %s (filter: %s)", workspace_dir, wanted or "all")

    seen_names: set[str] = set()
    repositories: list[dict[str, Any]] = []

    def _add_if_git(scan_path: Path, host_path: Path):
        name = scan_path.name
        if name.lower() in seen_names or name.lower() in excluded_names or name.startswith("."):
            return
        if wanted is not None and name not in wanted and str(host_path) not in wanted:
            return
        if _is_git_repository(scan_path):
            seen_names.add(name.lower())
            repositories.append(_repository_info(scan_path=scan_path, host_path=host_path))
            log.debug("Found git repository: %s at %s", name, scan_path)

    # Scan workspace repositories directory (cloned from GitHub)
    if workspace_dir.exists() and workspace_dir.is_dir():
        try:
            for child in sorted(workspace_dir.iterdir()):
                if child.is_dir():
                    _add_if_git(child, child)
        except Exception as exc:
            log.warning("Error scanning workspace repos %s: %s", workspace_dir, exc)

    repositories.sort(key=lambda repo: repo["name"].lower())
    log.info("Discovered %d repositories in workspace", len(repositories))
    return repositories


def active_repository_names(settings: Settings, min_score: int = 1) -> list[str]:
    """Names of repos whose commit-activity score meets ``min_score``."""
    return [
        repo["name"]
        for repo in discover_graph_repositories(settings)
        if (repo.get("activity_score") or 0) >= min_score
    ]


def _is_git_repository(path: Path) -> bool:
    return (path / ".git").exists()


def _repository_info(*, scan_path: Path, host_path: Path) -> dict[str, Any]:
    remote_url = _git(scan_path, "config", "--get", "remote.origin.url")
    branch = _git(scan_path, "rev-parse", "--abbrev-ref", "HEAD")
    current_commit = _git(scan_path, "rev-parse", "HEAD")
    return {
        "name": scan_path.name,
        "path": str(host_path),
        "container_path": str(scan_path),
        "local_clone_available": True,
        "remote_url": remote_url or "",
        "branch": branch or "",
        "current_commit": current_commit or "",
        "pull_command": f"git -C {host_path} pull --ff-only",
        **_git_activity(scan_path),
    }


# ─── GitHub API Helpers ────────────────────────────────────────────────────────

def parse_github_repo_slug(url_or_slug: str) -> str | None:
    """Extract owner/repo from URL or slug like 'AonamiTech/trail' or 'https://github.com/AonamiTech/trail.git'."""
    s = url_or_slug.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    m = re.search(r"(?:github\.com/|github\.com:)?([^/:]+)/([^/]+)$", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


def fetch_github_org_repos(org_or_user: str, token: str = "") -> list[dict[str, Any]]:
    """Fetch all repositories for a GitHub organization or user."""
    slug = org_or_user.strip().lstrip("@")
    if not slug:
        return []

    # Clean URL if full user URL passed
    if "github.com/" in slug:
        slug = slug.split("github.com/")[-1].strip("/")

    clean_token = token.strip().strip('"').strip("'") if token else ""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Jira-AI-Agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if clean_token:
        headers["Authorization"] = f"Bearer {clean_token}"

    # Try organization first, then user, then authenticated user repos if token provided
    urls_to_try = [
        f"https://api.github.com/orgs/{slug}/repos?per_page=100&sort=updated",
        f"https://api.github.com/users/{slug}/repos?per_page=100&sort=updated",
    ]

    for api_url in urls_to_try:
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    if isinstance(data, list):
                        return [
                            {
                                "name": r.get("name"),
                                "full_name": r.get("full_name"),
                                "clone_url": r.get("clone_url"),
                                "html_url": r.get("html_url"),
                                "default_branch": r.get("default_branch", "main"),
                                "description": r.get("description") or "",
                                "private": r.get("private", False),
                                "stars": r.get("stargazers_count", 0),
                                "updated_at": r.get("updated_at"),
                            }
                            for r in data
                            if r.get("name")
                        ]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            if exc.code == 401:
                raise RuntimeError("GitHub API returned 401 Unauthorized: The provided Personal Access Token (PAT) is invalid, expired, or does not have access. Please verify your token at github.com/settings/tokens.")
            raise RuntimeError(f"GitHub API error ({exc.code}): {exc.reason}")
        except Exception as exc:
            if "401" in str(exc):
                raise RuntimeError("GitHub API returned 401 Unauthorized: The provided Personal Access Token (PAT) is invalid, expired, or does not have access. Please verify your token at github.com/settings/tokens.")
            raise RuntimeError(f"Failed to connect to GitHub API: {exc}")

    raise RuntimeError(f"GitHub organization or user '{slug}' not found.")


def fetch_single_github_repo(url_or_slug: str, token: str = "") -> dict[str, Any]:
    """Fetch metadata for a single GitHub repository URL."""
    slug = parse_github_repo_slug(url_or_slug)
    if not slug:
        raise ValueError(f"Invalid GitHub URL or repository slug: '{url_or_slug}'")

    api_url = f"https://api.github.com/repos/{slug}"
    clean_token = token.strip().strip('"').strip("'") if token else ""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Jira-AI-Agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if clean_token:
        headers["Authorization"] = f"Bearer {clean_token}"

    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as response:
            if response.status == 200:
                r = json.loads(response.read().decode("utf-8"))
                return {
                    "name": r.get("name"),
                    "full_name": r.get("full_name"),
                    "clone_url": r.get("clone_url"),
                    "html_url": r.get("html_url"),
                    "default_branch": r.get("default_branch", "main"),
                    "description": r.get("description") or "",
                    "private": r.get("private", False),
                    "stars": r.get("stargazers_count", 0),
                    "updated_at": r.get("updated_at"),
                }
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Repository '{slug}' not found on GitHub. If it is private, provide a valid GitHub Personal Access Token (PAT) with 'repo' scope.")
        if exc.code == 401:
            raise RuntimeError("GitHub API returned 401 Unauthorized: The provided Personal Access Token (PAT) is invalid, expired, or does not have access to this repository. Please verify your token.")
        raise RuntimeError(f"GitHub API error ({exc.code}): {exc.reason}")
    except Exception as exc:
        if "401" in str(exc):
            raise RuntimeError("GitHub API returned 401 Unauthorized: The provided Personal Access Token (PAT) is invalid, expired, or does not have access to this repository.")
        raise RuntimeError(f"Failed to fetch GitHub repository '{slug}': {exc}")


def clone_or_sync_repo(
    clone_url: str,
    target_name: str,
    token: str = "",
    branch: str = "main",
) -> dict[str, Any]:
    """Clone or pull latest commits of a repository into the managed workspace."""
    workspace = get_workspace_repos_dir()
    repo_dir = workspace / target_name

    auth_url = clone_url
    if token and "github.com/" in clone_url:
        auth_url = clone_url.replace("https://", f"https://x-access-token:{token.strip()}@")

    if (repo_dir / ".git").exists():
        # Sync / pull latest
        log.info("Updating existing workspace repository: %s", target_name)
        _git(repo_dir, "fetch", "--all", "--prune")
        _git(repo_dir, "checkout", branch)
        _git(repo_dir, "pull", "--ff-only")
    else:
        # Clone repo
        log.info("Cloning repository %s into %s", clone_url, repo_dir)
        cmd = ["git", "clone", "--depth", "100", auth_url, str(repo_dir)]
        if branch:
            cmd.extend(["-b", branch])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            # If specified branch failed, try default clone
            res = subprocess.run(["git", "clone", "--depth", "100", auth_url, str(repo_dir)], capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to clone repository '{target_name}': {res.stderr.strip() or res.stdout.strip()}")

    return _repository_info(scan_path=repo_dir, host_path=repo_dir)


# ─── Git Activity Calculation ──────────────────────────────────────────────────

_ACTIVITY_WINDOW_DAYS = 90
_RECENCY_HALFLIFE_DAYS = 30
_FREQUENCY_SATURATION = 25


def _git_activity(path: Path) -> dict[str, Any]:
    """Compute a 0-100 activity score plus the raw signals behind it."""
    now = time.time()

    last_raw = _git(path, "log", "-1", "--format=%ct|%s|%an|%cr")
    last_ts = None
    last_message = ""
    last_author = ""
    last_relative = ""
    if last_raw:
        parts = last_raw.split("|", 3)
        if len(parts) >= 1 and parts[0].isdigit():
            last_ts = int(parts[0])
        if len(parts) >= 2:
            last_message = parts[1]
        if len(parts) >= 3:
            last_author = parts[2]
        if len(parts) >= 4:
            last_relative = parts[3]

    since = f"{_ACTIVITY_WINDOW_DAYS} days ago"
    log_raw = _git(path, "log", f"--since={since}", "--format=%ct|%ae") or ""
    commits_90d = 0
    commits_30d = 0
    authors: set[str] = set()
    cutoff_30d = now - 30 * 86400
    for line in log_raw.splitlines():
        ts_str, _, author = line.partition("|")
        if not ts_str.isdigit():
            continue
        commits_90d += 1
        if int(ts_str) >= cutoff_30d:
            commits_30d += 1
        if author:
            authors.add(author)

    if last_ts is None:
        days_since_last = None
        recency = 0.0
    else:
        days_since_last = max(0.0, (now - last_ts) / 86400)
        recency = 60.0 * math.pow(0.5, days_since_last / _RECENCY_HALFLIFE_DAYS)

    frequency = 40.0 * min(1.0, commits_90d / _FREQUENCY_SATURATION)
    score = int(round(recency + frequency))

    return {
        "activity_score": score,
        "recency_score": round(recency, 1),
        "frequency_score": round(frequency, 1),
        "last_commit_ts": last_ts,
        "last_commit_days": round(days_since_last, 1) if days_since_last is not None else None,
        "last_commit_message": last_message,
        "last_commit_author": last_author,
        "last_commit_relative": last_relative,
        "commits_30d": commits_30d,
        "commits_90d": commits_90d,
        "authors_90d": len(authors),
    }


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
