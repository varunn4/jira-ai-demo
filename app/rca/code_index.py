"""Phase A — code-chunk vector index over Qdrant `code_chunks`.

Embeds function/class chunks (BGE-M3 dense via Ollama, matching how
`codebase_bge_m3` was built) and stores them with a locating payload:
  repo, file_path, symbol, start_line, end_line, language, commit_sha, chunk_type

Indexing is incremental: per repo we remember the indexed commit SHA in
Postgres (`rca_code_index_state`). A re-index only re-chunks files that changed
between the stored SHA and HEAD (via `git diff`), deleting stale points for
changed/removed files. This module reads the codebase and writes only to Qdrant
and its own state table — it never modifies any repo.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from app.config import Settings
from app.ollama_embedder import OllamaEmbedder
from app.rca import repos
from app.rca.code_chunker import CodeChunk, iter_repo_chunks, language_for

log = logging.getLogger(__name__)

_VECTOR_SIZE = 1024  # BGE-M3 dense
_UPSERT_BATCH = 256


def _stable_id(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def _qdrant_client(settings: Settings):
    from qdrant_client import QdrantClient
    candidates = [settings.qdrant_url, "http://jira-ai-qdrant:6333", "http://host.docker.internal:6333", "http://localhost:6333"]
    for url in candidates:
        if not url:
            continue
        try:
            client = QdrantClient(url=url, api_key=settings.qdrant_api_key or None, timeout=2.0)
            client.get_collections()
            return client
        except Exception:
            continue
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


def collection_exists(client, name: str) -> bool:
    try:
        return name in {c.name for c in client.get_collections().collections}
    except Exception:
        return False


def _ensure_collection(client, name: str) -> None:
    from qdrant_client.models import Distance, VectorParams
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            name,
            vectors_config=VectorParams(size=_VECTOR_SIZE, distance=Distance.COSINE),
        )
        log.info("Created Qdrant collection '%s' (dim=%d)", name, _VECTOR_SIZE)
    # Payload index on `repo` keeps per-repo filters/deletes fast.
    try:
        from qdrant_client.models import PayloadSchemaType
        client.create_payload_index(name, "repo", PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "file_path", PayloadSchemaType.KEYWORD)
    except Exception:
        pass  # already exists or unsupported — non-fatal


def _embedder(settings: Settings) -> OllamaEmbedder:
    return OllamaEmbedder(
        base_url=settings.ollama_url,
        model=settings.ollama_embed_model,
        timeout_seconds=settings.ollama_embed_timeout_seconds,
        batch_size=settings.ollama_embed_batch_size,
        concurrency=settings.ollama_embed_concurrency,
    )


# ── Postgres state (indexed SHA per repo) ─────────────────────────────────────

def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def init_schema(settings: Settings) -> None:
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rca_code_index_state (
                repo         TEXT PRIMARY KEY,
                commit_sha   TEXT NOT NULL,
                chunk_count  INTEGER NOT NULL DEFAULT 0,
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def get_indexed_sha(settings: Settings, repo: str) -> Optional[str]:
    if not settings.database_url:
        return None
    with _connect(settings) as conn:
        row = conn.execute(
            "SELECT commit_sha FROM rca_code_index_state WHERE repo = %s", (repo,)
        ).fetchone()
        return row["commit_sha"] if row else None


def _set_indexed_sha(settings: Settings, repo: str, sha: str, chunk_count: int) -> None:
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute(
            """
            INSERT INTO rca_code_index_state (repo, commit_sha, chunk_count, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (repo) DO UPDATE
              SET commit_sha = EXCLUDED.commit_sha,
                  chunk_count = EXCLUDED.chunk_count,
                  updated_at = NOW()
            """,
            (repo, sha, chunk_count),
        )


def _clear_indexed_sha(settings: Settings, repo: str) -> None:
    """Forget a repo's indexed SHA so the next build re-chunks it in full.

    Called when a build left embedding gaps (e.g. Ollama timeouts): dropping the
    state row means a subsequent normal (non-force) build treats the repo as
    never-indexed and retries every chunk, so the index self-heals instead of
    permanently skipping the repo at a partially-indexed HEAD."""
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute("DELETE FROM rca_code_index_state WHERE repo = %s", (repo,))


# ── Qdrant write helpers ──────────────────────────────────────────────────────

def _delete_file_points(client, collection: str, repo: str, file_paths: list[str]) -> None:
    if not file_paths:
        return
    from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny, FilterSelector
    flt = Filter(must=[
        FieldCondition(key="repo", match=MatchValue(value=repo)),
        FieldCondition(key="file_path", match=MatchAny(any=file_paths)),
    ])
    client.delete(collection_name=collection, points_selector=FilterSelector(filter=flt))


def _upsert_chunks(
    settings: Settings,
    client,
    collection: str,
    chunks: list[CodeChunk],
    sha: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[int, int]:
    """Embed + upsert chunks. Returns (written, failed) where `failed` counts
    chunks whose embedding could not be produced (so the caller can decide not to
    mark the repo indexed and retry later).

    `progress`, if given, is called as progress(chunks_processed, chunks_total)
    after each batch so a job runner can show live within-repo progress + ETA
    (the total is known up front, before the slow embedding loop starts)."""
    from qdrant_client.models import PointStruct

    embedder = _embedder(settings)
    if not embedder.is_available():
        raise RuntimeError(
            f"Ollama embedder unavailable at {settings.ollama_url} "
            f"(model {settings.ollama_embed_model}); cannot index code chunks"
        )

    total = len(chunks)
    if progress:
        progress(0, total)
    written = 0
    failed = 0
    for i in range(0, len(chunks), _UPSERT_BATCH):
        batch = chunks[i : i + _UPSERT_BATCH]
        # Report progress per embedding sub-batch (every few chunks) rather than
        # once per 256-chunk upsert — otherwise the counter looks frozen at 0 for
        # minutes while a whole batch embeds on CPU.
        emb_cb = None
        if progress:
            def emb_cb(done: int, _batch_total: int, _base: int = i) -> None:
                progress(min(_base + done, total), total)
        vectors = embedder.embed_batch(
            [c.embed_text() for c in batch], progress_callback=emb_cb
        )
        points = []
        for chunk, vec in zip(batch, vectors):
            if not vec:
                failed += 1
                continue
            points.append(PointStruct(
                id=_stable_id(chunk.point_seed()),
                vector=vec,
                payload={
                    "repo": chunk.repo,
                    "file_path": chunk.file_path,
                    "symbol": chunk.symbol,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "language": chunk.language,
                    "commit_sha": sha,
                    "chunk_type": chunk.chunk_type,
                    "text": chunk.text[:1500],
                },
            ))
        if points:
            client.upsert(collection_name=collection, points=points)
            written += len(points)
            log.info("code_chunks[%s]: upserted %d/%d", chunks[0].repo, written, len(chunks))
        if progress:
            progress(min(i + len(batch), total), total)
    if failed:
        log.warning("code_chunks[%s]: %d/%d chunk(s) failed to embed",
                    chunks[0].repo, failed, len(chunks))
    return written, failed


# ── public API ────────────────────────────────────────────────────────────────

@dataclass
class IndexResult:
    repo: str
    head_sha: str
    previous_sha: Optional[str]
    files_changed: int
    chunks_written: int
    files_deleted: int
    skipped: bool
    chunks_failed: int = 0


def _changed_files(settings: Settings, repo: str, old_sha: str) -> tuple[list[str], list[str]]:
    """Return (changed_or_added, deleted) source files since old_sha (vs HEAD)."""
    out = repos._git(settings, repo, "diff", "--name-status", f"{old_sha}..HEAD")
    changed, deleted = [], []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        if language_for(path) is None:
            continue
        if status.startswith("D"):
            deleted.append(path)
        else:
            changed.append(path)
    return changed, deleted


def index_repo(
    settings: Settings,
    repo: str,
    *,
    force_full: bool = False,
    chunk_progress: Optional[Callable[[int, int], None]] = None,
) -> IndexResult:
    """Index (or delta-sync) one repo's code chunks into Qdrant.

    Incremental by default: only re-chunks files changed since the stored SHA.
    Pass force_full=True to re-chunk every tracked source file.
    `chunk_progress(done, total)` reports live embedding progress for this repo.
    """
    init_schema(settings)
    collection = settings.rca_code_chunks_collection
    head = repos.head_sha(settings, repo)
    if not head:
        raise RuntimeError(f"Cannot resolve HEAD for repo {repo!r}")

    prev = None if force_full else get_indexed_sha(settings, repo)
    client = _qdrant_client(settings)
    _ensure_collection(client, collection)

    if prev == head and not force_full:
        log.info("code_chunks[%s]: already at %s — skip", repo, head[:10])
        return IndexResult(repo, head, prev, 0, 0, 0, skipped=True)

    if prev and not force_full:
        changed, deleted = _changed_files(settings, repo, prev)
        target_files = changed
        # Drop stale points for changed + removed files before re-adding.
        _delete_file_points(client, collection, repo, changed + deleted)
        files_deleted = len(deleted)
    else:
        target_files = None  # all tracked source files
        deleted = []
        files_deleted = 0

    chunks = list(iter_repo_chunks(settings, repo, target_files))
    if chunks:
        written, failed = _upsert_chunks(
            settings, client, collection, chunks, head, progress=chunk_progress
        )
    else:
        written, failed = 0, 0
        if chunk_progress:
            chunk_progress(0, 0)

    # Only advance the indexed SHA when every chunk embedded cleanly. If any
    # chunk failed (e.g. a transient Ollama timeout), forget the repo's state so
    # the next build retries it in full rather than skipping it at a
    # partially-indexed HEAD.
    if failed:
        _clear_indexed_sha(settings, repo)
        log.warning("code_chunks[%s]: %d chunk(s) failed to embed — leaving repo "
                    "un-indexed so the next build retries it", repo, failed)
    else:
        _set_indexed_sha(settings, repo, head, written)

    return IndexResult(
        repo=repo,
        head_sha=head,
        previous_sha=prev,
        files_changed=len(target_files) if target_files is not None else -1,
        chunks_written=written,
        files_deleted=files_deleted,
        skipped=False,
        chunks_failed=failed,
    )


def build_index(
    settings: Settings,
    repo_names: Optional[list[str]] = None,
    *,
    force_full: bool = False,
    progress: Optional[Any] = None,
    chunk_progress: Optional[Callable[[int, int, str, int, int], None]] = None,
) -> dict[str, Any]:
    """Build/update the code_chunks index across repos (incremental by default).

    `progress`, if given, is called as progress(done, total, repo, result) after
    each repo. `chunk_progress(repo_index, repo_total, repo, chunks_done,
    chunks_total)` fires continuously *during* a repo so a job runner can surface
    a live within-repo bar + ETA (crucial on the first full build, where a single
    repo can hold thousands of chunks). Returns a summary.
    """
    from app.rca import repos as repo_access
    repos_to_index = repo_names or repo_access.list_repos(settings)
    total = len(repos_to_index)
    # Emit the repo total up front so the UI shows "0/N repos" immediately
    # instead of waiting for the first (possibly large) repo to finish.
    if progress:
        try:
            progress(0, total, "", {})
        except Exception:
            pass
    results: list[dict[str, Any]] = []
    chunks_total = 0
    failed_total = 0
    for i, repo in enumerate(repos_to_index, 1):
        repo_chunk_cb = None
        if chunk_progress:
            def repo_chunk_cb(done: int, ctotal: int, _i: int = i, _repo: str = repo) -> None:
                try:
                    chunk_progress(_i, total, _repo, done, ctotal)
                except Exception:
                    pass
        try:
            res = index_repo(
                settings, repo, force_full=force_full, chunk_progress=repo_chunk_cb
            )
            chunks_total += res.chunks_written
            failed_total += res.chunks_failed
            entry = {"repo": repo, "chunks_written": res.chunks_written,
                     "skipped": res.skipped, "head": res.head_sha[:10]}
            if res.chunks_failed:
                entry["chunks_failed"] = res.chunks_failed
        except Exception as exc:
            log.warning("code_chunks build failed for %s: %s", repo, exc)
            entry = {"repo": repo, "error": str(exc)}
        results.append(entry)
        if progress:
            try:
                progress(i, total, repo, entry)
            except Exception:
                pass
    log.info("code_chunks build complete: %d repos, %d chunks, %d failed",
             total, chunks_total, failed_total)
    return {"repos": total, "chunks_written": chunks_total,
            "chunks_failed": failed_total, "results": results}


def search(
    settings: Settings,
    query: str,
    repo: Optional[str] = None,
    k: int = 8,
) -> list[dict[str, Any]]:
    """Semantic search over code_chunks. Optionally scope to one repo.

    Returns [] cleanly (not an error) when the collection has not been built yet,
    when Ollama is unavailable, or on any query failure — semantic search is one
    of five retrievers and the pipeline degrades gracefully without it.
    """
    client = _qdrant_client(settings)
    collection = settings.rca_code_chunks_collection
    if not collection_exists(client, collection):
        log.info("code_chunks collection '%s' not built yet — semantic search skipped "
                 "(build it via POST /rca/code-index/build)", collection)
        return []

    # An existing-but-empty collection (e.g. every embed timed out during the
    # build) has nothing to match — skip the query embed so RCA doesn't stall on
    # a slow/hung Ollama for zero possible results.
    try:
        if client.count(collection_name=collection, exact=False).count == 0:
            log.info("code_chunks collection '%s' is empty — semantic search skipped", collection)
            return []
    except Exception as exc:
        log.debug("code_chunks count check failed (continuing): %s", exc)

    embedder = _embedder(settings)
    if not embedder.is_available():
        log.warning("Ollama embedder unavailable — semantic_code_search returns []")
        return []
    vec = embedder.embed(query)
    if not vec:
        return []

    flt = None
    if repo:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        flt = Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo))])

    try:
        hits = client.query_points(
            collection_name=collection, query=vec, limit=k,
            query_filter=flt, with_payload=True,
        ).points
    except Exception as exc:
        log.warning("code_chunks search failed: %s", exc)
        return []

    results = []
    for h in hits:
        p = h.payload or {}
        results.append({
            "repo": p.get("repo", ""),
            "file_path": p.get("file_path", ""),
            "symbol": p.get("symbol", ""),
            "start_line": p.get("start_line"),
            "end_line": p.get("end_line"),
            "chunk_type": p.get("chunk_type", ""),
            "language": p.get("language", ""),
            "score": round(float(h.score), 4),
            "text": p.get("text", ""),
        })
    return results
