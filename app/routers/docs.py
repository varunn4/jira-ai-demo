"""Repository Documentation Generation and Export router."""

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from app.auth import CurrentUser, require_tab
from app.config import settings
from app.repo_doc_generator import (
    list_doc_repositories,
    list_document_types,
)
from app.schemas import (
    RepoDocEmailRequest,
    RepoDocExportRequest,
    RepoDocRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["Repository Docs"])

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_for_doc(markdown: str, base: str) -> bytes:
    from app.markdown_docx import markdown_to_docx_bytes

    return markdown_to_docx_bytes(markdown, title=base)


def _base_name(filename: str) -> str:
    return filename[:-3] if filename.lower().endswith(".md") else filename


def _build_doc_attachments(job: dict[str, Any]) -> list[tuple[str, bytes, str]]:
    """Return email/download attachments for a finished doc job.

    One .docx for a single document; a .zip of .docx files for an 'all' job.
    """
    docs = job.get("docs") or []
    if not docs:
        raise HTTPException(status_code=400, detail="Job has no generated documents")

    if len(docs) == 1:
        d = docs[0]
        base = _base_name(d["filename"])
        return [(f"{base}.docx", _docx_for_doc(d["markdown"], base), _DOCX_MIME)]

    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in docs:
            base = _base_name(d["filename"])
            try:
                zf.writestr(f"{base}.docx", _docx_for_doc(d["markdown"], base))
            except Exception:  # noqa: BLE001 - fall back to markdown for this entry
                log.exception("DOCX build failed for %s; zipping markdown instead", base)
                zf.writestr(f"{base}.md", d["markdown"])
    return [(f"{job.get('repo', 'repo')}-docs.zip", buf.getvalue(), "application/zip")]


def _doc_email_bodies(job: dict[str, Any], requester: str) -> tuple[str, str]:
    """Build the plain-text and HTML email body with full generation details:
    repo, per-document token counts, and cost."""
    repo = job.get("repo", "repository")
    docs = job.get("docs") or []
    usage = job.get("usage") or {}
    generated_at = job.get("updated_at") or job.get("created_at") or ""
    model = next((d.get("model") for d in docs if d.get("model")), "") or "—"

    def toks(n: Any) -> str:
        return f"{int(n or 0):,}"

    # Costs are stored in USD; display them converted to INR.
    rate = settings.usd_to_inr

    def inr(n: Any) -> str:
        return f"₹{float(n or 0) * rate:,.2f}"

    total_in = usage.get("input_tokens", 0)
    total_out = usage.get("output_tokens", 0)
    total_cost = usage.get("cost_usd", 0)
    generated = usage.get("generated_count", 0)
    reused = usage.get("reused_count", 0)

    # ── Plain text ──
    lines = [
        f"Repository documentation for '{repo}'",
        "",
        f"Repository:    {repo}",
        f"Requested by:  {requester}",
        f"Generated at:  {generated_at}",
        f"Model:         {model}",
        "",
        f"Documents ({len(docs)}):",
    ]
    for d in docs:
        state = "reused (no code change)" if d.get("reused") else "generated"
        lines.append(
            f"  - {d.get('label', d.get('doc_type'))}: {state} · "
            f"{toks(d.get('input_tokens'))} in / {toks(d.get('output_tokens'))} out · "
            f"{inr(d.get('cost_usd'))}"
        )
    lines += [
        "",
        f"Totals: {toks(total_in)} input + {toks(total_out)} output tokens · {inr(total_cost)}",
        f"        {generated} generated, {reused} reused",
        "",
        f"Attached: {', '.join(d['filename'].replace('.md', '.docx') for d in docs) if len(docs) == 1 else repo + '-docs.zip (' + str(len(docs)) + ' Word files)'}",
    ]
    text = "\n".join(lines)

    # ── HTML ──
    cell = "padding:8px 12px;border-bottom:1px solid #e6eaf2"
    cell_r = cell + ";text-align:right"
    row_parts = []
    for d in docs:
        status_html = (
            "<span style='color:#059669;font-weight:600'>reused</span>"
            if d.get("reused")
            else "generated"
        )
        label = d.get("label", d.get("doc_type"))
        row_parts.append(
            f"<tr>"
            f"<td style='{cell}'>{label}</td>"
            f"<td style='{cell}'>{status_html}</td>"
            f"<td style='{cell_r}'>{toks(d.get('input_tokens'))}</td>"
            f"<td style='{cell_r}'>{toks(d.get('output_tokens'))}</td>"
            f"<td style='{cell_r}'>{inr(d.get('cost_usd'))}</td>"
            f"</tr>"
        )
    rows = "".join(row_parts)
    html = f"""\
<div style="font-family:Inter,Segoe UI,Arial,sans-serif;color:#0f172a;max-width:640px">
  <h2 style="margin:0 0 4px;font-size:20px">Repository documentation</h2>
  <p style="margin:0 0 16px;color:#64748b">Generated for <b>{repo}</b></p>
  <table style="border-collapse:collapse;font-size:13px;margin-bottom:16px">
    <tr><td style="padding:2px 12px 2px 0;color:#64748b">Repository</td><td><b>{repo}</b></td></tr>
    <tr><td style="padding:2px 12px 2px 0;color:#64748b">Requested by</td><td>{requester}</td></tr>
    <tr><td style="padding:2px 12px 2px 0;color:#64748b">Generated at</td><td>{generated_at}</td></tr>
    <tr><td style="padding:2px 12px 2px 0;color:#64748b">Model</td><td>{model}</td></tr>
  </table>
  <table style="border-collapse:collapse;width:100%;font-size:13px;border:1px solid #e6eaf2;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#eef2f9;text-align:left">
        <th style="padding:9px 12px">Document</th>
        <th style="padding:9px 12px">Status</th>
        <th style="padding:9px 12px;text-align:right">Input tok</th>
        <th style="padding:9px 12px;text-align:right">Output tok</th>
        <th style="padding:9px 12px;text-align:right">Cost</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
    <tfoot>
      <tr style="background:#f7f9fd;font-weight:700">
        <td style="padding:9px 12px" colspan="2">Total ({generated} generated, {reused} reused)</td>
        <td style="padding:9px 12px;text-align:right">{toks(total_in)}</td>
        <td style="padding:9px 12px;text-align:right">{toks(total_out)}</td>
        <td style="padding:9px 12px;text-align:right">{inr(total_cost)}</td>
      </tr>
    </tfoot>
  </table>
  <p style="margin:16px 0 0;color:#64748b;font-size:12px">
    The document{'s are' if len(docs) != 1 else ' is'} attached as
    {'a ZIP of Word files' if len(docs) != 1 else 'a Word file'}.
  </p>
</div>"""
    return text, html


