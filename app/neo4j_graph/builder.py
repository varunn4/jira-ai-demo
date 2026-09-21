"""Orchestrate a Neo4j graph build for the active repositories.

Selects active repos (activity score >= threshold) from the same discovery used
by the Repositories tab, optionally restricted to an explicit name list, then
writes the git + code layers. Emits progress events so a background job can
surface live status to the admin UI.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any, Callable

from app.config import settings
from app.repository_discovery import discover_graph_repositories

from .code_layer import build_code_layer
from .config import GraphBuildConfig, repo_local_path
from .git_layer import build_git_layer
from .writer import Neo4jWriter

log = logging.getLogger(__name__)

ProgressFn = Callable[[dict[str, Any]], None]

WipeMode = str  # "all" | "managed" | "none"


_ERROR_HINTS = ("permission denied", "host key", "unprotected private key",
                "authentication agent", "publickey", "repository not found",
                "connection timed out", "connection refused", "could not resolve",
                "operation timed out", "no route to host", "port 22", "port 443",
                "could not read from remote", "fatal:")


def _meaningful_error(out: str) -> str:
    """Pick the informative line from git/ssh stderr (not the trailing filler)."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in lines:
        if any(h in ln.lower() for h in _ERROR_HINTS):
            return ln
    return lines[0] if lines else "unknown error"


def _git_pull(repo: dict[str, Any]) -> tuple[bool, str]:
    """git pull --ff-only for one repo. Returns (success, output). Best-effort."""
    path = repo_local_path(repo)
    if not path:
        return False, "no local path"
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=path, capture_output=True, text=True, timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def pull_all_repositories(emit: ProgressFn) -> dict[str, Any]:
    """git pull every discovered repo so activity scores + graph see latest code."""
    repos = discover_graph_repositories(settings)
    emit({"phase": "pull", "level": "info", "repos_total": len(repos),
          "message": f"Pulling latest code for {len(repos)} repositories…"})
    pulled = failed = 0
    sample_errors: list[str] = []
    first_full_error = ""
    for repo in repos:
        ok, out = _git_pull(repo)
        if ok:
            pulled += 1
        else:
            failed += 1
            err = _meaningful_error(out)
            log.warning("git pull failed for %s: %s", repo.get("name"), err)
            if not first_full_error and out:
                # Emit the full raw stderr of the first failure so the real ssh
                # cause (not just git's generic 'Could not read…') is visible.
                first_full_error = out[:500]
            if len(sample_errors) < 3:
                sample_errors.append(f"{repo.get('name')}: {err[:160]}")
    msg = f"git pull done: {pulled} ok, {failed} failed"
    if sample_errors:
        msg += " — e.g. " + " | ".join(sample_errors)
    emit({"phase": "pull", "level": "warning" if failed else "info", "message": msg})
    if first_full_error:
        emit({"phase": "pull", "level": "warning",
              "message": "First pull error (raw):\n" + first_full_error})
    return {"pulled": pulled, "failed": failed, "total": len(repos),
            "sample_errors": sample_errors, "first_full_error": first_full_error}


def select_active_repositories(
    cfg: GraphBuildConfig,
    selected_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    repos = discover_graph_repositories(settings)
    active = [r for r in repos if (r.get("activity_score") or 0) >= cfg.activity_min_score]
    if selected_names:
        wanted = {n for n in selected_names}
        active = [r for r in active if r.get("name") in wanted]
    active.sort(key=lambda r: r.get("activity_score") or 0, reverse=True)
    return active


def build_graph(
    *,
    cfg: GraphBuildConfig | None = None,
    selected_names: list[str] | None = None,
    wipe_mode: WipeMode = "managed",
    include_code: bool = True,
    pull_latest: bool | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    cfg = cfg or GraphBuildConfig.from_settings()
    emit = progress or (lambda _e: None)

    if not cfg.neo4j_password:
        raise RuntimeError("NEO4J_PASSWORD is not configured on the server.")

    # Pull latest code FIRST so activity scores reflect fresh remote commits and
    # the graph is built from up-to-date sources.
    do_pull = cfg.pull_latest if pull_latest is None else pull_latest
    pull_summary = pull_all_repositories(emit) if do_pull else None

    active = select_active_repositories(cfg, selected_names)
    emit({"phase": "discovery", "level": "info", "repos_total": len(active),
          "message": f"{len(active)} active repositories selected"})
    if not active:
        return {"repositories": 0, "counts": {}, "repo_names": []}

    started = time.time()
    with Neo4jWriter(
        uri=cfg.neo4j_uri, user=cfg.neo4j_user, password=cfg.neo4j_password,
        database=cfg.neo4j_database, batch_size=cfg.write_batch_size,
    ) as writer:
        writer.verify()
        emit({"phase": "connect", "level": "info",
              "message": f"Connected to Neo4j at {cfg.neo4j_uri}"})

        if wipe_mode == "all":
            emit({"phase": "wipe", "level": "info", "message": "Wiping entire database…"})
            writer.wipe_all()
        elif wipe_mode == "managed":
            emit({"phase": "wipe", "level": "info",
                  "message": "Wiping managed labels (preserving Jira/embeddings)…"})
            writer.wipe(cfg.managed_labels)
        writer.ensure_schema()

        total = len(active)
        for idx, repo in enumerate(active, start=1):
            name = repo.get("name", "repo")
            emit({"phase": "repository", "level": "info", "repo": name,
                  "repos_done": idx - 1, "repos_total": total,
                  "message": f"Building {name} ({idx}/{total})"})
            tracked = build_git_layer(writer, repo, cfg)
            if include_code:
                build_code_layer(writer, repo, tracked, cfg)
            emit({"phase": "repository_done", "level": "info", "repo": name,
                  "repos_done": idx, "repos_total": total,
                  "message": f"Finished {name} ({idx}/{total})"})

        counts = dict(writer.counts)

    elapsed = round(time.time() - started, 1)
    emit({"phase": "done", "level": "info", "repos_done": len(active),
          "repos_total": len(active),
          "message": f"Graph build finished in {elapsed}s ({len(active)} repos)"})
    return {
        "repositories": len(active),
        "repo_names": [r.get("name") for r in active],
        "counts": counts,
        "elapsed_seconds": elapsed,
        "pull": pull_summary,
    }
