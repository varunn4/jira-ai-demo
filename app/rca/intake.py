"""Phase C — Jira defect intake & extraction.

Fetches the full ticket (description, comments, attachments, component,
affected/fix versions, linked issues) and runs one LLM extraction call that
returns strict JSON:
  error_messages, stack_frames (file:line:symbol), mentioned_endpoints,
  repro_steps, suspected_area.

Read-only: it fetches Jira and calls the model; it touches no code.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth

from app.config import Settings
from app.jira_fetcher import _jira_get  # read-only GET with retry/backoff
from app.jira_graph import _adf_to_text
from app.json_utils import parse_model_json

log = logging.getLogger(__name__)

# Screenshots pasted into a Jira ticket land as image attachments. We download
# them and feed them to the extraction model (vision) so on-screen details that
# never appear in the prose — the URL/route, UI labels, column headers, an error
# shown in the UI — become searchable signals. Anthropic accepts png/jpeg/gif/webp.
_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_TICKET_IMAGES = 8          # cap how many screenshots we send per ticket
_MAX_IMAGE_BYTES = 4_000_000    # per-image budget; skip anything larger

# Tickets link a PRD / Tech-Doc (usually a Google Doc) describing the feature's
# intended behavior. We read them via the existing Google Drive integration
# (app.doc_review handles org-restricted OAuth / service-account auth) so the
# extraction model has the spec, not just the bug report.
_MAX_LINKED_DOCS = 4
_MAX_DOC_CHARS = 8000           # per-doc char budget fed to extraction

# Issue fields we need for diagnosis. Comments and attachments come back nested.
_FIELDS = (
    "summary,description,status,issuetype,priority,components,labels,"
    "fixVersions,versions,comment,attachment,issuelinks,environment,created,updated"
)


@dataclass
class TicketIntake:
    key: str
    summary: str
    description: str
    status: str
    issue_type: str
    priority: str
    components: list[str]
    labels: list[str]
    affected_versions: list[str]
    fix_versions: list[str]
    environment: str
    comments: list[str]
    attachments: list[dict[str, Any]]
    linked_issues: list[dict[str, str]]
    raw: dict[str, Any] = field(default_factory=dict)
    linked_docs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("raw", None)
        # Keep linked-doc metadata but drop the (potentially large) fetched text
        # from the stored dict — its signal is already distilled into `extracted`.
        d["linked_docs"] = [
            {"label": x.get("label"), "url": x.get("url"),
             "error": x.get("error"), "has_text": bool(x.get("text"))}
            for x in self.linked_docs
        ]
        return d


def fetch_ticket(settings: Settings, jira_key: str) -> TicketIntake:
    """Fetch one Jira issue with the fields RCA needs. Read-only."""
    if not settings.jira_base_url:
        raise RuntimeError("JIRA_BASE_URL not configured")
    data = _jira_get(f"/rest/api/3/issue/{jira_key}", params={"fields": _FIELDS})
    fields = data.get("fields", {}) or {}
    raw_description = fields.get("description")

    comments = [
        _adf_to_text(c.get("body"))
        for c in ((fields.get("comment") or {}).get("comments") or [])
    ]
    attachments = [
        {"filename": a.get("filename", ""), "mime": a.get("mimeType", ""),
         "size": a.get("size", 0), "url": a.get("content", "")}
        for a in (fields.get("attachment") or [])
    ]
    linked = []
    for link in (fields.get("issuelinks") or []):
        rel = (link.get("type") or {})
        other = link.get("inwardIssue") or link.get("outwardIssue") or {}
        if other:
            linked.append({
                "key": other.get("key", ""),
                "relation": rel.get("inward") if link.get("inwardIssue") else rel.get("outward", ""),
                "summary": (other.get("fields") or {}).get("summary", ""),
            })

    return TicketIntake(
        key=data.get("key", jira_key),
        summary=fields.get("summary") or "",
        description=_adf_to_text(raw_description),
        status=(fields.get("status") or {}).get("name", ""),
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        priority=(fields.get("priority") or {}).get("name", ""),
        components=[c.get("name", "") for c in (fields.get("components") or [])],
        labels=fields.get("labels") or [],
        affected_versions=[v.get("name", "") for v in (fields.get("versions") or [])],
        fix_versions=[v.get("name", "") for v in (fields.get("fixVersions") or [])],
        environment=_adf_to_text(fields.get("environment")) if fields.get("environment") else "",
        comments=[c for c in comments if c],
        attachments=attachments,
        linked_issues=linked,
        raw=data,
        linked_docs=_fetch_linked_docs(raw_description),
    )


def _fetch_linked_docs(raw_description: Any) -> list[dict[str, Any]]:
    """Fetch the text of PRD / Tech-Doc links in the ticket via the existing
    Google Drive integration (app.doc_review, which handles org-restricted OAuth /
    service-account auth). Best-effort: any extraction/auth/fetch failure is
    recorded per-doc and never breaks intake.
    """
    try:
        from app import doc_review
        links = doc_review.extract_doc_links(raw_description)
    except Exception as exc:  # import or parse failure — degrade to no docs
        log.warning("RCA intake: linked-doc extraction failed: %s", exc)
        return []
    docs: list[dict[str, Any]] = []
    for link in links[:_MAX_LINKED_DOCS]:
        try:
            text, err = doc_review.fetch_doc_text(link.url, limit_chars=_MAX_DOC_CHARS)
        except Exception as exc:  # network/auth — record and continue
            text, err = "", f"{type(exc).__name__}: {exc}"
        if err:
            log.info("RCA intake: linked doc %s not readable: %s", link.url, err)
        docs.append({"label": link.label, "url": link.url, "text": text, "error": err})
    return docs


_EXTRACTION_SYSTEM = """\
You analyze a Jira defect ticket and extract structured diagnostic signals for an
automated root-cause investigation.

