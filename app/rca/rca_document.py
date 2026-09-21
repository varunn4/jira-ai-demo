"""Render an evidence-first diagnosis into the RCA output contract.

build_document() shapes the structured diagnosis into the delivery model;
render_markdown() produces the human/Jira text following the fixed OUTPUT FORMAT
(Issue Classification → Confidence → FACTS → INFERENCES → UNKNOWNS → ROOT CAUSE /
MOST LIKELY ROOT CAUSE → INTRODUCED BY → EVIDENCE → CONTRIBUTING FACTORS →
NEXT ACTION); render_docx() produces the Word version.

A root cause is headed "ROOT CAUSE" only when confirmed (explains every symptom,
≥2 independent evidence sources, no contradiction); otherwise it is headed
"MOST LIKELY ROOT CAUSE" and lists the evidence still required. EVIDENCE is
grouped into Code / Git / Logs / Tests / Configuration / Ticket. NEXT ACTION
matches confidence: the exact fix at High, verification steps at Medium, the
additional evidence required at Low. An INSUFFICIENT DATA run renders ONLY that
single line.
"""
from __future__ import annotations

from typing import Any

from app.rca.synthesis import (
    EVIDENCE_CATEGORIES, INSUFFICIENT_DATA_MESSAGE, _evidence_category,
)


