"""RCA (AI Root Cause Analysis) router."""

import logging
import re
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response

from app.auth import CurrentUser, require_tab
from app.config import settings
from app.embedding_status import record_embedding_update
from app.graph_job import job_store
from app.rca import localize as rca_localize, rca_document, runner as rca_runner
from app.rca.store import RCARunStore
from app.repository_discovery import active_repository_names

log = logging.getLogger(__name__)

router = APIRouter(tags=["RCA"])

rca_run_store = RCARunStore(settings)


def _run_rca_pipeline(run_id: str) -> None:
    """Background worker: execute the read-only RCA pipeline for a run."""
    run = rca_run_store.get(run_id)
    if run is None:
        log.warning("RCA run %s vanished before execution", run_id)
        return
    rca_runner.run_pipeline(settings, rca_run_store, run)


@router.post("/rca/{jira_key}")
def rca_start(
    jira_key: str,
    background_tasks: BackgroundTasks,
    repo: str | None = None,
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    """Kick off a root-cause analysis run for a Jira key. Returns run_id."""
    key = jira_key.strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9]+-\d+$", key):
        raise HTTPException(status_code=400, detail=f"Invalid Jira key: {jira_key!r}")
    try:
        rca_run_store.init_schema()
        run = rca_run_store.create(
            key,
            user_id=getattr(_user, "id", None),
            user_email=getattr(_user, "email", None),
        )
        if repo and repo.strip():
            run.localized_repos = [
                {"repo": repo.strip(), "score": 10.0, "reasons": [f"Explicitly selected: '{repo.strip()}'"]}
            ]
            rca_run_store._persist(run)
        background_tasks.add_task(_run_rca_pipeline, run.run_id)
        log.info("Enqueued RCA run %s for %s (repo=%s) by %s", run.run_id, key, repo, getattr(_user, "email", "unknown"))
        return {"run_id": run.run_id, "jira_key": key, "status": run.status}
    except Exception as exc:
        log.exception("Failed to start RCA for %s: %s", key, exc)
        raise HTTPException(status_code=500, detail=f"Failed to start RCA: {str(exc)}")


