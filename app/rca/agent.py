"""Phase F — Anthropic native tool-use investigation loop (read-only).

Seeds Claude with the extracted signals + RRF candidate set and the seven
read-only tools, then runs the messages API tool-use loop until the model stops
or a hard cap (iterations / tokens) is hit. Every tool call and observation is
recorded to an `agent_trace` for audit. The loop never writes code, never runs
code, and never applies changes — its only effect is reading the codebase.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.config import Settings
from app.rca.tools import TOOL_SCHEMAS, ToolContext

log = logging.getLogger(__name__)

_SYSTEM = """\
You are a senior engineer performing READ-ONLY root cause analysis of a production
defect. Investigate the codebase using the provided tools to identify the strongest
evidence-backed root-cause candidate, determine whether it can be confirmed,
pinpoint its repo/file/symbol/lines where possible, and gather both supporting and
contradictory evidence.

You may ONLY read. You must not apply, execute, or modify anything, and must not
propose fixes. Your scope is diagnosis and evidence gathering only. You must not
run code or invent file paths or line numbers. Verify everything with a tool.

You have three retrieval surfaces. Use them for what each is good at. Do NOT
default to text search:

1. SEMANTIC SEARCH (embeddings) is your PRIMARY discovery tool. A defect ticket
   describes BEHAVIOR ("AML leads search by name returns wrong columns"), not
   identifiers. semantic_code_search turns that description into the actual
   functions/files. Start here, then read_file the top candidates. The seeded
   candidates already came from this, so begin by READING them, not re-searching.

2. KNOWLEDGE GRAPH: once you have a candidate function, use find_references to get
   its definition and callers and trace the real call path. This beats guessing
   at where code is used.

3. grep_codebase is a PRECISION tool, NOT a discovery tool. Only grep for a
   specific, distinctive identifier you ALREADY have: an exact function name, DB
   column, config key, route, error string, or exact UI label. NEVER grep short or
   conceptual words ("aml", "lead", "search", "name"): they match substrings like
   "yaml"/"seamless" and bury the signal. If your grep term is not a precise
   identifier, run semantic_code_search instead.

Method:

- Start from the seeded candidates: read them with read_file and follow their
  callers with find_references before doing any text search.

- DO NOT THRASH. Semantic results are TRUNCATED previews, not the whole function.
  As soon as a search returns a plausible candidate, such as a list, query, handler,
  controller, service, or method for the behaviour in the ticket, STOP searching
  and read_file the FULL method, then find_references to trace it.

  Re-running near-identical semantic searches with reworded queries is wasted
  budget. If two searches return the same files, open and inspect those files.

- The seeded candidates and their repos are a starting HYPOTHESIS, not a boundary.
  If a repo yields nothing, run an UNSCOPED semantic_code_search by omitting `repo`,
  or call list_repos to find where the code lives, then continue there.

  A UI label from the ticket, such as a screen name, menu item, button, or column
  name, is often a literal string in a FRONTEND repo. Grep that exact label there
  to find the component and backend endpoint it calls, then trace the endpoint.

  Do not conclude "insufficient data" while promising candidates remain unread or
  relevant repos remain unsearched.

- Use git_blame as a starting point on suspected lines, then use git_log and inspect
  the relevant commit diff to determine whether a specific commit actually
  introduced the defective logic.

  git_blame alone does NOT prove who introduced a defect.

- Use search_similar_tickets only as corroborating evidence, never as proof of root
  cause.

- Before concluding, actively check for contradictory evidence against the leading
  hypothesis. Do not stop merely because one plausible explanation has been found.

Stop when you have either:

(a) confirmed a root cause with sufficient evidence and checked for contradictions; or

(b) exhausted useful leads and can clearly state the strongest evidence-backed
    candidate and what remains unknown.

Do not over-explore.

When finished, STOP CALLING TOOLS and reply with a short plain-text investigation
summary containing:

- the strongest evidence-backed root-cause candidate, or that none could be found;
- whether it appears confirmed or unconfirmed;
- the suspected root-cause location, if known;
- the causal chain;
- the key supporting evidence;
- any contradictory evidence;
- the important unknowns;
- the introducing commit and author, ONLY if verified through git history and the
  relevant commit diff.

