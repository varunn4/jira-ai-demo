"""Repomix-only refresh for testcase generation context."""
from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .config import Config, RepoConfig
from .packer import estimate_tokens, pack_repo
from .state import current_head_sha, fetch_and_pull

log = logging.getLogger(__name__)


@dataclass
class RepomixRepoState:
    name: str
    last_packed_sha: str | None = None
    last_packed_at: str | None = None
    last_status: str = "never_packed"
    last_error: str | None = None


class RepomixStateStore:
    """Tracks packed XML freshness without touching architecture scan state."""

    def __init__(self, path: Path):
        self.path = path
        self._cache = self._load()

    def _load(self) -> dict[str, RepomixRepoState]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text())
        return {name: RepomixRepoState(**data) for name, data in raw.items()}

    def get(self, repo_name: str) -> RepomixRepoState:
        if repo_name not in self._cache:
            self._cache[repo_name] = RepomixRepoState(name=repo_name)
        return self._cache[repo_name]

    def set(self, state: RepomixRepoState) -> None:
        self._cache[state.name] = state
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self._cache.items()}, indent=2))
        tmp.replace(self.path)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_repo_url(value: str) -> str:
    """Normalize common Git remote forms for loose matching."""
    value = value.strip()
    if not value:
        return ""
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        value = f"ssh://{host}/{path}"

    parsed = urlparse(value)
    if parsed.netloc:
        normalized = f"{parsed.netloc}{parsed.path}"
    else:
        normalized = value

    normalized = normalized.lower().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _remote_urls(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "remote", "-v"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    urls: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] not in urls:
            urls.append(parts[1])
    return urls


def _matches_repo_url(repo: RepoConfig, repo_url: str) -> bool:
    wanted = _normalize_repo_url(repo_url)
    if not wanted:
        return False
    try:
        return wanted in {_normalize_repo_url(url) for url in _remote_urls(repo.repo_root)}
    except subprocess.CalledProcessError:
        return False


def _select_repos(
    cfg: Config,
    *,
    repo_names: list[str] | None = None,
    repo_url: str | None = None,
) -> list[RepoConfig]:
    if repo_names:
        by_name = {repo.name: repo for repo in cfg.repos}
        missing = [name for name in repo_names if name not in by_name]
        if missing:
            raise ValueError(f"Unknown repo name(s): {', '.join(missing)}")
        return [by_name[name] for name in repo_names]

    if repo_url and repo_url.strip():
        matches = [repo for repo in cfg.repos if _matches_repo_url(repo, repo_url)]
        if not matches:
            known = ", ".join(repo.name for repo in cfg.repos)
            raise ValueError(f"repoUrl did not match any configured repo. Known repos: {known}")
        return matches

    return list(cfg.repos)


def reindex_repomix(
    cfg: Config,
    *,
    repo_names: list[str] | None = None,
    repo_url: str | None = None,
    do_git_pull: bool = True,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Refresh packed Repomix XML files for selected repos.

    This intentionally uses its own state file. Updating packed XML should not
    advance the architecture-map SHA that `/scan/nightly` uses for patching.
    """
    repos = _select_repos(cfg, repo_names=repo_names, repo_url=repo_url)
    store = RepomixStateStore(cfg.workspace_dir / "repomix_state.json")
    pulled_roots: set[Path] = set()

    packed: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    details: list[dict] = []

    for repo in repos:
        git_pull_status = "not_requested"
        try:
            if do_git_pull and repo.repo_root not in pulled_roots:
                try:
                    git_pull_status = fetch_and_pull(repo.repo_root)
                except subprocess.CalledProcessError as e:
                    git_pull_status = f"failed: {e}"
                    log.warning("[%s] git pull failed locally (%s); using disk state", repo.name, e)
                pulled_roots.add(repo.repo_root)

            sha = current_head_sha(repo.repo_root)
            state = store.get(repo.name)
            packed_file = cfg.packed_dir / f"{repo.name}.xml"

            if not force and packed_file.exists() and state.last_packed_sha == sha:
                skipped.append(repo.name)
                details.append({
                    "repo": repo.name,
                    "status": "skipped",
                    "sha": sha,
                    "git_pull_status": git_pull_status,
                    "output_file": str(packed_file),
                    "reason": "already packed for current HEAD",
                })
                continue

            if progress:
                progress(f"packing {repo.name}")
            pack_repo(repo.path, packed_file, use_skeleton=cfg.use_skeleton, extra_ignores=repo.exclude)
            token_estimate = estimate_tokens(packed_file)
            store.set(RepomixRepoState(
                name=repo.name,
                last_packed_sha=sha,
                last_packed_at=_now(),
                last_status="ok",
                last_error=None,
            ))
            packed.append(repo.name)
            details.append({
                "repo": repo.name,
                "status": "packed",
                "sha": sha,
                "git_pull_status": git_pull_status,
                "output_file": str(packed_file),
                "estimated_tokens": token_estimate,
            })
        except Exception as e:
            log.exception("[%s] repomix reindex failed", repo.name)
            prev = store.get(repo.name)
            store.set(RepomixRepoState(
                name=repo.name,
                last_packed_sha=prev.last_packed_sha,
                last_packed_at=_now(),
                last_status="failed",
                last_error=str(e),
            ))
            failed.append({"repo": repo.name, "error": str(e)})
            details.append({
                "repo": repo.name,
                "status": "failed",
                "git_pull_status": git_pull_status,
                "error": str(e),
            })

    return {
        "selected": [repo.name for repo in repos],
        "packed": packed,
        "skipped": skipped,
        "failed": failed,
        "details": details,
    }