@router.get("/rca/runs")
def rca_list_runs(
    limit: int = 25,
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    is_admin = _user.is_service or _user.role == "admin"
    return {
        "runs": rca_run_store.list_recent(
            limit=limit,
            user_id=getattr(_user, "id", None),
            is_admin=is_admin,
        )
    }


@router.get("/rca/runs/{run_id}")
def rca_get_run(
    run_id: str,
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    run = rca_run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"RCA run '{run_id}' not found")
    data = run.to_dict()
    if run.document:
        data["markdown"] = rca_document.render_markdown(run.document)
    return data


@router.get("/rca/runs/{run_id}/document.docx")
def rca_download_docx(
    run_id: str,
    _user: CurrentUser = Depends(require_tab("rca")),
) -> Response:
    run = rca_run_store.get(run_id)
    if run is None or not run.document:
        raise HTTPException(status_code=404, detail="No completed RCA document for this run")
    data = rca_document.render_docx(run.document)
    filename = f"RCA-{run.jira_key}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _rca_excluded_repos(request_exclude: Any = None) -> set[str]:
    """Repos to skip for an RCA index build: the env default union the request's."""
    names = {
        r.strip() for r in settings.rca_index_excluded_repos.split(",") if r.strip()
    }
    for r in request_exclude or []:
        if str(r).strip():
            names.add(str(r).strip())
    return names


def _run_code_index_build(job_id: str, repo_names: list[str], force_full: bool) -> None:
    """Background worker: build the code_chunks semantic index."""
    from app.rca import code_index
    job = job_store.get(job_id)
    if job is None:
        return
    job.status = "running"
    repos_to_index = repo_names or None
    # Per-repo wall-clock so we can extrapolate a within-repo ETA (the only
    # honest ETA on a first full build — total chunk count is unknown up front).
    repo_clock: dict[str, Any] = {"repo": None, "start": None}
    try:
        def _progress(done: int, total: int, repo: str, result: dict[str, Any]) -> None:
            job.totals["repositories"] = total
            job.progress["repositories_done"] = done
            if repo:  # skip the initial "0/N" tick (empty repo name)
                job.logs.append(
                    {"repo": repo, **{k: v for k, v in result.items() if k != "repo"}}
                )

        def _chunk_progress(
            repo_index: int, repo_total: int, repo: str, cdone: int, ctotal: int
        ) -> None:
            if repo != repo_clock["repo"]:
                repo_clock["repo"] = repo
                repo_clock["start"] = time.monotonic()
            job.totals["repositories"] = repo_total
            job.progress["repositories_done"] = repo_index - 1  # repos fully done
            job.progress["current_repo_index"] = repo_index
            job.progress["current_repo_chunks_done"] = cdone
            job.progress["current_repo_chunks_total"] = ctotal
            job.progress["current_repo_elapsed"] = int(
                time.monotonic() - (repo_clock["start"] or time.monotonic())
            )
            job.meta["current_repo"] = repo

        summary = code_index.build_index(
            settings, repos_to_index, force_full=force_full,
            progress=_progress, chunk_progress=_chunk_progress)
        job.logs.append({"summary": summary})
        record_embedding_update(settings, settings.rca_code_chunks_collection)
        job.mark_done()
    except Exception as exc:  # noqa: BLE001
        log.exception("code_chunks build job %s failed", job_id)
        job.mark_failed(str(exc))


@router.post("/rca/code-index/build")
def rca_build_code_index(
    payload: dict[str, Any] | None = None,
    background_tasks: BackgroundTasks = None,
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    """Build/update the code_chunks semantic index (Phase A). Returns a job_id.

    Scope (in order of precedence):
      • explicit ``repositories`` list  → index exactly those
      • ``scope: "all"``                → index every git repo under the root
      • default (``scope: "active"``)   → only repos with commit activity
    """
    payload = payload or {}
    repo_names = [str(r) for r in (payload.get("repositories") or [])]
    scope = str(payload.get("scope") or "active").lower()
    force_full = bool(payload.get("force_full", False))

    # Repos to skip: env default (RCA_INDEX_EXCLUDED_REPOS) ∪ this request's list.
    excluded = _rca_excluded_repos(payload.get("exclude"))

    if repo_names:
        scope = "explicit"
    elif scope == "all":
        from app.rca import repos as rca_repos
        repo_names = rca_repos.list_repos(settings)
    else:
        # Default: restrict to active repos so stale/archived clones aren't
        # re-chunked and embedded on every build.
        scope = "active"
        repo_names = active_repository_names(settings)

    skipped = [r for r in repo_names if r in excluded]
    repo_names = [r for r in repo_names if r not in excluded]

    job = job_store.create(action="rca_code_index_build")
    background_tasks.add_task(_run_code_index_build, job.job_id, repo_names, force_full)
    return {"job_id": job.job_id, "status": job.status, "scope": scope,
            "repository_count": len(repo_names),
            "excluded": sorted(excluded), "skipped": skipped,
            "repositories": repo_names}


@router.get("/rca/code-index/status")
def rca_code_index_status(
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    """Whether the code_chunks collection exists + per-repo indexed SHAs."""
    from app.rca import code_index
    try:
        client = code_index._qdrant_client(settings)
        exists = code_index.collection_exists(client, settings.rca_code_chunks_collection)
        count = 0
        if exists:
            count = client.count(settings.rca_code_chunks_collection).count
    except Exception as exc:  # noqa: BLE001
        return {"collection": settings.rca_code_chunks_collection, "exists": False,
                "excluded_default": sorted(_rca_excluded_repos()),
                "error": str(exc)}
    return {"collection": settings.rca_code_chunks_collection, "exists": exists,
            "excluded_default": sorted(_rca_excluded_repos()),
            "points": count}


@router.get("/rca/repo-map")
def rca_repo_map_list(
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    return {"mappings": rca_localize.list_mappings(settings)}


@router.post("/rca/repo-map/seed")
def rca_repo_map_seed(
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    return rca_localize.seed_map(settings)


@router.post("/rca/repo-map")
def rca_repo_map_upsert(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("rca")),
) -> dict[str, Any]:
    component = str(payload.get("component", "")).strip()
    repo = str(payload.get("repo", "")).strip()
    if not component or not repo:
        raise HTTPException(status_code=400, detail="component and repo are required")
    rca_localize.upsert_mapping(settings, component, repo,
                                float(payload.get("weight", 1.0)), source="manual")
    return {"status": "ok", "component": component, "repo": repo}
