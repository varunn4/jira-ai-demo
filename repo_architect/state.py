"""Tracks the last-scanned git SHA per repo for delta sync."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional


@dataclass
class RepoState:
    name: str
    last_sha: Optional[str] = None
    last_scanned_at: Optional[str] = None  # ISO 8601
    last_status: str = "never_scanned"  # "ok" | "failed" | "skipped" | "never_scanned"
    last_error: Optional[str] = None


class StateStore:
    """JSON file storing per-repo state. Simple, no external deps."""

    def __init__(self, path: Path):
        self.path = path
        self._cache: Dict[str, RepoState] = self._load()

    def _load(self) -> Dict[str, RepoState]:
        if not self.path.exists():
            return {}
        with self.path.open() as f:
            raw = json.load(f)
        return {name: RepoState(**data) for name, data in raw.items()}

    def get(self, repo_name: str) -> RepoState:
        if repo_name not in self._cache:
            self._cache[repo_name] = RepoState(name=repo_name)
        return self._cache[repo_name]

    def set(self, state: RepoState) -> None:
        self._cache[state.name] = state
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # atomic write
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(
                {name: asdict(s) for name, s in self._cache.items()},
                f,
                indent=2,
            )
        tmp.replace(self.path)


def current_head_sha(repo_path: Path) -> str:
    """Return the current HEAD commit SHA, or a fallback hash if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        import hashlib, time
        try:
            mtime = repo_path.stat().st_mtime
            return hashlib.sha1(f"{repo_path.name}_{mtime}".encode()).hexdigest()[:16]
        except Exception:
            return hashlib.sha1(f"{repo_path.name}_{time.time()}".encode()).hexdigest()[:16]


def fetch_and_pull(repo_path: Path) -> str:
    """Fetch + fast-forward pull. Returns a status string for logging:

      "ok"           — fetched and pulled cleanly
      "no_remote"    — repo has no remote configured (nothing to pull)
      "auth_failed"  — SSH/HTTPS auth failed; not a transient error
      "network"      — network is down / DNS / proxy issue

    Raises on local errors that genuinely require attention: non-fast-forward,
    dirty working tree, detached HEAD, etc. — those need a human, not a retry.

    Caller is expected to log the status and proceed with whatever's locally
    on disk. The architectural scan should still work on local commits even
    if the remote is unreachable.
    """
    # Check there's a remote at all. Bare local repos and freshly-init repos
    # have no remote; pulling is meaningless.
    remotes = subprocess.run(
        ["git", "remote"], cwd=repo_path, capture_output=True, text=True, check=True,
    )
    if not remotes.stdout.strip():
        return "no_remote"

    fetch = subprocess.run(
        ["git", "fetch", "--all", "--prune"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if fetch.returncode != 0:
        err = (fetch.stderr or "").lower()
        if "permission denied" in err or "publickey" in err or "authentication failed" in err:
            return "auth_failed"
        if "could not resolve host" in err or "connection refused" in err or "timed out" in err:
            return "network"
        # Anything else from fetch — surface it. Could be a corrupt remote ref,
        # a stale lock file, etc.
        raise subprocess.CalledProcessError(
            fetch.returncode, fetch.args, output=fetch.stdout, stderr=fetch.stderr,
        )

    # Fast-forward only. A non-FF pull means someone force-pushed or local
    # commits diverged; we don't want this script silently merging.
    subprocess.run(["git", "pull", "--ff-only"], cwd=repo_path, check=True)
    return "ok"