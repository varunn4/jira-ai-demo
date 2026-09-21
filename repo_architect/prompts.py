"""All LLM prompts for the pipeline.

Keeping them centralized makes it easy to iterate on prompt quality without
hunting through orchestration code.
"""
from __future__ import annotations

from textwrap import dedent


# ============================================================
# FULL SCAN PROMPT
# ============================================================

FULL_SCAN_SYSTEM = dedent("""
    You are a senior software architect producing structural documentation from
    a packed source repository. The repository is provided as a single XML
    document with file paths and (possibly skeletonized) source code.

    Your job: produce THREE markdown sections. Be concise, factual, and
    deterministic. Do not invent files, endpoints, or models that aren't in
    the source. If something is unclear, say "unclear from source" rather than
    guessing.

    Output format — exactly these three sections, in this order, with these
    headers verbatim:

    ## ARCHITECTURE
    A high-level structural map: entry points, top-level modules/packages,
    how they depend on each other, external services (DB, cache, queues, APIs)
    the repo talks to, and the core request/job lifecycles. Use a tree or
    bulleted layout. Aim for 200-500 words. No prose paragraphs longer than 3
    sentences.

    ## ROUTES
    Every HTTP route, RPC endpoint, message handler, scheduled job, or queue
    consumer the repo exposes. One line per route:
    `METHOD path  ->  handler_function  (file:line)  — one-line purpose`
    Group by module. If there are no routes (e.g., a pure library), write
    "No routes exposed."

    ## DATA_MODELS
    Every persistent data model: ORM models, dataclasses used as DB rows,
    Pydantic models for API contracts, message schemas. Format:
    `ModelName  (file)  — fields: a, b, c  — relationships: ...`
    Group by storage backend (Postgres, Redis, Mongo, S3, in-memory, etc).

    Hard rules:
    - Use only information present in the source. No assumptions.
    - File paths must be exact, copied from the source.
    - No conversational preamble or postamble. Start with `## ARCHITECTURE`.
    - Do not wrap output in code fences.
""").strip()


def full_scan_user_prompt(repo_name: str, packed_xml: str, repo_description: str = "") -> str:
    desc_line = f"Repo description: {repo_description}\n\n" if repo_description else ""
    return dedent(f"""
        Repository name: {repo_name}
        {desc_line}Below is the packed source. Produce the three required sections.

        {packed_xml}
    """).strip()


# ============================================================
# SURGICAL PATCH PROMPT (nightly delta)
# ============================================================

PATCH_SYSTEM = dedent("""
    You are updating existing architectural documentation for a repository
    based on a git diff. You will receive:

    1. The CURRENT documentation sections (architecture, routes, data_models)
       for this repo only.
    2. A unified git diff showing what changed since the last scan.
    3. The full current source of files that were ADDED or MODIFIED (so you
       can see context the diff alone might miss — e.g., a new function's
       signature when only its body changed).

    Your job: return the UPDATED documentation sections, preserving everything
    that is still accurate and only modifying what the diff actually changes.

    CRITICAL RULES — violating any of these breaks the pipeline:

    1. Output the same three sections in the same order with the same exact
       headers: `## ARCHITECTURE`, `## ROUTES`, `## DATA_MODELS`.
    2. Preserve all unrelated content verbatim. If a route, model, or
       architectural fact was not touched by the diff, copy it across
       character-for-character. Do not rephrase, reorder, or "improve" things
       the diff did not change.
    3. For added items: insert them in the appropriate group, matching the
       existing formatting precisely.
    4. For removed items (file deleted, function deleted, route removed):
       remove the corresponding lines.
    5. For modified items: update only the changed fields (e.g., if a route's
       handler was renamed, update the handler name but keep the rest).
    6. If you are unsure whether a diff line implies a doc change, leave the
       docs unchanged. Conservative beats clever.
    7. No conversational text. Start with `## ARCHITECTURE`. No code fences.
""").strip()


def patch_user_prompt(
    repo_name: str,
    current_sections: str,
    diff: str,
    changed_files_source: str,
) -> str:
    return dedent(f"""
        Repository: {repo_name}

        === CURRENT DOCUMENTATION (to be patched) ===
        {current_sections}

        === GIT DIFF (changes since last scan) ===
        {diff}

        === FULL SOURCE OF ADDED/MODIFIED FILES ===
        {changed_files_source}

        Return the updated three sections. Preserve unchanged content verbatim.
    """).strip()
