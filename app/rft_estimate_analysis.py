"""LLM-backed effort estimation for the WF7 RFT estimate report.

For each ticket (with an Original Estimate) we ground the model in the ticket
fields plus best-effort codebase context, then ask it to predict the realistic
effort an *average experienced developer* would need. We compare that against
the Jira Original Estimate and flag tickets that drift beyond a threshold.

Predictions are cached in ``rft_estimate_predictions`` keyed by a content hash
(summary + description + issue type + original estimate), so the daily run only
calls the LLM for new/changed tickets — subsequent runs are near-free.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from app.config import Settings
from app.json_utils import parse_model_json
from app.llm_client import build_llm_client

LOGGER = logging.getLogger(__name__)

# Bump when the prompt / output shape changes so cached rows regenerate.
# v3: estimates grounded in the Neo4j code graph.
# v4: baseline persona changed to a median (50th-percentile) FAANG engineer.
# v5: configurable team persona + history-calibration blend.
_PROMPT_VERSION = "v5"

_SYSTEM_PROMPT_TEMPLATE = (
    "You are a senior engineering estimator. Estimate how long a single "
    "__PERSONA__ would realistically need to fully deliver the work: "
    "implementation, self-test, code review fixes. Use the ticket details and "
    "any codebase context provided. Account for complexity, unknowns, "
    "integration and testing — not just the happy-path coding time. You are also "
    "given the team's current Original Estimate (the developer's own estimate); "
    "compare your realistic estimate against it. "
    "Respond with STRICT JSON only:\n"
    '{"predicted_hours": <number, working hours>, '
    '"confidence": "low|medium|high", '
    '"reason": "<= 12 words: the main driver, or why confidence is not high", '
    '"explanation": "1-2 complete sentences explaining why your estimate is '
    'higher/lower than (or matches) the current Original Estimate"}'
)


def _system_prompt(settings: Settings) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.replace("__PERSONA__", settings.rft_estimate_benchmark_persona)


def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.database_url, row_factory=dict_row)


def ensure_predictions_schema(settings: Settings) -> None:
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rft_estimate_predictions (
                jira_ticket_id   TEXT PRIMARY KEY,
                content_hash     TEXT NOT NULL,
                original_seconds BIGINT,
                predicted_hours  DOUBLE PRECISION,
                confidence       TEXT,
                rationale        TEXT,
                flag             TEXT,
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Self-healing columns added after the initial release.
        conn.execute("ALTER TABLE rft_estimate_predictions ADD COLUMN IF NOT EXISTS reason TEXT")
        conn.execute("ALTER TABLE rft_estimate_predictions ADD COLUMN IF NOT EXISTS explanation TEXT")
        conn.commit()


def _content_hash(ticket: dict[str, Any], persona: str = "") -> str:
    # Persona is part of the prompt, so changing it must regenerate cached rows.
    raw = _PROMPT_VERSION + "|" + persona + "|" + "|".join(
        str(ticket.get(k, ""))
        for k in ("summary", "description", "issue_type", "estimate_seconds")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_cache(settings: Settings, keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    try:
        with _connect(settings) as conn:
            rows = conn.execute(
                "SELECT * FROM rft_estimate_predictions WHERE jira_ticket_id = ANY(%s)",
                (keys,),
            ).fetchall()
        return {r["jira_ticket_id"]: r for r in rows}
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("prediction cache read skipped: %s", exc)
        return {}


def _store(settings: Settings, key: str, h: str, ticket: dict[str, Any], pred: dict[str, Any]) -> None:
    try:
        with _connect(settings) as conn:
            conn.execute(
                """
                INSERT INTO rft_estimate_predictions
                    (jira_ticket_id, content_hash, original_seconds,
                     predicted_hours, confidence, reason, explanation, flag, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (jira_ticket_id) DO UPDATE SET
                    content_hash     = EXCLUDED.content_hash,
                    original_seconds = EXCLUDED.original_seconds,
                    predicted_hours  = EXCLUDED.predicted_hours,
                    confidence       = EXCLUDED.confidence,
                    reason           = EXCLUDED.reason,
                    explanation      = EXCLUDED.explanation,
                    flag             = EXCLUDED.flag,
                    updated_at       = NOW()
                """,
                (
                    key,
                    h,
                    ticket.get("estimate_seconds"),
                    pred.get("predicted_hours"),
                    pred.get("confidence"),
                    pred.get("reason"),
                    pred.get("explanation"),
                    pred.get("flag"),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("prediction cache write skipped for %s: %s", key, exc)


def _codebase_context(settings: Settings, ticket: dict[str, Any], graph_reader=None) -> str:
    """Best-effort codebase context: Neo4j code graph first, repograph fallback."""
    # 1) Neo4j knowledge graph — complexity/coupling/churn of related code.
    if graph_reader is not None:
        try:
            text = graph_reader.text_for(ticket)
            if text:
                return text[:2500]
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("neo4j context failed for %s: %s", ticket.get("key"), exc)

    # 2) Fallback: repograph HTTP service.
    try:
        from app.graph_context import GraphContextClient

        ctx = GraphContextClient(settings).fetch_context(ticket_data=ticket)
        items = ctx.get("items", []) or []
        if not items:
            return ""
        text = json.dumps(items, ensure_ascii=False)
        return text[:2500]
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("codebase context unavailable for %s: %s", ticket.get("key"), exc)
        return ""


def _flag(predicted_seconds: float, original_seconds: float, threshold_pct: int) -> tuple[str, float]:
    if not original_seconds:
        return "n/a", 0.0
    delta_pct = (predicted_seconds - original_seconds) / original_seconds * 100.0
    if delta_pct > threshold_pct:
        return "UNDER", delta_pct      # original estimate too low — needs more time
    if delta_pct < -threshold_pct:
        return "PLUS", delta_pct       # original estimate too high (padded)
    return "OK", delta_pct


def _predict_one(settings, llm, ticket: dict[str, Any], graph_reader=None) -> Optional[dict[str, Any]]:
    context = _codebase_context(settings, ticket, graph_reader)
    user = json.dumps(
        {
            "ticket": {
                "key": ticket.get("key"),
                "summary": ticket.get("summary"),
                "description": (ticket.get("description") or "")[:4000],
                "issue_type": ticket.get("issue_type"),
                "original_estimate": ticket.get("estimate"),
            },
            "codebase_context": context or "none available",
        },
        ensure_ascii=False,
    )
    try:
        raw = llm.complete(_system_prompt(settings), user, max_tokens=400)
        parsed = parse_model_json(raw)
        hours = float(parsed.get("predicted_hours"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("estimate prediction failed for %s: %s", ticket.get("key"), exc)
        return None
    return {
        "predicted_hours": round(hours, 2),
        "confidence": str(parsed.get("confidence", "")).lower()[:10],
        "reason": str(parsed.get("reason", ""))[:140],
        "explanation": str(parsed.get("explanation", ""))[:400],
    }


def analyze_tickets(settings: Settings, tickets: list[dict[str, Any]]) -> dict[str, Any]:
    """Enrich each ticket in place with predicted_hours/predicted/delta_pct/flag.

    Returns {"analyzed": int, "flagged": int, "llm": bool}. Never raises — on any
    failure the ticket simply carries no prediction (flag 'n/a').
    """
    threshold = settings.rft_estimate_flag_threshold_pct
    if not settings.database_url:
        # Cache needs a DB; we can still analyze live but won't persist.
        LOGGER.info("rft analysis: no DATABASE_URL — predictions will not be cached")
    else:
        ensure_predictions_schema(settings)

    cache = _load_cache(settings, [t["key"] for t in tickets])

    try:
        llm = build_llm_client(settings)
        llm_ok = True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("rft analysis: LLM unavailable (%s); listing without predictions", exc)
        llm = None
        llm_ok = False

    # Open one pooled Neo4j reader for the whole batch (None if disabled/unreachable).
    graph_reader = None
    if settings.rft_estimate_use_graph:
        try:
            from app.neo4j_graph.ticket_context import open_reader

            graph_reader = open_reader()
            if graph_reader:
                LOGGER.info("rft analysis: grounding estimates in the Neo4j code graph")
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("rft analysis: neo4j reader unavailable: %s", exc)

    # Learn the team's estimate→actual overrun from closed tickets once per run.
    from app.rft_calibration import calibrated_hours, team_overrun_factor

    calibration = team_overrun_factor(settings)
    cal_factor = calibration["factor"]
    cal_weight = settings.rft_estimate_history_weight if calibration["available"] else 0.0

    persona = settings.rft_estimate_benchmark_persona
    analyzed = 0
    flagged = 0
    # 0 / negative = unlimited: analyze every eligible (uncached) ticket.
    budget = settings.rft_estimate_max_analyze
    unlimited = budget <= 0

    try:
        for ticket in tickets:
            key = ticket["key"]
            h = _content_hash(ticket, persona)
            pred: Optional[dict[str, Any]] = None

            cached = cache.get(key)
            if cached and cached.get("content_hash") == h and cached.get("predicted_hours") is not None:
                pred = {
                    "predicted_hours": float(cached["predicted_hours"]),
                    "confidence": cached.get("confidence") or "",
                    "reason": cached.get("reason") or "",
                    "explanation": cached.get("explanation") or "",
                }
            elif llm_ok and (unlimited or budget > 0):
                pred = _predict_one(settings, llm, ticket, graph_reader)
                budget -= 1

            if pred is None:
                ticket["predicted_hours"] = None
                ticket["predicted"] = "—"
                ticket["delta_pct"] = None
                ticket["flag"] = "n/a"
                ticket["confidence"] = ""
                ticket["reason"] = ""
                ticket["explanation"] = ""
                continue

            # `pred["predicted_hours"]` is the raw LLM estimate (cached as-is).
            # Calibrate it to the team's real pace at report time so the factor
            # can update without invalidating the per-ticket LLM cache.
            raw_hours = pred["predicted_hours"]
            should_have_hours = calibrated_hours(
                raw_hours, int(ticket["estimate_seconds"]), cal_factor, cal_weight
            )
            predicted_seconds = should_have_hours * 3600.0
            flag, delta_pct = _flag(predicted_seconds, float(ticket["estimate_seconds"]), threshold)
            ticket["raw_predicted_hours"] = raw_hours
            ticket["predicted_hours"] = should_have_hours
            ticket["predicted"] = _fmt_hours(should_have_hours)
            ticket["delta_pct"] = round(delta_pct)
            ticket["flag"] = flag
            ticket["confidence"] = pred["confidence"]
            ticket["reason"] = pred["reason"]
            ticket["explanation"] = pred["explanation"]
            analyzed += 1
            if flag in ("UNDER", "PLUS"):
                flagged += 1

            if settings.database_url and not (cached and cached.get("content_hash") == h):
                _store(settings, key, h, ticket, {**pred, "flag": flag})
    finally:
        if graph_reader is not None:
            graph_reader.close()

    return {
        "analyzed": analyzed,
        "flagged": flagged,
        "llm": llm_ok,
        "calibration": calibration,
        "persona": persona,
    }


def _fmt_hours(hours: float) -> str:
    """Compact 'Xd Yh' using Jira's 8h working day."""
    seconds = int(round(hours * 3600))
    if seconds <= 0:
        return "0h"
    parts: list[str] = []
    for label, size in (("w", 5 * 8 * 3600), ("d", 8 * 3600), ("h", 3600), ("m", 60)):
        if seconds >= size:
            qty, seconds = divmod(seconds, size)
            parts.append(f"{qty}{label}")
    return " ".join(parts[:2]) or "0h"
