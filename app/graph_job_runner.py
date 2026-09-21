"""Background job orchestrator for Qdrant Vector DB build operations.

Each job runs as an asyncio background task. Progress is written back to the
GraphJob in `job_store` *and* persisted to the `graph_jobs` table so history
survives restarts. GitHub repositories and git pull results are also logged.

Jobs only build and upsert embeddings into the Qdrant Vector DB for Jira
tickets and codebase files.

Job actions:
  update            – git pull (optional) + embed repos + Jira fetch/embed
  regenerate        – same as update, forces Jira re-fetch (ignores cache)
  create_new        – same as regenerate (re-embeds everything)
  jira_tickets_only – skip repos; only fetch/embed Jira tickets
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import psycopg
from app.config import settings
from app.embedding_status import record_embedding_update
from app.codebase_graph import (
    build_codebase_embedding_documents,
    embed_codebase_documents,
)
from app.graph_job import GraphJob
from app.jira_fetcher import fetch_all_tickets
from app.jira_graph import _adf_to_text
from app.flag_embedder import BGEM3Embedder
from app.ollama_embedder import OllamaEmbedder
from app.qdrant_store import (
    JIRA_COLLECTION,
    JIRA_HYBRID_COLLECTION,
    upsert_jira_embeddings,
    upsert_jira_hybrid_embeddings,
)
from app.repository_discovery import discover_graph_repositories

log = logging.getLogger(__name__)


# ─── PostgreSQL helpers ──────────────────────────────────────────────────────

def _db_connect() -> Optional[psycopg.Connection]:
    if not settings.database_url:
        return None
    try:
        return psycopg.connect(settings.database_url)
    except Exception as exc:
        log.warning("DB connection failed; job will not be persisted: %s", exc)
        return None


def _persist_job_start(job: GraphJob) -> None:
    conn = _db_connect()
    if conn is None:
        return
    with conn:
        conn.execute(
            """
            INSERT INTO graph_jobs (job_id, action, status, started_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (job_id) DO NOTHING
            """,
            (UUID(job.job_id), job.action, job.status),
        )


def _persist_job_progress(job: GraphJob) -> None:
    conn = _db_connect()
    if conn is None:
        return
    with conn:
        conn.execute(
            """
            UPDATE graph_jobs
            SET status      = %s,
                repos_total = %s,
                repos_done  = %s,
                jira_total  = %s,
                jira_done   = %s,
                totals      = %s::jsonb,
                progress    = %s::jsonb,
                logs        = %s::jsonb,
                error_msg   = %s,
                completed_at = %s,
                updated_at   = NOW()
            WHERE job_id = %s
            """,
            (
                job.status,
                job.totals.get("repositories", 0),
                job.progress.get("repositories_done", 0),
                job.totals.get("jira_tickets", 0),
                job.progress.get("jira_tickets_done", 0),
                json.dumps(job.totals, ensure_ascii=False, default=str),
                json.dumps(job.progress, ensure_ascii=False, default=str),
                json.dumps(job.logs, ensure_ascii=False, default=str),
                job.error,
                job.completed_at,
                UUID(job.job_id),
            ),
        )


def _append_job_log(job: GraphJob, step: str, level: str, message: str) -> None:
    log_method = getattr(log, level, log.info)
    log_method("Graph job %s [%s] %s", job.job_id, step, message)
    job.logs.append(
        {
            "step": step,
            "level": level,
            "message": message,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _sync_embedding_total(job: GraphJob) -> None:
    job.totals["embedding_documents"] = (
        job.totals.get("jira_embedding_documents", 0)
        + job.totals.get("codebase_embedding_documents", 0)
    )


def _sync_embedding_progress(job: GraphJob) -> None:
    job.progress["embedding_documents_done"] = (
        job.progress.get("jira_embedding_documents_done", 0)
        + job.progress.get("codebase_embedding_documents_done", 0)
    )
    job.progress["embedding_eta_seconds"] = (
        job.progress.get("jira_embedding_eta_seconds", 0)
        or job.progress.get("codebase_embedding_eta_seconds", 0)
    )


def _eta_seconds(done: int, total: int, started_at: float) -> int:
    if done <= 0 or total <= 0 or done >= total:
        return 0
    elapsed = max(0.001, time.monotonic() - started_at)
    # Early parallel batches often complete in bursts and produce wildly
    # optimistic estimates. Wait for a small but meaningful sample.
    if elapsed < 120 and done < max(100, int(total * 0.05)):
        return 0
    rate = done / elapsed
    if rate <= 0:
        return 0
    return int(round((total - done) / rate))


def _format_eta(done: int, total: int, started_at: float) -> str:
    remaining_seconds = _eta_seconds(done, total, started_at)
    if remaining_seconds <= 0:
        return ""
    if remaining_seconds < 60:
        return f" ETA {remaining_seconds}s"
    minutes, seconds = divmod(remaining_seconds, 60)
    if minutes < 60:
        return f" ETA {minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f" ETA {hours}h {minutes}m"


def _upsert_repo_row(conn: psycopg.Connection, repo: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO github_repositories
            (name, full_name, container_path, host_path, remote_url, branch, current_commit, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (container_path) DO UPDATE
            SET name           = EXCLUDED.name,
                full_name      = EXCLUDED.full_name,
                host_path      = EXCLUDED.host_path,
                remote_url     = EXCLUDED.remote_url,
                branch         = EXCLUDED.branch,
                current_commit = EXCLUDED.current_commit,
                last_seen      = NOW()
        """,
        (
            repo.get("name", ""),
            repo.get("name", ""),        # full_name same as name for local repos
            repo.get("container_path") or repo.get("path", ""),
            repo.get("path", ""),
            repo.get("remote_url", ""),
            repo.get("branch", ""),
            repo.get("current_commit", ""),
        ),
    )


