"""Tree-sitter chunker: split repo source into function/class units.

Reuses the same tree-sitter parsing the Neo4j code layer uses
(``app.neo4j_graph.code_layer.parse_file``) so chunk boundaries match the graph
exactly — a symbol resolved in the graph maps cleanly to a chunk here.

Each chunk is a function/method/class unit with its source text and line range.
This module only reads files; it never writes anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.config import Settings
from app.neo4j_graph.code_layer import LANG_BY_EXT, parse_file
from app.rca import repos

log = logging.getLogger(__name__)

# Cap chunk text so one symbol can't dominate an embedding request; long bodies
# are truncated (the head carries the most identifying signal for retrieval).
_MAX_CHUNK_CHARS = 4000


@dataclass(frozen=True)
class CodeChunk:
    repo: str
    file_path: str
    symbol: str
    start_line: int
    end_line: int
    language: str
    chunk_type: str  # "function" | "method" | "class" | "interface"
    text: str

    def point_seed(self) -> str:
        """Stable identity for incremental upserts (independent of commit)."""
        return f"{self.repo}:{self.file_path}:{self.chunk_type}:{self.symbol}:{self.start_line}"

    def embed_text(self) -> str:
        """Text fed to the embedder — a small locating header plus the body."""
        header = (
            f"repo: {self.repo}\n"
            f"path: {self.file_path}\n"
            f"symbol: {self.symbol} ({self.chunk_type}, {self.language})\n"
        )
        return (header + "\n" + self.text)[:_MAX_CHUNK_CHARS]


def language_for(relpath: str) -> str | None:
    return LANG_BY_EXT.get(PurePosixPath(relpath).suffix.lower())


def chunk_file(
    settings: Settings,
    repo: str,
    relpath: str,
) -> list[CodeChunk]:
    """Parse one source file into function/class chunks. Returns [] if unparsable."""
    lang = language_for(relpath)
    if not lang:
        return []
    try:
        abspath = repos.resolve_in_repo(settings, repo, relpath)
    except repos.RepoAccessError:
        return []
    try:
        if abspath.stat().st_size > settings.rca_chunk_max_file_bytes:
            return []
        src = abspath.read_bytes()
    except OSError:
        return []

    try:
        fp = parse_file(repo, relpath, src, lang)
    except Exception as exc:  # never let one bad file kill indexing
        log.debug("[%s] chunk parse failed for %s: %s", repo, relpath, exc)
        return []

    lines = src.decode("utf-8", errors="replace").splitlines()

    def _slice(start: int, end: int) -> str:
        return "\n".join(lines[max(0, start - 1) : end])

    chunks: list[CodeChunk] = []
    for c in fp.classes:
        chunks.append(CodeChunk(
            repo=repo, file_path=relpath, symbol=c["name"],
            start_line=c["start"], end_line=c["end"], language=lang,
            chunk_type="interface" if c.get("label") == "Interface" else "class",
            text=_slice(c["start"], c["end"]),
        ))
    for f in fp.functions:
        chunks.append(CodeChunk(
            repo=repo, file_path=relpath, symbol=f["name"],
            start_line=f["start"], end_line=f["end"], language=lang,
            chunk_type="method" if f.get("enclosing_class") else "function",
            text=_slice(f["start"], f["end"]),
        ))
    return chunks


def iter_repo_chunks(
    settings: Settings,
    repo: str,
    relpaths: list[str] | None = None,
) -> Iterator[CodeChunk]:
    """Yield chunks for the given files (or all tracked source files)."""
    files = relpaths if relpaths is not None else repos.list_tracked_files(settings, repo)
    for rel in files:
        if language_for(rel) is None:
            continue
        for chunk in chunk_file(settings, repo, rel):
            yield chunk
