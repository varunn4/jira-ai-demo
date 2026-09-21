"""FastAPI app exposing the three core endpoints.

Endpoints:
  POST /scan/initial         — kick off a full scan of all repos (async)
  POST /scan/nightly         — kick off a delta sync (async)
  POST /repomix/reindex      — refresh packed Repomix XML files (async)
  POST /testcases/generate   — generate test cases from a JIRA ticket (sync)
  GET  /jobs/{job_id}        — poll an async job
  GET  /jobs                 — list recent jobs
  GET  /health               — liveness

Run with:
    uvicorn repo_architect.api.app:app --host 0.0.0.0 --port 8000

OpenAPI docs at /docs (Swagger) and /redoc.
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from ..combiner import combine
from ..config import Config, load_config
from ..llm import LLMClient
from ..patcher import sync_all
from ..reindex import reindex_repomix
from ..scanner import scan_all
from ..testcases import JiraTicket, generate_test_cases
from .jobs import JobHandle, JobStore
from .models import (
    GenerateTestCasesRequest,
    GenerateTestCasesResponse,
    InitialScanRequest,
    JobDetail,
    JobResponse,
    NightlySyncRequest,
    RepomixReindexRequest,
)

log = logging.getLogger(__name__)


_TESTCASE_HEADING_RE = re.compile(r"^###\s*(TC[-\s]?\d+):?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_FIELD_RE_TEMPLATE = r"-\s*{field}:\s*(.*?)(?=\n-\s*[A-Za-z][A-Za-z /_-]*:|\n###|\Z)"


def _extract_testcase_field(body: str, field: str) -> str:
    match = re.search(
        _FIELD_RE_TEMPLATE.format(field=re.escape(field)),
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _parse_plain_testcases(markdown: str) -> list[dict[str, Any]]:
    """Convert the plain markdown format into workflow-friendly testcase items."""
    matches = list(_TESTCASE_HEADING_RE.finditer(markdown or ""))
    if not matches:
        return []

    parsed: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        steps_block = _extract_testcase_field(body, "Steps")
        steps = [
            line.strip()
            for line in re.sub(r"^\s*\d+\.\s*", "", steps_block, flags=re.MULTILINE).splitlines()
            if line.strip()
        ]
        expected = _extract_testcase_field(body, "Expected result")
        parsed.append({
            "title": match.group(2).strip(),
            "steps": steps,
            "expected": expected,
            "raw": body,
        })
    return parsed


# ============================================================
# App state — loaded once at startup
# ============================================================

class AppState:
    """Holds singletons: config, job store, LLM client.

    Attached to the FastAPI app's lifespan so they're created once and shared
    across requests. Avoids reloading the YAML or rebuilding clients on every
    call.
    """

    def __init__(self) -> None:
        self.config: Config | None = None
        self.job_store: JobStore | None = None
        self.llm: LLMClient | None = None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cfg_path = Path("config/repos.yaml")
    state.config = load_config(cfg_path)
    state.job_store = JobStore(state.config.workspace_dir / "jobs.json")
    state.llm = LLMClient(model=state.config.model)
    log.info(
        "API started. %d repos configured. model=%s",
        len(state.config.repos), state.config.model,
    )
    yield
    # Shutdown — let the executor finish in-flight jobs gracefully
    if state.job_store is not None:
        state.job_store._executor.shutdown(wait=False)


app = FastAPI(
    title="repo-architect",
    version="0.1.0",
    description="Architectural maps + test case generation from JIRA tickets.",
    lifespan=lifespan,
)


# ============================================================
# Health
# ============================================================

@app.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "repos_configured": len(state.config.repos) if state.config else 0,
        "model": state.config.model if state.config else None,
    }


# ============================================================
# Job polling
# ============================================================

@app.get("/jobs/{job_id}", response_model=JobDetail, tags=["jobs"])
def get_job(job_id: str) -> JobDetail:
    job = state.job_store.get(job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    return JobDetail(**job.__dict__)


@app.get("/jobs", tags=["jobs"])
def list_jobs(limit: int = 50) -> list[JobDetail]:
    jobs = state.job_store.list(limit=limit)
    return [JobDetail(**j.__dict__) for j in jobs]


def _job_response(job, kind: str) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        kind=kind,
        status=job.status,
        created_at=job.created_at,
        poll_url=f"/jobs/{job.id}",
    )


# ============================================================
# 1) Initial scan — async
# ============================================================

@app.post(
    "/scan/initial",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["scans"],
    summary="Trigger a full fresh scan of all configured repos.",
)
def initial_scan(req: InitialScanRequest = InitialScanRequest()) -> JobResponse:
    cfg = state.config

    if not req.force:
        # Refuse if state.json already has scans — prevents accidental clobbering.
        existing = [
            r.name for r in cfg.repos
            if state.job_store and (cfg.state_file.exists())
        ]
        if cfg.state_file.exists():
            import json as _json
            try:
                data = _json.loads(cfg.state_file.read_text())
                already = [name for name, s in data.items() if s.get("last_sha")]
                if already:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        detail={
                            "message": "Some repos already scanned. Use force=true to re-scan all.",
                            "already_scanned": already,
                        },
                    )
            except _json.JSONDecodeError:
                pass

    def run(h: JobHandle) -> Dict[str, Any]:
        h.progress(f"scanning {len(cfg.repos)} repos")
        results = scan_all(cfg)
        h.progress("combining master files")
        combine(cfg)
        return {
            "total_repos": len(cfg.repos),
            "succeeded": [r.repo for r in results],
            "failed": [r.name for r in cfg.repos if r.name not in {x.repo for x in results}],
            "output_dir": str(cfg.output_dir),
        }

    job = state.job_store.submit("initial_scan", run)
    return _job_response(job, "initial_scan")


# ============================================================
# 2) Nightly sync — async
# ============================================================

@app.post(
    "/scan/nightly",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["scans"],
    summary="Trigger a delta sync: pull repos, patch only changed sections.",
)
def nightly_sync(req: NightlySyncRequest = NightlySyncRequest()) -> JobResponse:
    cfg = state.config

    def run(h: JobHandle) -> Dict[str, Any]:
        h.progress(f"syncing {len(cfg.repos)} repos (git_pull={not req.no_git_pull})")
        summary = sync_all(cfg, do_git_pull=not req.no_git_pull)
        return {
            "full_scanned": summary.scanned_full,
            "patched": summary.patched,
            "skipped": summary.skipped,
            "failed": [{"repo": n, "error": e} for n, e in summary.failed],
        }

    job = state.job_store.submit("nightly_sync", run)
    return _job_response(job, "nightly_sync")


# ============================================================
# 3) Repomix reindex — async
# ============================================================

@app.post(
    "/repomix/reindex",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["repomix"],
    summary="Refresh Repomix packed XML files used by testcase generation.",
)
def repomix_reindex(req: RepomixReindexRequest = RepomixReindexRequest()) -> JobResponse:
    cfg = state.config

    def run(h: JobHandle) -> Dict[str, Any]:
        h.progress(f"reindexing Repomix data (git_pull={not req.no_git_pull})")
        return reindex_repomix(
            cfg,
            repo_names=req.repos,
            repo_url=req.repo_url,
            do_git_pull=not req.no_git_pull,
            force=req.force,
            progress=h.progress,
        )

    job = state.job_store.submit("repomix_reindex", run)
    return _job_response(job, "repomix_reindex")


# ============================================================
# 4) Test case generation — sync
# ============================================================

@app.post(
    "/testcases/generate",
    response_model=GenerateTestCasesResponse,
    tags=["testcases"],
    summary="Generate test cases from a JIRA ticket using Repomix maps and Qdrant semantic hits.",
)
def generate_testcases(req: GenerateTestCasesRequest) -> GenerateTestCasesResponse:
    ticket = JiraTicket(
        key=req.ticket.key,
        summary=req.ticket.summary,
        issue_type=req.ticket.issue_type,
        description=req.ticket.description,
        acceptance_criteria=req.ticket.acceptance_criteria,
        labels=req.ticket.labels,
        components=req.ticket.components,
    )

    try:
        result = generate_test_cases(
            ticket=ticket,
            cfg=state.config,
            llm=state.llm,
            style=req.style,
            repos_override=req.repos,
            embedding_model=req.embedding_model,
            top_k=req.top_k,
            include_semantic_context=req.include_semantic_context,
            audience=req.audience,
            pr_context=req.pr_context,
        )
    except ValueError as e:
        # Auto-detection failed or explicit repos didn't match — 400, not 500
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    except FileNotFoundError:
        # Let the app-level handler emit the friendly "run /scan/initial" message.
        raise
    except Exception as e:
        # Surface the real upstream failure (e.g. an Anthropic 401/403/model-access
        # error from the LLM call) instead of a bare 500, so callers/n8n can see it.
        log.exception("/testcases/generate failed")
        upstream_status = getattr(e, "status_code", None)
        detail = getattr(e, "message", None) or str(e) or repr(e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{type(e).__name__}"
            + (f" (upstream {upstream_status})" if upstream_status else "")
            + f": {detail}",
        ) from e

    return GenerateTestCasesResponse(
        ticket_key=result.ticket_key,
        grounded_repos=result.grounded_repos,
        style=result.style,
        testcases=_parse_plain_testcases(result.test_cases),
        test_cases=result.test_cases,
        semantic_hits_count=result.semantic_hits_count,
        files_touched_count=result.files_touched_count,
        architecture_context_chars=result.architecture_context_chars,
        repomix_context_chars=result.repomix_context_chars,
    )


# ============================================================
# Friendlier error handler for missing arch maps
# ============================================================

@app.exception_handler(FileNotFoundError)
async def file_not_found(_request, exc: FileNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"missing file: {exc}. Run /scan/initial first."},
    )
