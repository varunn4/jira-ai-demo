"""Ring Studio and Zoho Desk integrations router."""

import json
import logging
from typing import Any, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
)

from app import ring_studio
from app.auth import CurrentUser, require_tab
from app.config import settings
from app.zoho_client import (
    CustomerTicketsRequest,
    CustomerTicketsResponse,
    TicketDetailResponse,
    ZohoClient,
    ZohoError,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["Plugins & Integrations"])

zoho_client = ZohoClient(settings)


# ─── Ring Studio (diamond-ring image-prompt generator) ───────────────────────
# Capability key "rings" (role: designer / admin). Assembles CELESTE-style
# luxury jewelry spec-sheet prompts and optionally renders them to images.

@router.get("/ring-studio/banks")
def ring_studio_banks(
    _user: CurrentUser = Depends(require_tab("rings")),
) -> dict[str, Any]:
    """All value banks + paired options the design form renders as dropdowns."""
    return ring_studio.list_banks()


@router.post("/ring-studio/prompt", response_model=ring_studio.PromptResponse)
def ring_studio_prompt(
    request: ring_studio.PromptRequest,
    _user: CurrentUser = Depends(require_tab("rings")),
) -> ring_studio.PromptResponse:
    """Assemble a single master prompt. seed = reproducible; overrides pin fields."""
    prompt, meta = ring_studio.build_prompt(seed=request.seed, overrides=request.overrides)
    return ring_studio.PromptResponse(prompt=prompt, meta=meta, seed=request.seed)


@router.post("/ring-studio/batch")
def ring_studio_batch(
    request: ring_studio.BatchRequest,
    _user: CurrentUser = Depends(require_tab("rings")),
) -> dict[str, Any]:
    """Generate N reproducible prompts (seed = start_seed + index)."""
    rows = ring_studio.generate_batch(request.count, request.start_seed)
    return {"count": len(rows), "start_seed": request.start_seed, "rows": rows}


@router.post("/ring-studio/batch.jsonl")
def ring_studio_batch_download(
    request: ring_studio.BatchRequest,
    _user: CurrentUser = Depends(require_tab("rings")),
) -> Response:
    """Download the batch as a ring_prompts.jsonl file (one design per line)."""
    rows = ring_studio.generate_batch(request.count, request.start_seed)
    body = ring_studio.batch_to_jsonl(rows)
    return Response(
        content=body,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ring_prompts.jsonl"'},
    )


@router.post("/ring-studio/image", response_model=ring_studio.RingJobRef)
def ring_studio_image(
    request: ring_studio.ImageRequest,
    background_tasks: BackgroundTasks,
    _user: CurrentUser = Depends(require_tab("rings")),
) -> ring_studio.RingJobRef:
    """Enqueue a render of the FIVE per-view images (hero, top, side, front,
    detail) for one ring design, all depicting the exact same ring.

    Rendering takes ~40–60s (five OpenAI image calls), which would trip
    reverse-proxy timeouts if done inline, so this returns a ``job_id``
    immediately; poll ``GET /ring-studio/image/{job_id}`` for the result.

    Pass the ``meta`` of an already-generated design to keep that exact ring, or
    ``seed``/``overrides`` to assemble a fresh one first. The render falls back
    to returning the five view prompts when OPENAI_API_KEY is not configured.
    """
    if request.meta:
        meta = request.meta
    else:
        _, meta = ring_studio.build_prompt(seed=request.seed, overrides=request.overrides)

    job = ring_studio.ring_job_store.create()
    background_tasks.add_task(
        ring_studio.run_render_job, job.job_id, settings, meta, request.seed,
        quality=request.quality,
    )
    return ring_studio.RingJobRef(job_id=job.job_id, status=job.status)


@router.get("/ring-studio/image/{job_id}", response_model=ring_studio.RingJobStatus)
def ring_studio_image_status(
    job_id: str,
    _user: CurrentUser = Depends(require_tab("rings")),
) -> ring_studio.RingJobStatus:
    """Poll a queued render. While ``status`` is pending/running the result is
    null; on completion it carries the RingViewsResult, or ``error`` on failure."""
    job = ring_studio.ring_job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Ring render job '{job_id}' not found")
    return ring_studio.RingJobStatus(
        job_id=job.job_id, status=job.status, result=job.result, error=job.error,
    )


