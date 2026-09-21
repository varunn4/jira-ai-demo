"""Nightly delta sync orchestration: per-repo surgical patches.

Flow per repo:
  1. `git fetch && git pull --ff-only` to refresh the working copy
  2. Compare HEAD against last-scanned SHA
  3. If no meaningful changes -> skip, log, advance SHA (the working tree did
     change, but nothing relevant — record the new SHA so we don't re-diff
     the same noise tomorrow)
  4. Otherwise: load the existing per-repo markdown, compute the diff + changed
     file contents, send the surgical patching prompt, parse the response,
     write back to per_repo/

After all repos finish, the combiner re-runs to produce fresh master files.
"""
from __future__ import annotations

import datetime as dt
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .combiner import combine
from .config import Config, RepoConfig
from .differ import compute_delta, read_changed_sources
from .llm import LLMClient
from .prompts import PATCH_SYSTEM, patch_user_prompt
from .scanner import scan_repo
from .sections import ParsedSections, parse_sections
from .state import RepoState, StateStore, current_head_sha, fetch_and_pull

log = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    scanned_full: list[str]
    patched: list[str]
    skipped: list[str]
    failed: list[tuple[str, str]]  # (repo_name, error)


def _load_existing_sections(per_repo_dir: Path, repo_name: str) -> ParsedSections | None:
    f = per_repo_dir / f"{repo_name}.md"
    if not f.exists():
        return None
    try:
        return parse_sections(f.read_text())
    except Exception as e:
        log.error("Could not parse existing %s: %s", f, e)
        return None


def patch_repo(repo: RepoConfig, from_sha: str, cfg: Config, llm: LLMClient) -> str:
    """Patch one repo. Returns the new HEAD SHA after the patch.

    Raises on failure; caller handles state.
    """
    existing = _load_existing_sections(cfg.per_repo_dir, repo.name)
    if existing is None:
        raise RuntimeError(
            f"No existing per-repo doc for {repo.name} — run a full scan first."
        )

    to_sha = current_head_sha(repo.repo_root)
    if to_sha == from_sha:
        log.info("  %s: HEAD unchanged (%s)", repo.name, to_sha[:8])
        return to_sha

    delta = compute_delta(
        repo.repo_root, from_sha, to_sha, subdir=repo.subdir_in_repo,
    )
    if delta.skipped:
        log.info(
            "  %s: %s..%s touched only ignored paths — advancing SHA, no LLM call",
            repo.name, from_sha[:8], to_sha[:8],
        )
        return to_sha

    sources = read_changed_sources(repo.repo_root, delta.changed_files)
    user_prompt = patch_user_prompt(
        repo_name=repo.name,
        current_sections=existing.render(),
        diff=delta.diff_text,
        changed_files_source=sources,
    )

    result = llm.complete(
        system=PATCH_SYSTEM,
        user=user_prompt,
        cache_system=True,
        max_tokens=cfg.max_output_tokens,
    )
    log.info(
        "  %s patched: input=%d, output=%d, cache_read=%d, stop=%s",
        repo.name, result.input_tokens, result.output_tokens,
        result.cache_read_tokens, result.stop_reason,
    )

    # Same truncation guard as scanner.
    if result.stop_reason == "max_tokens":
        raw_file = cfg.per_repo_dir / f"{repo.name}.patch.raw.md"
        raw_file.write_text(result.text)
        raise RuntimeError(
            f"{repo.name}: patch output truncated at max_tokens={cfg.max_output_tokens}. "
            f"Raw response saved to {raw_file}."
        )

    new_sections = parse_sections(result.text)

    # Back up the old per-repo file before overwriting
    per_repo_file = cfg.per_repo_dir / f"{repo.name}.md"
    if per_repo_file.exists():
        bak = per_repo_file.with_suffix(".md.bak")
        bak.write_text(per_repo_file.read_text())

    per_repo_file.write_text(new_sections.render())
    return to_sha


def sync_all(cfg: Config, do_git_pull: bool = True) -> SyncSummary:
    """Run nightly delta sync across all repos."""
    llm = LLMClient(model=cfg.model)
    store = StateStore(cfg.state_file)

    summary = SyncSummary(scanned_full=[], patched=[], skipped=[], failed=[])

    # Pull each git root exactly once, even if multiple fan-out children share it.
    pulled_roots: set[Path] = set()

    for repo in cfg.repos:
        try:
            if do_git_pull and repo.repo_root not in pulled_roots:
                try:
                    status = fetch_and_pull(repo.repo_root)
                    if status != "ok":
                        log.warning(
                            "[%s] git pull skipped (%s) — using whatever's "
                            "locally on disk. Fix the remote/auth setup if "
                            "you want nightly remote updates.",
                            repo.name, status,
                        )
                except subprocess.CalledProcessError as e:
                    # Real local-side git error (non-FF, dirty tree, etc.) — log
                    # and proceed. Don't kill the whole sync over one repo.
                    log.warning(
                        "[%s] git pull failed locally (%s); using disk state",
                        repo.name, e,
                    )
                pulled_roots.add(repo.repo_root)

            state = store.get(repo.name)

            # Cold start: never scanned this repo -> do a full scan
            if not state.last_sha:
                log.info("[%s] no prior state — running full scan", repo.name)
                result = scan_repo(repo, cfg, llm)
                store.set(RepoState(
                    name=repo.name,
                    last_sha=result.sha,
                    last_scanned_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                    last_status="ok",
                ))
                summary.scanned_full.append(repo.name)
                continue

            log.info("[%s] patching from %s", repo.name, state.last_sha[:8])
            new_sha = patch_repo(repo, state.last_sha, cfg, llm)

            store.set(RepoState(
                name=repo.name,
                last_sha=new_sha,
                last_scanned_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                last_status="ok",
            ))

            if new_sha == state.last_sha:
                summary.skipped.append(repo.name)
            else:
                summary.patched.append(repo.name)

        except Exception as e:
            log.exception("[%s] failed", repo.name)
            prev = store.get(repo.name)
            store.set(RepoState(
                name=repo.name,
                last_sha=prev.last_sha,  # never advance on failure
                last_scanned_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                last_status="failed",
                last_error=str(e),
            ))
            summary.failed.append((repo.name, str(e)))

    # Re-build master files from whatever the patches produced
    log.info("Re-combining master files...")
    combine(cfg)

    return summary