def build_document(ticket: dict[str, Any], extracted: dict[str, Any],
                   diagnosis: dict[str, Any], agent_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the delivery model from the evidence-first diagnosis."""
    loc = _format_location(diagnosis.get("root_cause_location") or {})
    root_cause = str(diagnosis.get("root_cause") or "Undetermined.")
    return {
        "title": f"{ticket.get('key', '')} – {ticket.get('summary', '')}".strip(" –"),
        "key": ticket.get("key", ""),
        "insufficient_data": root_cause == INSUFFICIENT_DATA_MESSAGE,
        "issue_classification": diagnosis.get("issue_classification", "Cannot Determine"),
        "confidence_label": diagnosis.get("confidence_label", "Low"),
        "root_cause": root_cause,
        "root_cause_status": diagnosis.get("root_cause_status", "undetermined"),
        "root_cause_location": loc,
        "introduced_by": diagnosis.get("introduced_by"),
        "facts": diagnosis.get("facts", []),
        "inferences": diagnosis.get("inferences", []),
        "unknowns": diagnosis.get("unknowns", []),
        "evidence": diagnosis.get("evidence", []),
        "contributing_factors": diagnosis.get("contributing_factors", []),
        "additional_evidence_required": diagnosis.get("additional_evidence_required", []),
        "verification_steps": diagnosis.get("verification_steps", []),
        "recommended_fix": diagnosis.get("recommended_fix"),
        "agent_meta": agent_meta or {},
    }


def _format_location(rc: dict[str, Any]) -> str:
    parts = []
    if rc.get("repo"):
        parts.append(rc["repo"])
    file_sym = rc.get("file") or ""
    if rc.get("symbol"):
        file_sym = f"{file_sym} :: {rc['symbol']}" if file_sym else rc["symbol"]
    if file_sym:
        parts.append(file_sym)
    loc = " / ".join(parts)
    if rc.get("lines"):
        loc += f" (lines {rc['lines']})"
    return loc or "Not localized"


# ── markdown rendering (drives both on-screen text and the docx) ──────────────

def render_markdown(document: dict[str, Any]) -> str:
    key = document.get("key", "")

    # RULE 2 — an INSUFFICIENT DATA run states only that. No fabricated sections.
    if document.get("insufficient_data"):
        return (f"# Root Cause Analysis (RCA) — {key}\n\n"
                f"{INSUFFICIENT_DATA_MESSAGE}")

    classification = document.get("issue_classification", "Cannot Determine")
    confidence = document.get("confidence_label", "Low")
    out: list[str] = []

    out.append(f"# Root Cause Analysis (RCA) — {key}")
    out.append(f"_AI-generated diagnosis · {classification} · confidence {confidence}_\n")

    out.append(f"**Issue Classification:** {classification}")
    out.append(f"**Confidence:** {confidence}\n")

    _bullets(out, "FACTS", document.get("facts", []))
    _bullets(out, "INFERENCES", document.get("inferences", []))
    _bullets(out, "UNKNOWNS", document.get("unknowns", []))

    # Confirmed cause → "ROOT CAUSE"; a leading-but-unverified cause →
    # "MOST LIKELY ROOT CAUSE" with the evidence still needed to confirm it.
    status = document.get("root_cause_status", "undetermined")
    most_likely = status == "most_likely"
    out.append("## MOST LIKELY ROOT CAUSE" if most_likely else "## ROOT CAUSE")
    out.append(document.get("root_cause", "Undetermined.") + "\n")
    if most_likely:
        extra = [str(x).strip() for x in document.get("additional_evidence_required", []) if str(x).strip()]
        out.append("**Additional evidence required:**")
        out.extend(f"- {x}" for x in (extra or ["(not specified)"]))
        out.append("")

    _render_introduced_by(out, document.get("introduced_by"))
    _render_evidence(out, document.get("evidence", []))

    cf = document.get("contributing_factors", [])
    if cf:
        _bullets(out, "CONTRIBUTING FACTORS", cf)

    _render_next_action(out, document)
    return "\n".join(out)


def _render_introduced_by(out: list[str], introduced: Any) -> None:
    """Authorship block from git history only (RULE 10). Undetermined when the
    introducing commit is unknown; never inferred, never framed as blame."""
    out.append("## INTRODUCED BY")
    if isinstance(introduced, dict) and introduced.get("commit"):
        out.append(f"Introduced By: {introduced.get('name') or 'Unknown author'}")
        out.append(f"Commit: {introduced['commit']}")
        if introduced.get("date"):
            out.append(f"Date: {introduced['date']}")
    else:
        out.append("Introduced By: Undetermined")
    out.append("")


def _render_evidence(out: list[str], evidence: list[dict[str, Any]]) -> None:
    """Evidence grouped into the fixed buckets; empty categories are shown as
    unavailable so the reader can see what was and wasn't found."""
    out.append("## EVIDENCE")
    by_cat: dict[str, list[dict[str, Any]]] = {c: [] for c in EVIDENCE_CATEGORIES}
    for e in evidence:
        cat = e.get("category")
        if cat not in by_cat:  # legacy/blame items carry `type`, not `category`
            cat = _evidence_category(e.get("category") or e.get("type"), e.get("ref", ""))
        by_cat[cat].append(e)
    for cat in EVIDENCE_CATEGORIES:
        out.append(f"**{cat}**")
        items = by_cat[cat]
        if items:
            for e in items:
                ref = f" — `{e.get('ref')}`" if e.get("ref") else ""
                out.append(f"- {e.get('detail','')}{ref}")
        else:
            out.append("- _None available._")
        out.append("")


def _render_next_action(out: list[str], document: dict[str, Any]) -> None:
    """NEXT ACTION matches confidence (RULE 12): the exact fix at High,
    verification steps at Medium, additional evidence required at Low."""
    out.append("## NEXT ACTION")
    confidence = document.get("confidence_label", "Low")
    fix = document.get("recommended_fix")
    if confidence == "High" and fix:
        out.append(fix)
    elif confidence == "Medium":
        steps = [str(s).strip() for s in document.get("verification_steps", []) if str(s).strip()]
        out.append("Verify before changing code:")
        out.extend(f"- {s}" for s in (steps
                   or ["Confirm the suspected cause against the affected code path before editing."]))
    else:
        needed = [str(x).strip() for x in document.get("additional_evidence_required", []) if str(x).strip()]
        out.append("Do not change code yet. Additional evidence required:")
        out.extend(f"- {x}" for x in (needed
                   or ["Additional evidence is required to localize the root cause."]))
    return


def _bullets(out: list[str], heading: str, items: list[Any]) -> None:
    """Append a `## heading` section of bullet items; skip when empty."""
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return
    out.append(f"## {heading}")
    for item in items:
        out.append(f"- {item}")
    out.append("")


def render_docx(document: dict[str, Any]) -> bytes:
    from app.markdown_docx import markdown_to_docx_bytes
    md = render_markdown(document)
    return markdown_to_docx_bytes(md, title=f"RCA — {document.get('key','')}")