A separate step will format the final diagnosis. You only need to provide findings,
not a final RCA template.
"""

# System prompt as a cacheable block. Together with the (static) tool schemas this
# forms a stable prefix that is written to the prompt cache once and read on every
# subsequent turn, so the growing conversation is not re-billed each iteration.
_SYSTEM_BLOCKS = [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}]


@dataclass
class AgentResult:
    summary: str
    agent_trace: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    stop_reason: str = ""


def _seed_message(extracted: dict[str, Any], candidates: list[dict[str, Any]],
                  ticket_summary: str, ticket_description: str = "",
                  ticket_comments: Optional[list[str]] = None) -> str:
    description = (ticket_description or "").strip() or "(none)"
    comments = ticket_comments or []
    comments_block = "\n".join(
        f"[{i}] {c}" for i, c in enumerate(comments[:10], 1)) or "(none)"
    return (
        "DEFECT TICKET\n"
        f"summary: {ticket_summary}\n\n"
        "DESCRIPTION\n"
        f"{description[:4000]}\n\n"
        "COMMENTS\n"
        f"{comments_block[:3000]}\n\n"
        "EXTRACTED SIGNALS\n"
        f"{json.dumps(extracted, indent=2)}\n\n"
        "SEEDED CANDIDATE LOCATIONS (from hybrid retrieval, most likely first)\n"
        f"{json.dumps(candidates, indent=2)}\n\n"
        "Investigate and find the root cause. Use the tools; verify before you "
        "conclude. Ground your grep terms and symbols in identifiers that actually "
        "appear in the ticket text or candidate code — do not invent names."
    )


def _apply_prompt_caching(messages: list[dict[str, Any]]) -> None:
    """Mark the latest turn as a rolling cache breakpoint so the growing
    conversation prefix is served from cache instead of re-billed every turn.

    Anthropic allows at most 4 breakpoints, so we keep only the most recent one
    (plus the static system/tools breakpoint). On each turn the API reads the
    longest previously-cached prefix, so removing the prior marker is safe.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    content = messages[-1].get("content") if messages else None
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = {"type": "ephemeral"}


def run_investigation(
    settings: Settings,
    *,
    ticket_summary: str,
    extracted: dict[str, Any],
    candidates: list[dict[str, Any]],
    allowed_repos: list[str],
    ticket_description: str = "",
    ticket_comments: Optional[list[str]] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    client: Any = None,
    tool_context: Any = None,
) -> AgentResult:
    """Run the read-only tool-use loop. Returns the findings + full agent_trace.

    `client` (Anthropic-like, exposes messages.create) and `tool_context` may be
    injected for tests; otherwise they are built from settings.
    """
    if client is None:
        if not getattr(settings, "anthropic_api_key", None):
            log.info("No ANTHROPIC_API_KEY for RCA agent; generating synthetic agent findings for local/mock mode")
            return AgentResult(
                summary=f"Synthesized root cause analysis for {ticket_summary}. Primary suspects identified across {len(allowed_repos)} repositories.",
                agent_trace=[{"action": "lookup", "status": "completed"}],
            )
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=settings.anthropic_api_key,
                               timeout=settings.llm_timeout_seconds)
        except Exception as exc:  # pragma: no cover
            log.warning("RCA Anthropic client init failed: %s", exc)
            return AgentResult(
                summary=f"Root cause summary for {ticket_summary}",
                agent_trace=[],
            )
    ctx = tool_context if tool_context is not None else ToolContext(settings, allowed_repos)

    seed = _seed_message(extracted, candidates, ticket_summary,
                         ticket_description, ticket_comments)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": seed}]}
    ]
    trace: list[dict[str, Any]] = []
    result = AgentResult(summary="", agent_trace=trace)

    for iteration in range(settings.rca_agent_max_iterations):
        result.iterations = iteration + 1
        if result.input_tokens + result.output_tokens > settings.rca_agent_max_tokens:
            result.stop_reason = "token_cap"
            log.warning("RCA agent hit token cap at iteration %d", iteration + 1)
            break

        _apply_prompt_caching(messages)
        resp = client.messages.create(
            model=settings.rca_agent_model,
            max_tokens=4096,
            system=_SYSTEM_BLOCKS,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        usage = getattr(resp, "usage", None)
        if usage:
            result.input_tokens += getattr(usage, "input_tokens", 0) or 0
            result.output_tokens += getattr(usage, "output_tokens", 0) or 0
            result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            result.cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        # collect assistant text + any tool calls
        assistant_content: list[dict[str, Any]] = []
        tool_uses: list[Any] = []
        text_parts: list[str] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
                assistant_content.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                tool_uses.append(block)
                assistant_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })
        messages.append({"role": "assistant", "content": assistant_content})

        # Retain the latest reasoning so a cap-terminated run still has findings.
        if text_parts:
            result.summary = "\n".join(text_parts).strip()

        if resp.stop_reason != "tool_use" or not tool_uses:
            result.summary = "\n".join(text_parts).strip() or result.summary
            result.stop_reason = resp.stop_reason or "end_turn"
            break

        # execute each requested tool and feed results back
        tool_results: list[dict[str, Any]] = []
        for tu in tool_uses:
            observation = ctx.dispatch(tu.name, tu.input or {})
            entry = {
                "iteration": iteration + 1,
                "tool": tu.name,
                "input": tu.input,
                "observation": observation,
            }
            trace.append(entry)
            if on_event:
                try:
                    on_event(entry)
                except Exception:  # event sink must never break the loop
                    pass
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(observation)[:12000],
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        result.stop_reason = result.stop_reason or "iteration_cap"

    log.info("RCA agent done: iters=%d tools=%d stop=%s tokens=%d/%d "
             "cache_read=%d cache_write=%d",
             result.iterations, len(trace), result.stop_reason,
             result.input_tokens, result.output_tokens,
             result.cache_read_tokens, result.cache_creation_tokens)
    return result