You DO NOT propose fixes.

Return ONLY a JSON object. No prose. No markdown fences. Use exactly these keys:

{
  "error_messages": [string],
  "stack_frames": [
    {
      "file": string|null,
      "line": integer|null,
      "symbol": string|null,
      "raw": string
    }
  ],
  "mentioned_endpoints": [string],
  "repro_steps": [string],
  "expected_behavior": string,
  "actual_behavior": string,
  "environment": [string],
  "mentioned_code_refs": [string],
  "linked_ticket_keys": [string],
  "suspected_area": string
}

Rules:

- Copy error strings exactly as written. They may be used for literal search.

- Include a stack frame only when the ticket actually contains trace-like content.

- Use null for unknown file, line, or symbol fields. Never invent paths, symbols,
  or line numbers.

- repro_steps must preserve the reported order.

- expected_behavior contains only explicitly stated or unambiguously documented
  intended behaviour. Do not infer product intent.

- actual_behavior contains only explicitly reported or directly visible behaviour.

- environment contains explicitly mentioned environment information such as
  production, staging, browser, operating system, app version, build, service, or
  deployment environment.

- mentioned_code_refs contains only explicitly mentioned PRs, commits,
  repositories, files, classes, functions, symbols, procedures, routes, or config
  keys.

- linked_ticket_keys contains only explicitly referenced ticket identifiers.

- suspected_area is a short phrase identifying the feature, module, screen, service,
  or functional area implicated by the ticket. It is NOT a root-cause hypothesis.

- If a section has no evidence, return:
  - [] for arrays;
  - "" for strings.

- Screenshots from the ticket may be attached as images. READ them. Extract:
  - visible URL or route into mentioned_endpoints;
  - on-screen error text into error_messages verbatim;
  - visible page titles, menu labels, breadcrumbs, button names, column headers,
    and field labels to sharpen suspected_area;
  - explicitly visible environment or version information into environment.

  Ignore decorative or boilerplate UI chrome.

