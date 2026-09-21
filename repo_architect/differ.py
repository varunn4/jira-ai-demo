"""Git diff helpers for the nightly delta sync.

Strategy: for each repo, diff `last_scanned_sha..HEAD`. From the diff we extract:
  - the unified diff text itself (passed to the LLM)
  - the list of changed files (so we can read their current full source)

Filtering: we skip diffs that only touch ignored paths (tests, migrations,
lockfiles, etc.) to avoid waste. If after filtering the diff is empty, the
repo is treated as unchanged.
"""
from __future__ import annotations

import fnmatch
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Paths that change often but never affect architecture — skip them.
IGNORE_GLOBS = (
    "**/*.lock",
    "**/*.min.js",
    "**/*.min.css",
    "**/package-lock.json",
    "**/yarn.lock",
    "**/poetry.lock",
    "**/Pipfile.lock",
    "**/__pycache__/**",
    "**/migrations/**",
    "**/.pytest_cache/**",
    "**/coverage/**",
    "**/.next/**",
    "**/dist/**",
    "**/build/**",
    "**/node_modules/**",
    "**/*.log",
    "**/*.md",          # docs themselves don't affect arch maps
    "**/*.test.js",
    "**/*.test.ts",
    "**/*.spec.js",
    "**/*.spec.ts",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
)


@dataclass
class RepoDelta:
    diff_text: str
    changed_files: list[str]  # paths relative to repo root, after filtering
    skipped: bool             # True if no meaningful changes


def _is_ignored(path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in IGNORE_GLOBS)


def _changed_files(
    repo_path: Path, from_sha: str, to_sha: str = "HEAD", subdir: str = ""
) -> list[str]:
    cmd = ["git", "diff", "--name-only", f"{from_sha}..{to_sha}"]
    if subdir:
        cmd.extend(["--", subdir])
    result = subprocess.run(cmd, cwd=repo_path, check=True, capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _unified_diff(
    repo_path: Path, from_sha: str, to_sha: str, paths: list[str]
) -> str:
    """Diff but restricted to the given paths."""
    if not paths:
        return ""
    cmd = ["git", "diff", f"{from_sha}..{to_sha}", "--", *paths]
    result = subprocess.run(cmd, cwd=repo_path, check=True, capture_output=True, text=True)
    return result.stdout


def compute_delta(
    repo_path: Path, from_sha: str, to_sha: str = "HEAD", subdir: str = "",
) -> RepoDelta:
    """Compute the filtered diff between two commits.

    `repo_path` is the git root. `subdir` (optional) scopes the diff to a
    single subdirectory — used when one parent repo fans out into multiple
    scan units (e.g., aws-configs/lambdas/ and aws-configs/iac/ are tracked
    separately even though they share a git root).
    """
    all_changed = _changed_files(repo_path, from_sha, to_sha, subdir=subdir)
    meaningful = [p for p in all_changed if not _is_ignored(p)]

    log.info(
        "  delta: %d changed files%s, %d after filtering",
        len(all_changed),
        f" in {subdir}/" if subdir else "",
        len(meaningful),
    )

    if not meaningful:
        return RepoDelta(diff_text="", changed_files=[], skipped=True)

    diff_text = _unified_diff(repo_path, from_sha, to_sha, meaningful)
    return RepoDelta(diff_text=diff_text, changed_files=meaningful, skipped=False)


def read_changed_sources(
    repo_path: Path, files: list[str], max_chars_per_file: int = 30_000
) -> str:
    """Read current contents of changed files for LLM context.

    Truncated per-file to keep the prompt bounded. Deleted files won't exist
    and are skipped (their disappearance is already in the diff).
    """
    parts = []
    for rel in files:
        abs_path = repo_path / rel
        if not abs_path.exists():
            parts.append(f"### {rel}\n[DELETED]\n")
            continue
        try:
            content = abs_path.read_text(errors="replace")
        except Exception as e:
            parts.append(f"### {rel}\n[unreadable: {e}]\n")
            continue
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + f"\n... [truncated, {len(content)} chars total]"
        parts.append(f"### {rel}\n```\n{content}\n```\n")
    return "\n".join(parts)