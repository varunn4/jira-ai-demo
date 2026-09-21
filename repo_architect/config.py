"""Loads and validates repos.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional in older installs
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


@dataclass
class RepoConfig:
    name: str
    path: Path                           # what we pack and scan
    description: str = ""
    # Extra Repomix ignore patterns for this repo.
    exclude: List[str] = field(default_factory=list)
    # For fan-out subdirs: the parent's git root. When unset, defaults to `path`.
    # The differ uses repo_root for `git diff` and scopes the diff to `path`.
    repo_root: Path | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser().resolve()
        self.exclude = [str(pattern) for pattern in (self.exclude or [])]
        if self.repo_root is not None:
            self.repo_root = Path(self.repo_root).expanduser().resolve()
        else:
            self.repo_root = self.path

    @property
    def subdir_in_repo(self) -> str:
        """Path of `self.path` relative to `self.repo_root`, as a forward-slash
        string suitable for `git diff -- <path>`. Empty string when path == root."""
        try:
            rel = self.path.relative_to(self.repo_root)
        except ValueError:
            return ""
        s = str(rel)
        return "" if s == "." else s


@dataclass
class Config:
    model: str = "claude-sonnet-4-6"
    use_skeleton: bool = True
    # Soft warning threshold for input size.
    max_input_tokens: int = 180_000
    # Hard ceiling — above this we refuse to send. Anthropic's per-request
    # limits make anything past ~600k input tokens a guaranteed 413.
    hard_input_token_ceiling: int = 500_000
    # Cap on generated output. Sonnet 4.6 supports up to 64K. Architecture
    # extraction sometimes runs long (especially the ROUTES section); 32K
    # is a generous ceiling that prevents the 8192-default truncation.
    max_output_tokens: int = 32_000
    repos: List[RepoConfig] = field(default_factory=list)

    # Existing Qdrant/Ollama embeddings used as semantic context for testcase
    # generation. Repomix remains the structural source of truth.
    qdrant_url: str = field(default_factory=lambda: os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/"))
    qdrant_api_key: str = field(default_factory=lambda: os.getenv("QDRANT_API_KEY", ""))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    ollama_embed_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("OLLAMA_EMBED_TIMEOUT_SECONDS", "600"))
    )
    semantic_hit_text_chars: int = field(
        default_factory=lambda: int(os.getenv("SEMANTIC_HIT_TEXT_CHARS", "1200"))
    )
    repomix_file_context_chars: int = field(
        default_factory=lambda: int(os.getenv("REPOMIX_FILE_CONTEXT_CHARS", "4000"))
    )
    repomix_context_max_chars: int = field(
        default_factory=lambda: int(os.getenv("REPOMIX_CONTEXT_MAX_CHARS", "16000"))
    )

    # workspace paths (relative to project root)
    workspace_dir: Path = field(default_factory=lambda: Path("workspace"))

    @property
    def packed_dir(self) -> Path:
        return self.workspace_dir / "packed"

    @property
    def per_repo_dir(self) -> Path:
        return self.workspace_dir / "per_repo"

    @property
    def output_dir(self) -> Path:
        return self.workspace_dir / "output"

    @property
    def state_file(self) -> Path:
        return self.workspace_dir / "state.json"

    def ensure_dirs(self) -> None:
        for d in (self.packed_dir, self.per_repo_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)


def _expand_repo_group(group: dict) -> List[RepoConfig]:
    """Expand a repo_group entry into one RepoConfig per immediate subdirectory.

    Required keys:
      name: prefix for generated repo names
      path: parent directory containing subdirs to scan

    Optional keys:
      include: list of glob patterns; if set, only matching subdir names are kept
      exclude: list of glob patterns; matching subdir names are dropped
      description: applied to all generated repos
    """
    import fnmatch

    group_name = group["name"]
    parent = Path(group["path"]).expanduser().resolve()
    include = group.get("include")  # list of glob patterns or None
    exclude = group.get("exclude", [])
    description = group.get("description", "")

    if not parent.exists():
        raise FileNotFoundError(f"repo_group '{group_name}' path does not exist: {parent}")
    if not parent.is_dir():
        raise ValueError(f"repo_group '{group_name}' path is not a directory: {parent}")

    # Determine the git root for the parent. Two cases:
    #   1. The parent itself is a git repo -> each subdir shares that root.
    #   2. Each subdir is its own git repo -> child's root is the child itself.
    parent_is_git = (parent / ".git").exists()

    subdirs = sorted(
        p for p in parent.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    if include:
        subdirs = [p for p in subdirs if any(fnmatch.fnmatch(p.name, g) for g in include)]
    if exclude:
        subdirs = [p for p in subdirs if not any(fnmatch.fnmatch(p.name, g) for g in exclude)]

    if not subdirs:
        raise ValueError(
            f"repo_group '{group_name}' at {parent} matched 0 subdirs after "
            f"include/exclude filters. Check your patterns."
        )

    out: List[RepoConfig] = []
    for sub in subdirs:
        sub_is_git = (sub / ".git").exists()
        if parent_is_git:
            repo_root = parent
        elif sub_is_git:
            repo_root = sub
        else:
            # Neither — skip with a clear log line. We don't want to silently
            # produce repos that have no git history to diff against.
            import logging
            logging.getLogger(__name__).warning(
                "repo_group '%s' skipping %s: not a git repo and parent isn't either",
                group_name, sub,
            )
            continue

        out.append(RepoConfig(
            name=f"{group_name}__{sub.name}",
            path=sub,
            description=description,
            exclude=exclude,
            repo_root=repo_root,
        ))

    if not out:
        raise ValueError(
            f"repo_group '{group_name}' produced no scannable repos — "
            f"neither parent nor any subdirs are git repos."
        )

    return out


def load_config(path: str | Path = "config/repos.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found at {path}. "
            f"Copy config/repos.example.yaml to {path} and edit it."
        )

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    # Flat list of explicit repos
    repos = [RepoConfig(**r) for r in raw.get("repos", [])]

    # Fan-out groups: each expands into multiple RepoConfigs
    for group in raw.get("repo_groups", []) or []:
        repos.extend(_expand_repo_group(group))

    if not repos:
        raise ValueError(f"No repos or repo_groups defined in {path}")

    # Validate every (now-expanded) repo
    seen_names = set()
    for repo in repos:
        if repo.name in seen_names:
            raise ValueError(f"Duplicate repo name: {repo.name}")
        seen_names.add(repo.name)
        if not repo.path.exists():
            raise FileNotFoundError(f"Repo path does not exist: {repo.path}")
        if not (repo.repo_root / ".git").exists():
            raise ValueError(
                f"Not a git repository: {repo.repo_root} "
                f"(needed for repo '{repo.name}' which scans {repo.path})"
            )

    cfg = Config(
        model=raw.get("model", "claude-sonnet-4-6"),
        use_skeleton=raw.get("use_skeleton", True),
        max_input_tokens=raw.get("max_input_tokens", 180_000),
        hard_input_token_ceiling=raw.get("hard_input_token_ceiling", 500_000),
        max_output_tokens=raw.get("max_output_tokens", 32_000),
        repos=repos,
    )
    cfg.ensure_dirs()
    return cfg


def require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        return "mock-key-local-run"
    return key