@router.get("/graph-admin/repo-docs/repositories")
def graph_admin_repo_doc_repositories(
    _user: CurrentUser = Depends(require_tab("docs")),
) -> dict[str, Any]:
    log.info("GET /graph-admin/repo-docs/repositories")
    try:
        repositories = list_doc_repositories()
    except Exception as exc:
        log.exception("Failed to list documentation repositories")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "repository_count": len(repositories),
        "repositories": repositories,
        # Real generation types plus the synthetic "all" bundle option.
        "doc_types": list_document_types() + [{"id": "all", "label": "All documents (ZIP)"}],
        # USD→INR rate so the UI can display costs in rupees.
        "usd_to_inr": settings.usd_to_inr,
    }


@router.post("/graph-admin/repo-docs/generate")
def graph_admin_generate_repo_doc(
    request: RepoDocRequest,
    user: CurrentUser = Depends(require_tab("docs")),
) -> dict[str, Any]:
    """Start async document generation and return a job id to poll.

    Generation runs 30s-4min, so it is done on a background thread to avoid
    reverse-proxy read timeouts (e.g. nginx 504). Poll
    /graph-admin/repo-docs/jobs/{job_id} for the result. ``doc_type == "all"``
    generates every document type and bundles them as a ZIP.
    """
    log.info(
        "POST /graph-admin/repo-docs/generate repo=%s doc_type=%s by=%s",
        request.repo,
        request.doc_type,
        user.email,
    )
    from app.repo_doc_jobs import start_doc_job

    job_id = start_doc_job(request.repo, request.doc_type, user_email=user.email)
    return {"job_id": job_id, "status": "pending"}