def _log_pull(
    conn: psycopg.Connection,
    job_id: str,
    repo_name: str,
    container_path: str,
    success: bool,
    output: str,
) -> None:
    conn.execute(
        """
        INSERT INTO github_pull_log (job_id, repo_name, container_path, success, output)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (UUID(job_id), repo_name, container_path, success, output[:2000]),
    )


# ─── Git pull helper ─────────────────────────────────────────────────────────

def _git_pull(repo: dict[str, Any]) -> tuple[bool, str]:
    """Run git pull --ff-only. Returns (success, output)."""
    container_path = repo.get("container_path") or repo.get("path", "")
    if not container_path:
        return False, "no container_path"
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=container_path,
            capture_output=True,
            text=True,
            timeout=120,
            env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out = (result.stdout + result.stderr).strip()
        return result.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


# ─── Main job runner ──────────────────────────────────────────────────────────

async def run_graph_job(
    job: GraphJob,
    pull_latest_code: bool = True,
    fetch_latest_jira: bool = True,
    include_jira_in_graph: bool = True,
    build_embeddings: bool = True,
    embedding_model: str = "codebase_bge_m3",
    selected_repositories: list[str] | None = None,
) -> None:
    """Execute a Qdrant build job in the background; updates `job` and persists to DB."""
    job.status = "running"
    job.meta["embedding_model"] = embedding_model
    _persist_job_start(job)
    log.info("Qdrant job %s started (action=%s)", job.job_id, job.action)

    codebase_documents: list[dict[str, Any]] = []

    try:
        action = job.action
        jira_only = action == "jira_tickets_only"
        force_jira_refresh = action in ("create_new", "regenerate", "jira_tickets_only")

        # ── Step 1: GitHub repos ──────────────────────────────────────────
        if not jira_only:
            repos = discover_graph_repositories(settings)
            if selected_repositories:
                selected = set(selected_repositories)
                repos = [
                    repo for repo in repos
                    if repo.get("name") in selected or repo.get("path") in selected
                ]
            job.totals["repositories"] = len(repos)
            _persist_job_progress(job)

            # Persist repo metadata
            conn = _db_connect()
            if conn:
                try:
                    for repo in repos:
                        _upsert_repo_row(conn, repo)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

            # Git pull + logging
            if pull_latest_code:
                log.info("Running git pull on %d repos ...", len(repos))

                for repo in repos:
                    success, output = await asyncio.to_thread(_git_pull, repo)

                    conn = _db_connect()
                    if conn:
                        try:
                            _log_pull(
                                conn,
                                job.job_id,
                                repo.get("name", ""),
                                repo.get("container_path") or repo.get("path", ""),
                                success,
                                output,
                            )
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                        finally:
                            conn.close()

            repo_concurrency = max(1, settings.graph_job_repo_concurrency)
            repo_semaphore = asyncio.Semaphore(repo_concurrency)

            _append_job_log(
                job,
                "repositories",
                "info",
                f"Scanning {len(repos)} repositories for Qdrant embeddings with concurrency {repo_concurrency}.",
            )
            _persist_job_progress(job)

            async def _process_repo(repo: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
                async with repo_semaphore:
                    documents: list[dict[str, Any]] = []
                    if build_embeddings:
                        documents = await asyncio.to_thread(
                            build_codebase_embedding_documents,
                            repo,
                            max_files=settings.codebase_embed_max_files_per_repo,
                            max_chars=settings.codebase_embed_max_chars,
                        )
                    return repo, documents

            completed_repos = 0
            repo_index_started_at = time.monotonic()
            repo_tasks = [asyncio.create_task(_process_repo(repo)) for repo in repos]
            for repo_task in asyncio.as_completed(repo_tasks):
                repo, documents = await repo_task
                codebase_documents.extend(documents)
                completed_repos += 1
                job.progress["repositories_done"] = completed_repos
                job.progress["repositories_eta_seconds"] = _eta_seconds(
                    completed_repos,
                    len(repos),
                    repo_index_started_at,
                )
                _append_job_log(
                    job,
                    "repositories",
                    "info",
                    (
                        f"{repo.get('name', 'repository')}: prepared "
                        f"{len(documents)} embedding document(s). "
                        f"Progress {completed_repos}/{len(repos)}."
                        f"{_format_eta(completed_repos, len(repos), repo_index_started_at)}"
                    ),
                )
                _persist_job_progress(job)

            job.progress["repositories_done"] = len(repos)
            if build_embeddings:
                job.totals["codebase_embedding_documents"] = len(codebase_documents)
                job.progress["codebase_embedding_documents_done"] = 0
                _sync_embedding_total(job)
                _sync_embedding_progress(job)
            _append_job_log(
                job,
                "repositories",
                "info",
                f"Prepared {len(codebase_documents)} codebase embedding document(s) across {len(repos)} repositories.",
            )
            _persist_job_progress(job)

        # ── Step 2: Jira tickets ──────────────────────────────────────────
        if jira_only or (fetch_latest_jira and include_jira_in_graph):
            log.info(
                "Fetching Jira tickets (force_refresh=%s) ...",
                force_jira_refresh,
            )

            tickets = await asyncio.to_thread(
                fetch_all_tickets,
                force_jira_refresh,
            )

            job.totals["jira_tickets"] = len(tickets)
            if build_embeddings and settings.qdrant_url:
                job.totals["jira_embedding_documents"] = len(tickets)
                job.progress["jira_embedding_documents_done"] = 0
                _sync_embedding_total(job)
                _sync_embedding_progress(job)
            _persist_job_progress(job)

            log.info("Got %d Jira tickets", len(tickets))

            if tickets and include_jira_in_graph:
                job.progress["jira_tickets_done"] = len(tickets)
                _persist_job_progress(job)

                # ── Step 3: Jira embeddings ──────────────────────────────
                if build_embeddings and settings.qdrant_url:
                    _sync_embedding_total(job)
                    _sync_embedding_progress(job)
                    _persist_job_progress(job)

                    embedder = OllamaEmbedder(
                        settings.ollama_url,
                        settings.ollama_embed_model,
                        timeout_seconds=settings.ollama_embed_timeout_seconds,
                        batch_size=settings.ollama_embed_batch_size,
                        concurrency=settings.ollama_embed_concurrency,
                    )

                    if embedder.is_available():
                        _append_job_log(
                            job,
                            "embeddings",
                            "info",
                            (
                                f"Starting {len(tickets)} Jira ticket embeddings "
                                f"with {settings.ollama_embed_model} in batches of {settings.ollama_embed_batch_size} "
                                f"using {settings.ollama_embed_concurrency} parallel request(s)."
                            ),
                        )

                        texts = _ticket_embed_texts(tickets)

                        last_jira_logged_bucket = {"value": -1}
                        jira_embedding_started_at = time.monotonic()

                        def _jira_embedding_progress(done: int, total: int) -> None:
                            job.progress["jira_embedding_documents_done"] = done
                            job.progress["jira_embedding_eta_seconds"] = _eta_seconds(
                                done,
                                total,
                                jira_embedding_started_at,
                            )
                            _sync_embedding_progress(job)
                            bucket = done // max(settings.ollama_embed_batch_size, 1)
                            should_log = done == 0 or done == total or bucket != last_jira_logged_bucket["value"]
                            if should_log:
                                last_jira_logged_bucket["value"] = bucket
                                _append_job_log(
                                    job,
                                    "embeddings",
                                    "info",
                                    (
                                        f"{'Generating' if done == 0 else 'Generated'} "
                                        f"{done}/{total} Jira ticket embeddings."
                                        f"{_format_eta(done, total, jira_embedding_started_at)}"
                                    ),
                                )
                            _persist_job_progress(job)

                        embeddings = await asyncio.to_thread(
                            embedder.embed_batch,
                            texts,
                            None,
                            _jira_embedding_progress,
                        )

                        stored = upsert_jira_embeddings(
                            qdrant_url=settings.qdrant_url,
                            tickets=tickets,
                            embeddings=embeddings,
                            api_key=settings.qdrant_api_key or None,
                        )

                        log.info("Stored %d Jira dense embeddings in Qdrant", stored)
                        if stored:
                            record_embedding_update(settings, JIRA_COLLECTION, stored)

                        # ── Hybrid ingest (BGE-M3 dense + sparse via FlagEmbedding) ──
                        flag_embedder = BGEM3Embedder()
                        if flag_embedder.is_available():
                            try:
                                _append_job_log(job, "embeddings", "info",
                                    "Running BGE-M3 hybrid (dense+sparse) encoding for Qdrant RRF search…")
                                hybrid_encoded = await asyncio.to_thread(
                                    flag_embedder.encode_batch, texts, 8
                                )
                                hybrid_stored = upsert_jira_hybrid_embeddings(
                                    qdrant_url=settings.qdrant_url,
                                    tickets=tickets,
                                    encoded=hybrid_encoded,
                                    api_key=settings.qdrant_api_key or None,
                                )
                                _append_job_log(job, "embeddings", "info",
                                    f"Stored {hybrid_stored} hybrid Jira embeddings (jira_tickets_hybrid).")
                                if hybrid_stored:
                                    record_embedding_update(
                                        settings, JIRA_HYBRID_COLLECTION, hybrid_stored)
                            except Exception as _he:
                                log.warning("Hybrid Jira ingest failed (non-fatal): %s", _he)
                                _append_job_log(job, "embeddings", "warning",
                                    f"Hybrid Jira embedding skipped: {_he}")
                        else:
                            _append_job_log(job, "embeddings", "info",
                                "FlagEmbedding not installed — skipping hybrid ingest. "
                                "Run: pip install FlagEmbedding")

                        job.progress["jira_embedding_documents_done"] = stored
                        job.progress["jira_embedding_eta_seconds"] = 0
                        _sync_embedding_progress(job)
                        _persist_job_progress(job)
                    else:
                        log.info(
                            "Ollama BGE-M3 not available; skipping Jira embeddings"
                        )
                        _append_job_log(
                            job,
                            "embeddings",
                            "warning",
                            "Ollama embedding model unavailable; skipped Jira embeddings.",
                        )
                        _persist_job_progress(job)

        # ── Step 4: Codebase embeddings ──────────────────────────────────
        if build_embeddings and settings.qdrant_url and codebase_documents:
            job.totals["codebase_embedding_documents"] = len(codebase_documents)
            job.progress.setdefault("codebase_embedding_documents_done", 0)
            _sync_embedding_total(job)
            _sync_embedding_progress(job)
            _append_job_log(
                job,
                "embeddings",
                "info",
                (
                    f"Starting {len(codebase_documents)} codebase embeddings "
                    f"with {embedding_model} in batches of {settings.ollama_embed_batch_size} "
                    f"using {settings.ollama_embed_concurrency} parallel request(s) "
                    f"and {settings.codebase_embed_max_chars} chars per file."
                ),
            )
            _persist_job_progress(job)

            last_logged_bucket = {"value": -1}
            codebase_embedding_started_at = time.monotonic()

            def _embedding_progress(done: int, total: int) -> None:
                job.progress["codebase_embedding_documents_done"] = done
                job.progress["codebase_embedding_eta_seconds"] = _eta_seconds(
                    done,
                    total,
                    codebase_embedding_started_at,
                )
                _sync_embedding_progress(job)
                bucket = done // max(settings.ollama_embed_batch_size, 1)
                should_log = done == 0 or done == total or bucket != last_logged_bucket["value"]
                if should_log:
                    last_logged_bucket["value"] = bucket
                    _append_job_log(
                        job,
                        "embeddings",
                        "info",
                        (
                            f"{'Generating' if done == 0 else 'Generated'} "
                            f"{done}/{total} codebase embeddings for {embedding_model}."
                            f"{_format_eta(done, total, codebase_embedding_started_at)}"
                        ),
                    )
                _persist_job_progress(job)

            stored = await asyncio.to_thread(
                embed_codebase_documents,
                qdrant_url=settings.qdrant_url,
                qdrant_api_key=settings.qdrant_api_key,
                ollama_url=settings.ollama_url,
                ollama_timeout_seconds=settings.ollama_embed_timeout_seconds,
                ollama_batch_size=settings.ollama_embed_batch_size,
                ollama_concurrency=settings.ollama_embed_concurrency,
                embedding_model_key=embedding_model,
                documents=codebase_documents,
                progress_callback=_embedding_progress,
            )
            job.progress["codebase_embedding_documents_done"] = stored
            job.progress["codebase_embedding_eta_seconds"] = 0
            _sync_embedding_progress(job)
            _append_job_log(
                job,
                "embeddings",
                "info",
                f"Stored {stored}/{len(codebase_documents)} codebase embeddings in {embedding_model}.",
            )
            if stored:
                record_embedding_update(settings, embedding_model, stored)
            _persist_job_progress(job)

        job.mark_done()
        _persist_job_progress(job)

        log.info("Qdrant job %s completed", job.job_id)

    except Exception as exc:
        log.exception("Qdrant job %s failed: %s", job.job_id, exc)

        job.mark_failed(str(exc))
        _persist_job_progress(job)


def _ticket_embed_texts(tickets: list[dict]) -> list[str]:
    result = []
    for t in tickets:
        fields = t.get("fields", {}) or {}
        summary = (fields.get("summary") or "")[:200]
        desc = _adf_to_text(fields.get("description"))[:500]
        result.append(f"{summary}\n{desc}".strip())
    return result
