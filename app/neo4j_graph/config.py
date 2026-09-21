"""Build-time configuration derived from the app Settings object."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings, settings as _app_settings

_DEFAULT_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", "dist", "build", "target",
    ".next", ".nuxt", ".venv", "venv", "env", "vendor", "qdrant_storage",
    "bower_components", ".repo-cache",
}

# Node labels this builder owns; a scoped wipe touches only these (preserving
# Jira* / EmbeddingDocument nodes built by other pipelines).
MANAGED_LABELS = (
    "Repo", "Author", "Branch", "Commit", "Directory", "File",
    "Module", "Class", "Interface", "Function", "Parameter", "ExternalClass",
)


@dataclass
class GraphBuildConfig:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str
    activity_min_score: int = 1
    max_commits_per_repo: int = 3000
    max_file_bytes: int = 1_500_000
    write_batch_size: int = 1000
    pull_latest: bool = True
    skip_dirs: set[str] = field(default_factory=lambda: set(_DEFAULT_SKIP_DIRS))
    managed_labels: tuple[str, ...] = MANAGED_LABELS

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "GraphBuildConfig":
        s = settings or _app_settings
        uri = s.neo4j_uri
        if "localhost" in uri or "127.0.0.1" in uri:
            uri = "bolt://jira-ai-neo4j:7687"
        password = s.neo4j_password if s.neo4j_password and s.neo4j_password != "your-neo4j-password" else "password"
        return cls(
            neo4j_uri=uri,
            neo4j_user=s.neo4j_user,
            neo4j_password=password,
            neo4j_database=s.neo4j_database,
            activity_min_score=s.neo4j_activity_min_score,
            max_commits_per_repo=s.neo4j_max_commits_per_repo,
            max_file_bytes=s.neo4j_max_file_bytes,
            write_batch_size=s.neo4j_write_batch_size,
            pull_latest=s.neo4j_build_pull_latest,
        )


def repo_local_path(repo: dict) -> str:
    """Path to read git/files from inside this process (container path wins)."""
    return repo.get("container_path") or repo.get("path") or ""
