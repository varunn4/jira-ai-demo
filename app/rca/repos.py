"""Read-only access to the repo working copies for RCA.

Every operation here is read-only: file reads, `git log`, `git blame`,
`git show`, `git ls-files`, and grep. Nothing writes, checks out, pulls, or
runs project code. Repo names and paths are validated so a tool call can never
escape the configured repo root.

Repos live under ``settings.rca_repo_root``; a repo's name is its immediate
subdirectory there (matching the Neo4j ``Repo.name`` / Qdrant ``repo`` values).
"""
from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.config import Settings

log = logging.getLogger(__name__)

# Cap how much a single read/grep can return so one tool call can't blow the
# agent's token budget. Callers may request narrower ranges.
_MAX_READ_LINES = 400
_MAX_GREP_MATCHES = 100
_GIT_TIMEOUT_SECONDS = 30

# A single matched line longer than this is almost always machine-generated —
# minified JS/CSS, a base64-embedded image/font, or a lock-file hash — never
# human-readable source. Such lines flood grep results with noise that starves
# the agent's token budget, so we drop them from the output.
_MAX_MATCH_LINE_LEN = 500

# Vendored, generated, asset, and DATA paths that carry no application logic but
# match short/common patterns — "aml" inside "yaml"/"seamless", base64 in bundled
# assets, or the substring "AML" inside a name like "KAMLESH" in a committed CSV
# data dump. Excluding them keeps grep focused on real source. Rather than chase
# each new noise source, we block the two whole classes: (1) generated/vendored
# trees, and (2) non-source file types (data, logs, docs, media). `git grep`
# pathspec globs, applied as exclusions after the positive `.` pathspec.
_GREP_EXCLUDE_GLOBS = [
    # generated / vendored / asset trees
    "**/vendor/**", "**/node_modules/**", "**/third_party/**",
    "**/dist/**", "**/build/**", "**/.git/**", "**/public/**",
    "**/storage/**", "**/cache/**", "**/logs/**", "**/logres/**",
    # generated / bundled source
    "**/*.min.js", "**/*.min.css", "**/*.map", "**/*.lock", "**/*-lock.json",
    # non-source data / log / doc / media file types (never application logic)
    "**/*.csv", "**/*.tsv", "**/*.log", "**/*.svg",
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.ico",
    "**/*.pdf", "**/*.xls", "**/*.xlsx", "**/*.woff", "**/*.woff2", "**/*.ttf",
]


class RepoAccessError(RuntimeError):
    """Raised when a repo/path is invalid or a git command fails fatally."""


def _root(settings: Settings) -> Path:
    return Path(settings.rca_repo_root).expanduser().resolve()


def repo_path(settings: Settings, repo: str) -> Path:
    """Resolve a repo name to its on-disk path, refusing traversal.

    The repo name must be a single path segment that resolves to a directory
    directly under the repo root. Anything else (``..``, absolute paths,
    nested slashes) is rejected.
    """
    if not repo or "/" in repo or "\\" in repo or repo in (".", ".."):
        raise RepoAccessError(f"Invalid repo name: {repo!r}")

    root = _root(settings)
    candidate = (root / repo).resolve()
    if candidate.parent != root:
        raise RepoAccessError(f"Repo path escapes root: {repo!r}")
    if not candidate.is_dir():
        raise RepoAccessError(f"Repo not found under {root}: {repo!r}")
    return candidate


def resolve_in_repo(settings: Settings, repo: str, relpath: str) -> Path:
    """Resolve a file path inside a repo, refusing escapes outside the repo."""
    base = repo_path(settings, repo)
    target = (base / relpath).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise RepoAccessError(f"Path escapes repo {repo!r}: {relpath!r}") from exc
    return target


def list_repos(settings: Settings) -> list[str]:
    """List git repositories available under the repo root."""
    root = _root(settings)
    if not root.is_dir():
        log.warning("RCA repo root does not exist: %s", root)
        return []
    repos: list[str] = []
    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        log.warning("Cannot list RCA repo root %s: %s", root, exc)
        return []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir() and (child / ".git").exists():
                repos.append(child.name)
        except OSError:
            continue  # unreadable entry (e.g. restricted system dir) — skip
    return repos


