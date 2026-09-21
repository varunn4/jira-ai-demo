"""Shared test helpers for the RCA suite — build throwaway git repos."""
from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from app.config import settings as _settings


def make_settings(repo_root: Path):
    """Return a copy of app settings pointed at a temp repo root."""
    return dataclasses.replace(_settings, rca_repo_root=str(repo_root))


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def init_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    """Create a git repo `name` under `root` with the given {relpath: content}."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t.test")
    git(repo, "config", "user.name", "Test")
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    return repo


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "-q", "-m", message)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()
