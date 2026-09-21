"""Local codebase scanning and embedding ingestion.

This is intentionally lightweight and language-agnostic enough for mixed
repositories. It scans first-party source files and stores model-specific file
embeddings in Qdrant collections.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from app.ollama_embedder import OllamaEmbedder
from app.qdrant_store import upsert_codebase_embeddings

log = logging.getLogger(__name__)

CODEBASE_EMBEDDING_MODELS: dict[str, dict[str, str]] = {
    "codebase_bge_m3": {
        "ollama_model": "bge-m3",
        "label": "BGE-M3",
    },
    "codebase_qwen3_0_6b": {
        "ollama_model": "qwen3:0.6b",
        "label": "Qwen3 0.6B",
    },
    "codebase_mxbai_large": {
        "ollama_model": "mxbai-embed-large",
        "label": "mxbai large",
    },
}

_SOURCE_EXTENSIONS = {
    ".php",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".java",
    ".go",
    ".rb",
    ".cs",
    ".sql",
    ".blade.php",
}

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".venv",
    "venv",
    "env",
    "qdrant_storage",
}

_LANGUAGE_BY_EXTENSION = {
    ".php": "PHP",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".py": "Python",
    ".java": "Java",
    ".go": "Go",
    ".rb": "Ruby",
    ".cs": "C#",
    ".sql": "SQL",
}


def build_codebase_embedding_documents(
    repo: dict[str, Any],
    *,
    max_files: int = 2000,
    max_chars: int = 2000,
) -> list[dict[str, Any]]:
    """Return first-party source-file documents ready for embedding."""
    scan = scan_repository(repo, include_text=True, max_text_bytes=250_000)
    docs = []
    for file_row in scan["files"]:
        text = file_row.pop("text", "")
        if not text.strip():
            continue
        docs.append(
            {
                "id": f"{scan['repo']['full_name']}:{file_row['path']}",
                "repo": scan["repo"]["full_name"],
                "repo_name": scan["repo"]["name"],
                "path": file_row["path"],
                "language": file_row["language"],
                "extension": file_row["extension"],
                "lines": file_row["lines"],
                "text": _embedding_text(scan["repo"], file_row, text, max_chars=max_chars),
            }
        )
        if len(docs) >= max_files:
            break
    return docs


def embed_codebase_documents(
    *,
    qdrant_url: str,
    qdrant_api_key: str,
    ollama_url: str,
    ollama_timeout_seconds: int,
    ollama_batch_size: int,
    ollama_concurrency: int,
    embedding_model_key: str,
    documents: list[dict[str, Any]],
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """Generate and store codebase embeddings in a model-specific Qdrant collection."""
    model = CODEBASE_EMBEDDING_MODELS.get(embedding_model_key, CODEBASE_EMBEDDING_MODELS["codebase_bge_m3"])
    embedder = OllamaEmbedder(
        ollama_url,
        model["ollama_model"],
        timeout_seconds=ollama_timeout_seconds,
        batch_size=ollama_batch_size,
        concurrency=ollama_concurrency,
    )
    if not embedder.is_available():
        log.warning("Ollama model %s unavailable; skipping codebase embeddings", model["ollama_model"])
        return 0
    embeddings = embedder.embed_batch(
        [doc["text"] for doc in documents],
        progress_callback=progress_callback,
    )
    return upsert_codebase_embeddings(
        qdrant_url=qdrant_url,
        collection_name=embedding_model_key,
        documents=documents,
        embeddings=embeddings,
        model_key=embedding_model_key,
        model_name=model["ollama_model"],
        api_key=qdrant_api_key or None,
    )


def scan_repository(
    repo: dict[str, Any],
    *,
    include_text: bool = False,
    max_text_bytes: int = 250_000,
) -> dict[str, Any]:
    root_value = repo.get("container_path") or repo.get("path")
    root = Path(root_value) if root_value else Path("/path/not/configured")
    full_name = repo.get("name") or root.name
    files: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    import_edges: list[dict[str, str]] = []
    call_edges: list[dict[str, str]] = []
    file_paths: set[str] = set()
    language_counts: Counter[str] = Counter()

    if not root.exists():
        return _empty_scan(repo, root)

    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for filename in filenames:
            path = Path(current_root) / filename
            rel_path = path.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel_path.parts):
                continue
            ext = _extension(path)
            if ext not in _SOURCE_EXTENSIONS:
                continue
            rel = str(rel_path)
            text = _read_text(path, max_bytes=max_text_bytes)
            if text is None:
                continue
            language = _LANGUAGE_BY_EXTENSION.get(ext, "Other")
            language_counts[language] += 1
            file_paths.add(rel)
            row = {
                "path": rel,
                "extension": ext,
                "language": language,
                "lines": text.count("\n") + (1 if text else 0),
                "size_bytes": path.stat().st_size,
            }
            if include_text:
                row["text"] = text
            files.append(row)

    function_by_name: dict[str, list[str]] = {}
    file_text: dict[str, str] = {}
    for file_row in files:
        path = root / file_row["path"]
        text = file_row.get("text") if include_text else _read_text(path, max_bytes=max_text_bytes)
        if text is None:
            continue
        file_text[file_row["path"]] = text
        imports = _extract_imports(text, file_row["extension"], file_row["path"], file_paths)
        import_edges.extend(imports)
        for fn in _extract_functions(text, file_row, full_name):
            functions.append(fn)
            function_by_name.setdefault(fn["name"], []).append(fn["qualified_name"])

    for fn in functions:
        text = file_text.get(fn["file_path"], "")
        body = _line_window(text, fn["start_line"], fn["end_line"])
        for call_name in _extract_call_names(body):
            targets = function_by_name.get(call_name) or []
            for target in targets[:5]:
                if target != fn["qualified_name"]:
                    call_edges.append({"caller": fn["qualified_name"], "callee": target})

    repo_row = {
        "name": repo.get("name") or root.name,
        "full_name": full_name,
        "url": repo.get("remote_url", ""),
        "local_path": repo.get("path") or str(root),
        "default_branch": repo.get("branch", ""),
        "language": language_counts.most_common(1)[0][0] if language_counts else "",
    }
    return {
        "repo": repo_row,
        "files": files,
        "functions": functions,
        "imports": _dedupe_edges(import_edges, ("source_path", "target_path", "kind")),
        "calls": _dedupe_edges(call_edges, ("caller", "callee")),
    }


def _empty_scan(repo: dict[str, Any], root: Path) -> dict[str, Any]:
    name = repo.get("name") or root.name
    return {
        "repo": {
            "name": name,
            "full_name": name,
            "url": repo.get("remote_url", ""),
            "local_path": repo.get("path") or str(root),
            "default_branch": repo.get("branch", ""),
            "language": "",
        },
        "files": [],
        "functions": [],
        "imports": [],
        "calls": [],
    }


def _extension(path: Path) -> str:
    if path.name.endswith(".blade.php"):
        return ".blade.php"
    return path.suffix.lower()


def _read_text(path: Path, max_bytes: int) -> Optional[str]:
    try:
        if path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _extract_imports(text: str, ext: str, source_path: str, file_paths: set[str]) -> list[dict[str, str]]:
    edges = []
    raw_imports: list[str] = []
    if ext in {".js", ".jsx", ".ts", ".tsx"}:
        raw_imports.extend(match.group(1) for match in re.finditer(r"(?:from\s+|require\()\s*['\"]([^'\"]+)['\"]", text))
    elif ext in {".php", ".blade.php"}:
        raw_imports.extend(match.group(1) for match in re.finditer(r"\b(?:include|include_once|require|require_once)\s*(?:\(|\s)\s*['\"]([^'\"]+)['\"]", text))
    elif ext == ".py":
        raw_imports.extend(match.group(1) for match in re.finditer(r"^\s*from\s+([.\w]+)\s+import\s+", text, flags=re.MULTILINE))

    for value in raw_imports:
        target = _resolve_relative_import(source_path, value, file_paths)
        if target:
            edges.append({"source_path": source_path, "target_path": target, "kind": "relative"})
    return edges


def _resolve_relative_import(source_path: str, value: str, file_paths: set[str]) -> str:
    if not value.startswith("."):
        return ""
    base = Path(source_path).parent
    candidate = (base / value).as_posix()
    candidates = [
        candidate,
        f"{candidate}.js",
        f"{candidate}.ts",
        f"{candidate}.php",
        f"{candidate}.py",
        f"{candidate}/index.js",
        f"{candidate}/index.ts",
        f"{candidate}/index.php",
    ]
    for item in candidates:
        normalized = str(Path(item))
        if normalized in file_paths:
            return normalized
    return ""


def _extract_functions(text: str, file_row: dict[str, Any], repo_full_name: str) -> list[dict[str, Any]]:
    ext = file_row["extension"]
    patterns = []
    if ext in {".php", ".blade.php"}:
        patterns = [
            re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
            re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        ]
    elif ext in {".js", ".jsx", ".ts", ".tsx"}:
        patterns = [
            re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\("),
            re.compile(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"),
            re.compile(r"\b([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"),
        ]
    elif ext == ".py":
        patterns = [re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)]
    else:
        return []

    rows = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            if name in {"if", "for", "while", "switch", "catch", "function"}:
                continue
            end_line = min(len(lines), idx + 120)
            rows.append(
                {
                    "name": name,
                    "qualified_name": f"{repo_full_name}:{file_row['path']}::{name}@{idx}",
                    "file_path": file_row["path"],
                    "language": file_row["language"],
                    "start_line": idx,
                    "end_line": end_line,
                }
            )
            break
    return rows[:500]


def _extract_call_names(text: str) -> set[str]:
    names = set()
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text):
        name = match.group(1)
        if name not in {"if", "for", "while", "switch", "catch", "return", "function"}:
            names.add(name)
    return names


def _line_window(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[max(0, start_line - 1): max(start_line, end_line)])


def _embedding_text(repo: dict[str, Any], file_row: dict[str, Any], text: str, *, max_chars: int = 2000) -> str:
    trimmed = text[:max(500, max_chars)]
    return "\n".join(
        [
            f"repo: {repo['full_name']}",
            f"path: {file_row['path']}",
            f"language: {file_row['language']}",
            "content:",
            trimmed,
        ]
    )


def _dedupe_edges(edges: list[dict[str, str]], keys: tuple[str, ...]) -> list[dict[str, str]]:
    seen = set()
    out = []
    for edge in edges:
        marker = tuple(edge.get(key, "") for key in keys)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(edge)
    return out