def _git(settings: Settings, repo: str, *args: str) -> str:
    """Run a read-only git command in a repo and return stdout (text)."""
    base = repo_path(settings, repo)
    try:
        proc = subprocess.run(
            ["git", "-C", str(base), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RepoAccessError(f"git {args[0]} failed in {repo}: {exc}") from exc
    if proc.returncode != 0 and not proc.stdout:
        # Many read commands (e.g. blame on an untracked range) exit non-zero
        # with a stderr message; surface it without raising so tools degrade.
        log.debug("git %s in %s rc=%s: %s", args[:1], repo, proc.returncode, proc.stderr.strip()[:200])
    return proc.stdout


def head_sha(settings: Settings, repo: str) -> str:
    """Current HEAD commit SHA, or '' if unavailable."""
    return _git(settings, repo, "rev-parse", "HEAD").strip()


def list_tracked_files(settings: Settings, repo: str) -> list[str]:
    """All git-tracked file paths (repo-relative, posix) for the repo."""
    out = _git(settings, repo, "ls-files")
    return [line for line in out.splitlines() if line]


def read_file(
    settings: Settings,
    repo: str,
    relpath: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> dict[str, Any]:
    """Read a (slice of a) file. 1-based inclusive line range; read-only.

    Returns {repo, path, start, end, total_lines, content, truncated}.
    """
    target = resolve_in_repo(settings, repo, relpath)
    if not target.is_file():
        raise RepoAccessError(f"File not found: {repo}/{relpath}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RepoAccessError(f"Cannot read {repo}/{relpath}: {exc}") from exc

    lines = text.splitlines()
    total = len(lines)
    s = max(1, start or 1)
    e = min(total, end or total)
    if e < s:
        e = s
    # Enforce a hard slice size regardless of the requested range.
    truncated = (e - s + 1) > _MAX_READ_LINES
    if truncated:
        e = s + _MAX_READ_LINES - 1
    selected = lines[s - 1 : e]
    body = "\n".join(f"{s + i}\t{line}" for i, line in enumerate(selected))
    return {
        "repo": repo,
        "path": relpath,
        "start": s,
        "end": e,
        "total_lines": total,
        "content": body,
        "truncated": truncated,
    }


def grep(
    settings: Settings,
    repo: str,
    pattern: str,
    is_regex: bool = False,
    *,
    max_matches: int = _MAX_GREP_MATCHES,
) -> list[dict[str, Any]]:
    """Search tracked files for a pattern via `git grep` (read-only).

    Returns a list of {path, line, text}. Fixed-string by default; set
    is_regex=True for extended-regex matching. Binary files are skipped.
    """
    if not pattern:
        return []
    flags = ["-n", "-I"]  # line numbers, skip binary
    flags.append("-E" if is_regex else "-F")
    # `git grep` over the working tree (no rev) searches tracked files only.
    # Restrict to real source: a positive `.` pathspec plus exclusion globs for
    # vendored/generated/asset trees that only add noise. `:(exclude,glob)` lets
    # the `**` globs span directories.
    pathspecs = ["--", "."] + [f":(exclude,glob){g}" for g in _GREP_EXCLUDE_GLOBS]
    out = _git(settings, repo, "grep", *flags, "-e", pattern, *pathspecs)
    matches: list[dict[str, Any]] = []
    for line in out.splitlines():
        # format: path:lineno:text
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno, text = parts
        if not lineno.isdigit():
            continue
        # Skip minified/base64/lock-hash lines that survived the path filter.
        if len(text) > _MAX_MATCH_LINE_LEN:
            continue
        matches.append({"path": path, "line": int(lineno), "text": text[:300]})
        if len(matches) >= max_matches:
            break
    return matches


def git_log(
    settings: Settings,
    repo: str,
    path: Optional[str] = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Recent commits (optionally for one path). Read-only.

    Returns [{sha, short, author, email, date, subject}].
    """
    limit = max(1, min(limit, 100))
    fmt = "%H%x1f%h%x1f%an%x1f%ae%x1f%cI%x1f%s"
    args = ["log", f"-{limit}", f"--pretty=format:{fmt}"]
    if path:
        # Validate the path lives in the repo, then pass it after `--`.
        resolve_in_repo(settings, repo, path)
        args += ["--", path]
    out = _git(settings, repo, *args)
    commits: list[dict[str, Any]] = []
    for line in out.splitlines():
        f = line.split("\x1f")
        if len(f) != 6:
            continue
        commits.append({
            "sha": f[0], "short": f[1], "author": f[2],
            "email": f[3], "date": f[4], "subject": f[5],
        })
    return commits


def commit_files(settings: Settings, repo: str, sha: str) -> list[str]:
    """Files a commit changed (read-only). Handles merge commits.

    Smart-commit keys frequently land on PR *merge* commits, which have no plain
    diff; for those we diff against the first parent so we get the files the PR
    actually introduced. Returns [] on any error so callers degrade gracefully.
    """
    if not sha:
        return []
    try:
        parents = _git(settings, repo, "rev-list", "--parents", "-n1", sha).split()
        is_merge = len(parents) > 2  # sha + 2+ parents
        if is_merge:
            out = _git(settings, repo, "diff", "--name-only", "--no-renames",
                       f"{sha}^1", sha)
        else:
            out = _git(settings, repo, "diff-tree", "--no-commit-id",
                       "--name-only", "-r", sha)
    except RepoAccessError:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_blame(
    settings: Settings,
    repo: str,
    path: str,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    """Blame a line range. Read-only.

    Returns [{line, sha, author, date, content}] for each line in [start,end].
    """
    resolve_in_repo(settings, repo, path)
    start = max(1, start)
    end = max(start, min(end, start + _MAX_READ_LINES - 1))
    out = _git(
        settings, repo, "blame", "--line-porcelain",
        "-L", f"{start},{end}", "HEAD", "--", path,
    )
    results: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in out.splitlines():
        if line and line[0] != "\t" and " " in line and len(line.split(" ", 1)[0]) == 40:
            # header line: <sha> <orig_line> <final_line> [num_lines]
            head = line.split(" ")
            cur = {"sha": head[0], "line": int(head[2]) if len(head) > 2 and head[2].isdigit() else None}
        elif line.startswith("author "):
            cur["author"] = line[len("author "):]
        elif line.startswith("author-time "):
            cur["date"] = line[len("author-time "):]
        elif line.startswith("\t"):
            cur["content"] = line[1:][:300]
            results.append(cur)
            cur = {}
    return results


@lru_cache(maxsize=1)
def _warn_missing_root(root: str) -> None:  # pragma: no cover - logging helper
    log.warning("RCA repo root %s is missing; git/grep tools will return empty", root)