- A linked design document, PRD, or Tech Doc may be included. Use it only to:
  - understand explicitly documented intended behaviour;
  - populate expected_behavior;
  - sharpen suspected_area;
  - extract explicitly named endpoints, procedures, modules, or code references.

  The defect lives in the implementation or runtime system, not automatically in
  the document.

  Never treat document prose as:
  - error output;
  - runtime evidence;
  - a stack frame;
  - proof of root cause.

- Do not classify the issue.
- Do not identify a root cause.
- Do not propose a fix.
- Do not assign confidence.

Your job is extraction only.
"""


def _intake_user_message(intake: TicketIntake) -> str:
    parts = [
        f"KEY: {intake.key}",
        f"SUMMARY: {intake.summary}",
        f"TYPE: {intake.issue_type}  PRIORITY: {intake.priority}  STATUS: {intake.status}",
        f"COMPONENTS: {', '.join(intake.components) or '(none)'}",
        f"AFFECTED VERSIONS: {', '.join(intake.affected_versions) or '(none)'}",
        f"LABELS: {', '.join(intake.labels) or '(none)'}",
        "",
        "DESCRIPTION:",
        intake.description or "(empty)",
    ]
    if intake.environment:
        parts += ["", "ENVIRONMENT:", intake.environment]
    if intake.comments:
        parts += ["", "COMMENTS:"]
        for i, c in enumerate(intake.comments[:20], 1):
            parts.append(f"[{i}] {c}")
    if intake.attachments:
        names = ", ".join(a["filename"] for a in intake.attachments if a.get("filename"))
        if names:
            parts += ["", f"ATTACHMENTS: {names}"]
    if intake.linked_issues:
        parts += ["", "LINKED ISSUES:"]
        for li in intake.linked_issues:
            parts.append(f"- {li['relation']} {li['key']}: {li['summary']}")
    readable_docs = [d for d in intake.linked_docs if d.get("text")]
    if readable_docs:
        parts += ["", "LINKED DESIGN DOCS (PRD / Tech Doc):"]
        for d in readable_docs:
            parts.append(f"--- {d.get('label') or 'DOC'} ({d.get('url')}) ---")
            parts.append(d["text"])
    return "\n".join(parts)


_EMPTY_EXTRACTION = {
    "error_messages": [],
    "stack_frames": [],
    "mentioned_endpoints": [],
    "repro_steps": [],
    "expected_behavior": "",
    "actual_behavior": "",
    "environment": [],
    "mentioned_code_refs": [],
    "linked_ticket_keys": [],
    "suspected_area": "",
}


def _download_attachment(settings: Settings, url: str) -> bytes:
    """Fetch a Jira attachment's bytes with the same basic auth used for the API."""
    resp = requests.get(
        url, headers={"Accept": "*/*"},
        auth=HTTPBasicAuth(settings.jira_email, settings.jira_api_token), timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def _collect_ticket_images(settings: Settings, intake: TicketIntake) -> list[dict[str, Any]]:
    """Download image attachments as Anthropic image blocks (base64). Best-effort:
    any download/size failure is skipped so extraction never breaks on a bad file."""
    if not (settings.jira_email and settings.jira_api_token):
        return []  # no creds to authenticate the attachment download
    blocks: list[dict[str, Any]] = []
    for att in intake.attachments:
        if len(blocks) >= _MAX_TICKET_IMAGES:
            break
        mime = (att.get("mime") or "").lower()
        url = att.get("url") or ""
        if mime not in _IMAGE_MIME_TYPES or not url:
            continue
        if att.get("size") and att["size"] > _MAX_IMAGE_BYTES:
            log.info("RCA intake %s: skipping oversized image %s (%s bytes)",
                     intake.key, att.get("filename"), att.get("size"))
            continue
        try:
            data = _download_attachment(settings, url)
        except Exception as exc:  # network/auth/HTTP — degrade, don't crash intake
            log.warning("RCA intake %s: could not download attachment %s: %s",
                        intake.key, att.get("filename"), exc)
            continue
        if not data or len(data) > _MAX_IMAGE_BYTES:
            continue
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime,
                       "data": base64.standard_b64encode(data).decode("ascii")},
        })
    if blocks:
        log.info("RCA intake %s: attached %d screenshot(s) to extraction", intake.key, len(blocks))
    return blocks