@router.get("/ring-studio/image/{job_id}/zip")
def ring_studio_image_zip(
    job_id: str,
    _user: CurrentUser = Depends(require_tab("rings")),
) -> Response:
    """Download all rendered views of a completed job as a single ZIP."""
    job = ring_studio.ring_job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Ring render job '{job_id}' not found")
    if job.status != "completed" or job.result is None:
        raise HTTPException(status_code=409, detail="Render is not complete yet.")
    bundle = ring_studio.build_views_zip(job.result)
    if bundle is None:
        raise HTTPException(status_code=404, detail="No rendered images to download.")
    filename, data = bundle
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/ring-studio/upload-render", response_model=ring_studio.RingJobRef)
async def ring_studio_upload_render(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    quality: str = Form(ring_studio._DEFAULT_QUALITY),
    meta_json: Optional[str] = Form(None),
    ring_size: Optional[str] = Form(None),
    metal_weight: Optional[str] = Form(None),
    gross_weight: Optional[str] = Form(None),
    _user: CurrentUser = Depends(require_tab("rings")),
) -> ring_studio.RingJobRef:
    """Enqueue a render seeded from UPLOADED photos of one ring (up to 4 angles).

    The uploads are fed to the model together as one multi-angle reference and
    the ring is re-designed from the raw prompt alone — no design fields are
    injected — with the other three views chaining off the hero. Multipart:
    ``files`` (1–4 images), ``quality`` (low/medium/high), and optional
    ``meta_json`` (a design meta from /ring-studio/prompt; unused by the
    reference prompts, kept for the render's naming/metadata). Returns a
    ``job_id``; poll ``GET /ring-studio/image/{job_id}`` for the result."""
    images = [f for f in files if f.filename]
    if not images:
        raise HTTPException(status_code=400, detail="Upload at least one image.")
    if len(images) > 4:
        raise HTTPException(status_code=400, detail="Upload at most 4 images.")

    base_images: list[bytes] = []
    for f in images:
        data = await f.read()
        if not data:
            continue
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"'{f.filename}' exceeds 15 MB.")
        if not (f.content_type or "").startswith("image/"):
            raise HTTPException(status_code=400, detail=f"'{f.filename}' is not an image.")
        base_images.append(data)
    if not base_images:
        raise HTTPException(status_code=400, detail="Uploaded files were empty.")

    if meta_json:
        try:
            meta = json.loads(meta_json)
            if not isinstance(meta, dict):
                raise ValueError("meta must be an object")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid meta_json: {exc}")
    else:
        _, meta = ring_studio.build_prompt()

    details = ring_studio.RingDetails(
        ring_size=ring_size, metal_weight=metal_weight, gross_weight=gross_weight,
    )

    job = ring_studio.ring_job_store.create()
    background_tasks.add_task(
        ring_studio.run_render_job, job.job_id, settings, meta, None,
        quality=quality, base_images=base_images, details=details,
    )
    return ring_studio.RingJobRef(job_id=job.job_id, status=job.status)


@router.get("/ring-studio/gallery")
def ring_studio_gallery(
    limit: int = 100,
    _user: CurrentUser = Depends(require_tab("rings")),
) -> dict[str, Any]:
    """Every generated ring with its Product Summary and images, newest first.

    Reads the persisted gallery, so it survives restarts and the capped in-memory
    job store. Returns an empty list with a message when the DB is unavailable —
    the Gallery is a nice-to-have and must never 500 the tab."""
    try:
        entries = ring_studio.list_generations(settings, limit=limit)
        return {"count": len(entries), "entries": [e.model_dump() for e in entries]}
    except Exception as exc:  # noqa: BLE001 - degrade, don't break the tab
        log.warning("Ring gallery unavailable: %s", exc)
        return {"count": 0, "entries": [], "message": f"Gallery unavailable: {exc}"}


# ── Zoho Desk — customer ticket visibility (capability key "zoho") ───────────
# Look a customer up by email/phone against Zoho Desk and list their tickets.
# A single shared client caches the OAuth access token across requests.

@router.get("/zoho/status")
def zoho_status(
    _user: CurrentUser = Depends(require_tab("zoho")),
) -> dict[str, Any]:
    """Whether Zoho Desk credentials are configured (drives the tab's banner)."""
    return {"configured": zoho_client.is_configured()}


@router.post("/zoho/tickets", response_model=CustomerTicketsResponse)
def zoho_customer_tickets(
    request: CustomerTicketsRequest,
    _user: CurrentUser = Depends(require_tab("zoho")),
) -> CustomerTicketsResponse:
    """Resolve a customer (email/phone) to their Zoho contact and list tickets."""
    email = (request.email or "").strip() or None
    phone = (request.phone or "").strip() or None
    if not (email or phone):
        raise HTTPException(status_code=400, detail="Provide a customer email or phone number.")
    try:
        return zoho_client.customer_tickets(email=email, phone=phone)
    except ZohoError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/zoho/tickets/{ticket_id}", response_model=TicketDetailResponse)
def zoho_ticket_detail(
    ticket_id: str,
    _user: CurrentUser = Depends(require_tab("zoho")),
) -> TicketDetailResponse:
    """One ticket's detail and conversation threads (the row-click detail view)."""
    try:
        return zoho_client.ticket_detail(ticket_id)
    except ZohoError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