@router.post("/graph-admin/repo-docs/estimate")
def graph_admin_estimate_repo_doc(
    request: RepoDocRequest,
    _user: CurrentUser = Depends(require_tab("docs")),
) -> dict[str, Any]:
    """Pre-flight cost + cache check used by the UI to confirm spend before
    starting a paid job. Documents already generated for the current code are
    reused at no cost, so when ``all_cached`` is true the UI can skip the cost
    prompt. Spends no tokens.
    """
    log.info(
        "POST /graph-admin/repo-docs/estimate repo=%s doc_type=%s",
        request.repo,
        request.doc_type,
    )
    from app.repo_doc_jobs import estimate_doc_job

    try:
        return estimate_doc_job(request.repo, request.doc_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/graph-admin/repo-docs/jobs/{job_id}")
def graph_admin_repo_doc_job(
    job_id: str,
    _user: CurrentUser = Depends(require_tab("docs")),
) -> dict[str, Any]:
    from app.repo_doc_jobs import get_doc_job

    job = get_doc_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown document job id")
    return job


@router.post("/graph-admin/repo-docs/export")
def graph_admin_export_repo_doc(
    request: RepoDocExportRequest,
    _user: CurrentUser = Depends(require_tab("docs")),
) -> Response:
    log.info("POST /graph-admin/repo-docs/export filename=%s", request.filename)
    if not request.markdown.strip():
        raise HTTPException(status_code=400, detail="No markdown content to export")
    try:
        from app.markdown_docx import markdown_to_docx_bytes

        data = markdown_to_docx_bytes(request.markdown, title=request.filename)
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="The 'python-docx' package is required for DOCX export",
        ) from exc
    except Exception as exc:
        log.exception("DOCX export failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    base = request.filename or "document"
    if base.lower().endswith(".md"):
        base = base[:-3]
    filename = f"{base}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/graph-admin/repo-docs/jobs/{job_id}/zip")
def graph_admin_repo_doc_zip(
    job_id: str,
    _user: CurrentUser = Depends(require_tab("docs")),
) -> Response:
    from app.repo_doc_jobs import get_doc_job

    job = get_doc_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown document job id")
    if job.get("status") != "done":
        raise HTTPException(status_code=409, detail="Job is not finished yet")
    attachments = _build_doc_attachments(job)
    filename, data, mime = attachments[0]
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/graph-admin/repo-docs/email")
def graph_admin_email_repo_doc(
    request: RepoDocEmailRequest,
    user: CurrentUser = Depends(require_tab("docs")),
) -> dict[str, Any]:
    from app.email_sender import EmailConfigError, email_configured, send_email
    from app.repo_doc_jobs import get_doc_job

    if not email_configured():
        raise HTTPException(
            status_code=503,
            detail="Email is not configured. Set SMTP_HOST and SMTP_FROM (and credentials).",
        )
    job = get_doc_job(request.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown document job id")
    if job.get("status") != "done":
        raise HTTPException(status_code=409, detail="Job is not finished yet")

    recipients = [a for a in re.split(r"[,\s]+", request.to_email or "") if a]
    if not recipients:
        raise HTTPException(status_code=400, detail="Provide at least one recipient email")

    attachments = _build_doc_attachments(job)
    repo = job.get("repo", "repository")
    subject = f"Repository documentation — {repo}"
    body, html_body = _doc_email_bodies(job, user.email)
    try:
        send_email(
            to=recipients, subject=subject, body=body, html_body=html_body, attachments=attachments
        )
    except EmailConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to email repo docs")
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}") from exc

    log.info("Emailed repo docs job=%s to=%s by=%s", request.job_id, recipients, user.email)
    return {"sent": True, "recipients": recipients, "attachments": [a[0] for a in attachments]}


@router.get("/graph-admin/repo-docs/usage")
def graph_admin_repo_doc_usage(
    limit: int = 50,
    user: CurrentUser = Depends(require_tab("docs", "users")),
) -> dict[str, Any]:
    """Token/cost usage analytics for document generation.

    Admins (and the user-management role) see every user; others see only their
    own consumption.
    """
    from app.auth import has_tab
    from app.repo_doc_usage import usage_summary

    can_see_all = user.is_service or user.role == "admin" or has_tab(user.role, "users")
    scope = None if can_see_all else user.email
    return {
        "scope": "all" if can_see_all else user.email,
        "usd_to_inr": settings.usd_to_inr,
        **usage_summary(user_email=scope, limit=limit),
    }
