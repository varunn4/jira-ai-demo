"""Helpers for extracting structured JSON from model output."""

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def parse_model_json(model_output: str) -> dict[str, Any]:
    """Parse a JSON object from plain or fenced model output."""
    try:
        parsed = json.loads(model_output)
    except json.JSONDecodeError:
        log.debug("Direct JSON parse failed; attempting fenced/object extraction")
        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", model_output, re.DOTALL)
        if fenced_match:
            log.debug("Extracted JSON from fenced code block")
            return json.loads(fenced_match.group(1))

        object_match = re.search(r"\{.*\}", model_output, re.DOTALL)
        if object_match:
            log.debug("Extracted JSON from raw object match")
            return json.loads(object_match.group(0))

        log.error("Failed to parse JSON from model output (first 200 chars): %s", model_output[:200])
        raise

    if not isinstance(parsed, dict):
        log.error("Model output parsed as non-dict type=%s", type(parsed).__name__)
        raise json.JSONDecodeError("Expected a JSON object", model_output, 0)

    return parsed


def review_status(model_json: dict[str, Any]) -> str:
    """Return workflow status as good/bad from either compact or full validator JSON."""
    compact_status = str(model_json.get("status", "")).strip().lower()
    if compact_status in {"good", "bad"}:
        return compact_status

    final_decision = model_json.get("final_decision")
    if isinstance(final_decision, dict):
        final_status = str(final_decision.get("final_status", "")).strip().upper()
        can_move = final_decision.get("can_move_to_next_stage")
        if final_status == "PASS" and can_move is not False:
            return "good"

    return "bad"


def review_text(model_json: dict[str, Any]) -> str:
    """Return a concise human-facing review from compact or full validator JSON."""
    compact_review = model_json.get("review")
    if isinstance(compact_review, str) and compact_review.strip():
        return compact_review.strip()

    final_decision = model_json.get("final_decision")
    if isinstance(final_decision, dict):
        summary = final_decision.get("validator_summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()

    missing_items = model_json.get("missing_or_weak_items")
    if isinstance(missing_items, list) and missing_items:
        lines = []
        for item in missing_items[:5]:
            if not isinstance(item, dict):
                continue
            field = item.get("field_or_section") or "item"
            problem = item.get("problem") or item.get("recommended_fix") or "needs clarification"
            lines.append(f"{field}: {problem}")
        if lines:
            return "Ticket needs updates: " + "; ".join(lines)

    return "Ticket needs more detail before approval."
