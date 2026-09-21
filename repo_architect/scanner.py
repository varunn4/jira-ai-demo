"""Full-scan orchestration: pack → LLM → parse → write per-repo markdown."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Config, RepoConfig
from .llm import LLMClient
from .packer import pack_repo, estimate_tokens
from .prompts import FULL_SCAN_SYSTEM, full_scan_user_prompt
from .sections import ParsedSections, parse_sections
from .state import RepoState, StateStore, current_head_sha

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    repo: str
    sections: ParsedSections
    sha: str
    input_tokens: int
    output_tokens: int


def scan_repo(
    repo: RepoConfig,
    cfg: Config,
    llm: LLMClient,
) -> ScanResult:
    """Pack a repo, send to Claude, parse sections. Doesn't touch state.

    Caller is responsible for persisting state via StateStore.set().
    """
    log.info("Scanning %s at %s", repo.name, repo.path)

    sha = current_head_sha(repo.repo_root)
    packed_file = cfg.packed_dir / f"{repo.name}.xml"
    pack_repo(repo.path, packed_file, use_skeleton=cfg.use_skeleton, extra_ignores=repo.exclude)

    est_tokens = estimate_tokens(packed_file)
    log.info("  packed %s: ~%d tokens", repo.name, est_tokens)

    # Hard ceiling — refuse to send anything that's guaranteed to 413.
    if est_tokens > cfg.hard_input_token_ceiling:
        raise RuntimeError(
            f"{repo.name} packed to ~{est_tokens} tokens, which exceeds the "
            f"hard ceiling of {cfg.hard_input_token_ceiling}. This repo is too "
            f"large to send in one call. Options: (1) add aggressive ignores to "
            f"packer.py for this repo's noise, (2) enable use_skeleton if not "
            f"already on, (3) split the repo or scan it directory-by-directory."
        )

    if est_tokens > cfg.max_input_tokens:
        log.warning(
            "  %s exceeds max_input_tokens (%d > %d) but is under the hard "
            "ceiling (%d). Sending anyway — output may be expensive.",
            repo.name, est_tokens, cfg.max_input_tokens, cfg.hard_input_token_ceiling,
        )

    packed_text = packed_file.read_text()
    user_prompt = full_scan_user_prompt(repo.name, packed_text, repo.description)

    result = llm.complete(
        system=FULL_SCAN_SYSTEM,
        user=user_prompt,
        cache_system=True,
        max_tokens=cfg.max_output_tokens,
    )
    log.info(
        "  LLM done: input=%d, output=%d, cache_read=%d, cache_create=%d, stop=%s",
        result.input_tokens, result.output_tokens,
        result.cache_read_tokens, result.cache_creation_tokens,
        result.stop_reason,
    )

    # Dump raw response BEFORE parsing — invaluable when parsing fails.
    raw_file = cfg.per_repo_dir / f"{repo.name}.raw.md"
    raw_file.write_text(result.text)

    # Truncation check: if the model hit max_tokens, the output will almost
    # always be missing the final section. Surface that clearly.
    if result.stop_reason == "max_tokens":
        raise RuntimeError(
            f"{repo.name}: LLM output truncated at max_tokens={cfg.max_output_tokens}. "
            f"Raw response saved to {raw_file}. "
            f"Raise max_output_tokens in repos.yaml or shrink the input."
        )

    sections = parse_sections(result.text)

    # Write per-repo markdown
    per_repo_file = cfg.per_repo_dir / f"{repo.name}.md"
    per_repo_file.write_text(sections.render())
    log.info("  wrote %s", per_repo_file)

    return ScanResult(
        repo=repo.name,
        sections=sections,
        sha=sha,
        input_tokens=result.total_input_tokens,
        output_tokens=result.output_tokens,
    )


def scan_all(cfg: Config) -> list[ScanResult]:
    """Scan every repo in the config from scratch. Updates state on success."""
    llm = LLMClient(model=cfg.model)
    store = StateStore(cfg.state_file)
    results: list[ScanResult] = []

    for repo in cfg.repos:
        try:
            result = scan_repo(repo, cfg, llm)
            store.set(RepoState(
                name=repo.name,
                last_sha=result.sha,
                last_scanned_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                last_status="ok",
                last_error=None,
            ))
            results.append(result)
        except Exception as e:
            log.exception("Scan failed for %s", repo.name)
            store.set(RepoState(
                name=repo.name,
                last_sha=store.get(repo.name).last_sha,  # don't advance SHA on failure
                last_scanned_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                last_status="failed",
                last_error=str(e),
            ))

    return results