def extract_signals(
    settings: Settings,
    intake: TicketIntake,
    llm_client: Any = None,
) -> dict[str, Any]:
    """Run the LLM extraction call; return strict JSON (schema-guaranteed keys).

    Pass `llm_client` (anything with a `.complete(system, user, max_tokens=)`)
    to inject a stub in tests; otherwise an Anthropic client is built. Image
    attachments (screenshots) are downloaded and sent with the call when present.
    """
    if llm_client is None:
        from app.llm_client import build_llm_client
        llm_client = build_llm_client(settings, timeout_override=settings.llm_timeout_seconds)
    complete_kwargs: dict[str, Any] = {"max_tokens": 2000}
    if intake.attachments:
        images = _collect_ticket_images(settings, intake)
        if images:
            complete_kwargs["images"] = images
    raw = llm_client.complete(
        _EXTRACTION_SYSTEM,
        _intake_user_message(intake),
        **complete_kwargs,
    )
    try:
        parsed = parse_model_json(raw)
    except Exception as exc:
        log.warning("RCA extraction JSON parse failed for %s: %s", intake.key, exc)
        return dict(_EMPTY_EXTRACTION)

    # Normalize to the guaranteed schema so downstream phases can trust it.
    result = dict(_EMPTY_EXTRACTION)
    for key in result:
        if key in parsed and parsed[key] is not None:
            result[key] = parsed[key]
    result["error_messages"] = [s for s in _as_str_list(result["error_messages"]) if s]
    result["mentioned_endpoints"] = [s for s in _as_str_list(result["mentioned_endpoints"]) if s]
    result["repro_steps"] = _as_str_list(result["repro_steps"])
    result["stack_frames"] = _normalize_frames(result["stack_frames"])
    result["environment"] = _as_str_list(result.get("environment"))
    result["mentioned_code_refs"] = _as_str_list(result.get("mentioned_code_refs"))
    result["linked_ticket_keys"] = _as_str_list(result.get("linked_ticket_keys"))
    result["expected_behavior"] = str(result.get("expected_behavior") or "")
    result["actual_behavior"] = str(result.get("actual_behavior") or "")
    result["suspected_area"] = str(result.get("suspected_area") or "")
    return result


def run_intake(settings: Settings, jira_key: str, llm_client: Any = None) -> dict[str, Any]:
    """Fetch + extract for one ticket. Returns {ticket, extracted}."""
    intake = fetch_ticket(settings, jira_key)
    extracted = extract_signals(settings, intake, llm_client=llm_client)
    log.info(
        "RCA intake %s: %d error strings, %d stack frames, area=%r",
        jira_key, len(extracted["error_messages"]),
        len(extracted["stack_frames"]), extracted["suspected_area"],
    )
    return {"ticket": intake.to_dict(), "extracted": extracted}


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_frames(frames: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(frames, list):
        return out
    for fr in frames:
        if isinstance(fr, dict):
            line = fr.get("line")
            out.append({
                "file": (str(fr["file"]) if fr.get("file") else None),
                "line": (int(line) if isinstance(line, int) or (isinstance(line, str) and line.isdigit()) else None),
                "symbol": (str(fr["symbol"]) if fr.get("symbol") else None),
                "raw": str(fr.get("raw") or ""),
            })
        elif isinstance(fr, str) and fr.strip():
            out.append({"file": None, "line": None, "symbol": None, "raw": fr.strip()})
    return out
