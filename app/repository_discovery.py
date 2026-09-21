"""Discover top-level local Git repositories for graph build workflows."""

from __future__ import annotations

import logging
import math
import subprocess
import time
from pathlib import Path
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


def discover_graph_repositories(
    settings: Settings,
    only_names: set[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    root = Path(settings.repository_search_root).expanduser().resolve()
    host_root = Path(settings.repository_host_root)
    excluded_names = {
        name.strip()
        for name in settings.excluded_repository_names.split(",")
        if name.strip()
    }
    wanted = set(only_names) if only_names else None

    log.info("Discovering repositories under %s (excluded: %s, filter: %s)", root, excluded_names or "none", wanted or "all")

    if not root.exists() or not root.is_dir():
        log.warning("Repository search root does not exist: %s", root)
        return []

    repositories: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue

        if child.name.startswith(".") or child.name in excluded_names:
            log.debug("Skipping directory: %s", child.name)
            continue

        if wanted is not None and child.name not in wanted and str(host_root / child.name) not in wanted:
            continue

        if _is_git_repository(child):
            host_path = host_root / child.name
            repositories.append(_repository_info(scan_path=child, host_path=host_path))
            log.debug("Found git repository: %s", child.name)

    repositories.sort(key=lambda repo: repo["path"])
    log.info("Discovered %d repositories", len(repositories))
    return repositories


def active_repository_names(settings: Settings, min_score: int = 1) -> list[str]:
    """Names of repos whose commit-activity score meets ``min_score``.

    "Active" here matches the dashboard's default repo selection (score > 0) and
    honours EXCLUDED_REPOSITORY_NAMES, so callers that only want live repos
    (e.g. the RCA code index) can skip stale/archived clones.
    """
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


# Window (days) over which commit frequency is measured for the activity score.
_ACTIVITY_WINDOW_DAYS = 90
# Recency decay half-life (days): a repo whose last commit is this old keeps half
# of the recency portion of its score.
_RECENCY_HALFLIFE_DAYS = 30
# Commits within the window that earn the full frequency portion of the score.
_FREQUENCY_SATURATION = 25


def _git_activity(path: Path) -> dict[str, Any]:
    """Compute a 0-100 activity score plus the raw signals behind it.

    The score blends recency (how long since the last commit, decayed
    exponentially) and frequency (commit volume over a recent window) so that a
    repo someone touched once long ago scores low while one under steady
    development scores high.
    """
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

    # One log call covers the whole window; we derive 30/90-day counts and the
    # distinct-author count from it instead of issuing several git invocations.
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
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
