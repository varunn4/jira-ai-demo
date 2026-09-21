"""Concatenates per-repo section maps into three unified master files.

Output:
    workspace/output/architecture.md
    workspace/output/routes.md
    workspace/output/data_models.md

Each master file is organized by repo, with a `# <repo_name>` header per section.
Previous versions are kept as `.bak` so the human reviewer can diff before approving.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterable

from .config import Config
from .sections import ParsedSections, parse_sections

log = logging.getLogger(__name__)


def _backup_if_exists(path: Path) -> None:
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        log.info("backed up %s -> %s", path.name, bak.name)


def combine(cfg: Config) -> None:
    """Build master files from everything in per_repo/."""
    per_repo_files = sorted(cfg.per_repo_dir.glob("*.md"))
    if not per_repo_files:
        log.warning("No per-repo files in %s — nothing to combine.", cfg.per_repo_dir)
        return

    sections_by_repo: dict[str, ParsedSections] = {}
    for f in per_repo_files:
        try:
            sections_by_repo[f.stem] = parse_sections(f.read_text())
        except Exception as e:
            log.error("Skipping %s — could not parse: %s", f, e)

    out_files = {
        "architecture": cfg.output_dir / "architecture.md",
        "routes": cfg.output_dir / "routes.md",
        "data_models": cfg.output_dir / "data_models.md",
    }

    for path in out_files.values():
        _backup_if_exists(path)

    titles = {
        "architecture": "# Architecture Map",
        "routes": "# Routes & Endpoints Map",
        "data_models": "# Data Models Map",
    }
    fields = {
        "architecture": lambda s: s.architecture,
        "routes": lambda s: s.routes,
        "data_models": lambda s: s.data_models,
    }

    for key, out_path in out_files.items():
        parts = [titles[key], ""]
        for repo_name, sec in sections_by_repo.items():
            parts.append(f"## {repo_name}")
            parts.append("")
            parts.append(fields[key](sec).strip())
            parts.append("")
        out_path.write_text("\n".join(parts) + "\n")
        log.info("wrote %s (%d repos)", out_path, len(sections_by_repo))
