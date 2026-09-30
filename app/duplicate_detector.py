"""Duplicate ticket detection for Workflow1.

Two hard rules, in order:

1. Same repository, or it is not a duplicate. A login bug in `buzz` and a login
   bug in `admin-web` are different tickets no matter how alike they read, so the
   repository gate runs before any similarity work.

2. Same underlying issue, not similar wording. Keyword overlap calls "update the
   Node version in package.json" and "update the package.json dependencies" a
   match when they are unrelated, and misses "login times out" versus
   "auth session expires immediately" when they are the same defect. The verdict
   is therefore always made by the LLM reading both tickets in full.

Vector search, when Qdrant and an embedding backend happen to be reachable, is
used only to widen the candidate pool. It never decides the verdict, so the
behaviour of this module is identical whether or not those services are running.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)

# Candidates handed to the LLM. Above roughly a dozen the judgement degrades and
# the prompt gets expensive, and repo-scoped recall is high enough that it rarely binds.
MAX_CANDIDATES = 8

# Small in-process verdict cache. Its job is narrowly to stop the repeated submits of
# the same draft - a user correcting a rejection and resubmitting - from paying for an
# LLM call every time. Deliberately short-lived and tiny: a stale duplicate verdict is
# worse than a redundant call, and on Render each instance keeps its own copy.
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 64
_verdict_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()


def _cache_key(summary: str, description: str, repo_key: str) -> str:
    raw = f"{repo_key}\x00{summary.strip().lower()}\x00{description.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str):
    entry = _verdict_cache.get(key)
    if not entry:
        return None
    stored_at, verdict = entry
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        _verdict_cache.pop(key, None)
        return None
    _verdict_cache.move_to_end(key)
    return verdict


def _cache_put(key: str, verdict) -> None:
    _verdict_cache[key] = (time.time(), verdict)
    _verdict_cache.move_to_end(key)
    while len(_verdict_cache) > _CACHE_MAX_ENTRIES:
        _verdict_cache.popitem(last=False)


@dataclass
class DuplicateVerdict:
    is_duplicate: bool = False
    action: str = "none"  # "block" | "warn" | "none"
    ticket_key: str = ""
    ticket_summary: str = ""
    ticket_status: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    candidates_considered: int = 0
    checked: bool = False  # False when the check could not run at all

    def as_review_note(self) -> str:
        """Human-readable block for the Jira comment, Slack post and UI review text."""
        if not self.is_duplicate:
            return ""
        header = (
            "DUPLICATE TICKET DETECTED"
            if self.action == "block"
            else "POSSIBLE DUPLICATE - PLEASE CONFIRM BEFORE PROCEEDING"
        )
        status = f" (status: {self.ticket_status})" if self.ticket_status else ""
        return (
            f"[{header}]\n"
            f"This request appears to describe the same underlying issue as "
            f"*{self.ticket_key}*{status}: {self.ticket_summary}\n"
            f"Confidence: {self.confidence:.0%}\n"
            f"Assessment: {self.reasoning}"
        )


@dataclass
class _Candidate:
    key: str
    summary: str = ""
    description: str = ""
    status: str = ""
    repo: str = ""
    sources: set[str] = field(default_factory=set)


_REPO_URL_RE = re.compile(
    r"(?:https?://|git@|ssh://)?(?:www\.)?github\.com[/:]([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?=[\s/)\]>,;]|$)",
    re.IGNORECASE,
)


def repo_identity(repo: str | None) -> tuple[str, str]:
    """Reduce a repository reference to (owner/name, name).

    The owner half is often unavailable - the app UI supplies connected workspace
    repos by bare name - so both halves are returned and the caller decides how
    strictly to compare. All of these yield ("ranjank2alpha/buzz", "buzz"):

        https://github.com/ranjank2alpha/buzz
        https://github.com/ranjank2alpha/buzz.git
        git@github.com:ranjank2alpha/buzz.git

    while a bare "buzz" yields ("", "buzz").
    """
    text = (repo or "").strip().lower()
    if not text or text in {"none", "none provided", "not provided", "n/a", "null"}:
        return "", ""

    text = re.sub(r"\s*\(.*\)\s*$", "", text).strip()  # drop trailing annotations

    match = _REPO_URL_RE.search(text)
    if match:
        return f"{match.group(1)}/{match.group(2)}", match.group(2)

    text = re.sub(r"^(https?://|git@|ssh://)", "", text)
    text = text.split("#")[0].split("?")[0].rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]

    parts = [p for p in re.split(r"[/:]", text) if p]
    if not parts:
        return "", ""
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}", parts[-1]
    return "", parts[-1]


def normalize_repo(repo: str | None) -> str:
    """Bare repository name. Retained for callers that only need the short key."""
    return repo_identity(repo)[1]


def repos_match(a: str | None, b: str | None) -> bool:
    """Whether two repository references point at the same repository.

    When both sides carry an owner, the owner must agree - so orgA/buzz and
    orgB/buzz are correctly treated as different repositories. When either side
    is a bare name (which is all the app UI provides), the names alone decide.
    """
    a_full, a_name = repo_identity(a)
    b_full, b_name = repo_identity(b)
    if not a_name or not b_name:
        return False
    if a_full and b_full:
        return a_full == b_full
    return a_name == b_name


# How long a creation claim stays valid. Long enough to cover a full create
# (two LLM calls plus the Jira round trip), short enough that a crashed request
# cannot block the same ticket for long.
CLAIM_TTL_SECONDS = 180


class CreationClaim:
    """Short-lived exclusive claim on (repository + summary) during ticket creation.

    Duplicate detection only sees committed rows, so two identical submissions
    arriving together both found nothing and both created a ticket. Taking a claim
    before the check serialises them: the first proceeds, the second is told a
    matching request is already in flight. Released in a finally block, and
    self-expiring so a crash cannot wedge a ticket permanently.

    Degrades open: if the database is unreachable the claim is treated as granted
    rather than blocking legitimate ticket creation.
    """

    def __init__(self, settings: Settings, *, repo: str | None, summary: str, actor: str = "") -> None:
        self.settings = settings
        self.actor = actor or "unknown"
        repo_key = normalize_repo(repo)
        normalised = " ".join((summary or "").lower().split())
        self.key = hashlib.sha256(f"{repo_key}\x00{normalised}".encode("utf-8")).hexdigest()
        self.granted = False

    def __enter__(self) -> "CreationClaim":
        if not self.settings.database_url:
            self.granted = True
            return self
        try:
            import psycopg

            with psycopg.connect(self.settings.database_url) as conn:
                # Clear expired claims first so a crashed request never wedges a key.
                conn.execute(
                    "DELETE FROM ticket_creation_claims "
                    "WHERE claimed_at < NOW() - make_interval(secs => %s)",
                    (CLAIM_TTL_SECONDS,),
                )
                row = conn.execute(
                    """
                    INSERT INTO ticket_creation_claims (claim_key, claimed_by)
                    VALUES (%s, %s)
                    ON CONFLICT (claim_key) DO NOTHING
                    RETURNING claim_key
                    """,
                    (self.key, self.actor),
                ).fetchone()
                conn.commit()
            self.granted = row is not None
        except Exception as exc:  # noqa: BLE001 - never block creation on a claim failure
            log.warning("Creation claim unavailable (proceeding without it): %s", exc)
            self.granted = True
        if not self.granted:
            log.info("Creation claim refused: an identical request is already in flight")
        return self

    def __exit__(self, *exc_info) -> None:
        if not self.granted or not self.settings.database_url:
            return
        try:
            import psycopg

            with psycopg.connect(self.settings.database_url) as conn:
                conn.execute("DELETE FROM ticket_creation_claims WHERE claim_key = %s", (self.key,))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not release creation claim (it will expire): %s", exc)


class DuplicateDetector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── public entry point ────────────────────────────────────────────────────

    def check(
        self,
        *,
        summary: str,
        description: str,
        repo: str | None,
        exclude_key: str | None = None,
        allow_duplicate: bool = False,
    ) -> DuplicateVerdict:
        verdict = DuplicateVerdict()

        repo_key = normalize_repo(repo)
        if not repo_key:
            log.info("Duplicate check skipped: no repository on the ticket")
            return verdict

        cache_key = _cache_key(summary, description, repo_key)
        cached = _cache_get(cache_key)
        if cached is not None:
            log.info("Duplicate check served from cache for repo '%s'", repo_key)
            return self._apply_override(cached, allow_duplicate)

        candidates = self._collect_candidates(repo_key, summary, description, exclude_key)
        verdict.candidates_considered = len(candidates)
        if not candidates:
            verdict.checked = True
            log.info("Duplicate check: no prior tickets for repo '%s'", repo_key)
            _cache_put(cache_key, verdict)
            return verdict

        judged = self._adjudicate(summary, description, candidates)
        if judged is None:
            return verdict  # checked stays False: we could not decide, so claim nothing

        verdict.checked = True
        matched_key = str(judged.get("duplicate_of") or "").strip().upper()
        try:
            confidence = float(judged.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if not matched_key or matched_key in {"NONE", "NULL"}:
            return verdict

        match = next((c for c in candidates if c.key.upper() == matched_key), None)
        if match is None:
            log.warning("LLM named %s as a duplicate but it was not a candidate; ignoring", matched_key)
            return verdict

        block_at = self.settings.duplicate_block_confidence
        warn_at = self.settings.duplicate_warn_confidence

        if confidence >= block_at:
            verdict.action = "block"
        elif confidence >= warn_at:
            verdict.action = "warn"
        else:
            return verdict

        verdict.is_duplicate = True
        verdict.ticket_key = match.key
        verdict.ticket_summary = match.summary
        verdict.ticket_status = match.status
        verdict.confidence = confidence
        verdict.reasoning = str(judged.get("reasoning") or "").strip()

        log.info(
            "Duplicate check: %s of %s (confidence=%.2f, repo=%s)",
            verdict.action,
            verdict.ticket_key,
            confidence,
            repo_key,
        )
        _cache_put(cache_key, verdict)
        return self._apply_override(verdict, allow_duplicate)

    @staticmethod
    def _apply_override(verdict: DuplicateVerdict, allow_duplicate: bool) -> DuplicateVerdict:
        """Honour an explicit human decision that this is not a duplicate.

        A block is downgraded to a warning rather than erased: the reviewer said to
        proceed, but the suspected match is still worth recording on the ticket. The
        cached verdict is never mutated, so one person's override cannot leak into
        somebody else's check.
        """
        if not allow_duplicate or verdict.action != "block":
            return replace(verdict)
        log.info("Duplicate block on %s overridden by explicit request", verdict.ticket_key)
        return replace(
            verdict,
            action="warn",
            reasoning=(
                f"{verdict.reasoning} "
                "A reviewer explicitly confirmed this is distinct work and chose to proceed."
            ).strip(),
        )

    # ── candidate retrieval ───────────────────────────────────────────────────

    def _collect_candidates(
        self, repo_key: str, summary: str, description: str, exclude_key: str | None
    ) -> list[_Candidate]:
        by_key: dict[str, _Candidate] = {}

        for cand in self._candidates_from_postgres(repo_key):
            by_key.setdefault(cand.key.upper(), cand).sources.add("postgres")

        for cand in self._candidates_from_vectors(repo_key, summary, description):
            existing = by_key.get(cand.key.upper())
            if existing:
                existing.sources.add("vector")
            else:
                cand.sources.add("vector")
                by_key[cand.key.upper()] = cand

        if exclude_key:
            by_key.pop(exclude_key.strip().upper(), None)

        return list(by_key.values())[:MAX_CANDIDATES]

    def _candidates_from_postgres(self, repo_key: str) -> list[_Candidate]:
        """Every Jira ticket belonging to this repository.

        Driven from jira_ticket_cache, not from `tickets`. `tickets` only ever holds
        what Workflow1 or the app UI processed - on a live instance that was 3 rows out
        of 30, so anything raised directly in the Jira UI was invisible to duplicate
        detection. The cache mirrors all of Jira, and `tickets` is joined on for the
        repository recorded by the workflows.

        A ticket's repository comes from the workflow payload when we have it, and
        otherwise from a GitHub reference in the summary or description, which is how
        repos are attached to tickets raised directly in Jira.

        The repository filter runs in SQL so a LIMIT can never silently drop matching
        candidates the way a fetch-then-filter-in-Python pass does.
        """
        if not self.settings.database_url or not repo_key:
            return []
        try:
            import psycopg
            from psycopg.rows import dict_row

            like = f"%{repo_key}%"
            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    """
                    SELECT
                        COALESCE(c.ticket_key, t.jira_ticket_id) AS key,
                        COALESCE(c.summary, t.jira_payload->>'summary', '')          AS summary,
                        COALESCE(c.description, t.jira_payload->>'description', '')  AS description,
                        COALESCE(c.status, t.status, '')                             AS status,
                        COALESCE(
                            t.jira_payload->>'github_repo',
                            t.jira_payload->>'github_repo_url',
                            ''
                        ) AS payload_repo
                    FROM jira_ticket_cache c
                    FULL OUTER JOIN tickets t
                           ON UPPER(t.jira_ticket_id) = UPPER(c.ticket_key)
                    WHERE t.jira_payload->>'github_repo'     ILIKE %s
                       OR t.jira_payload->>'github_repo_url' ILIKE %s
                       OR c.description ILIKE %s
                       OR c.summary     ILIKE %s
                    ORDER BY COALESCE(c.updated_at, t.created_at) DESC NULLS LAST
                    LIMIT 200
                    """,
                    (like, like, like, like),
                ).fetchall()
        except Exception as exc:
            log.warning("Duplicate candidate lookup failed: %s", exc)
            return []

        out: list[_Candidate] = []
        for row in rows or []:
            key = str(row.get("key") or "").strip()
            if not key:
                continue

            summary = str(row.get("summary") or "")
            description = str(row.get("description") or "")
            repo = str(row.get("payload_repo") or "")
            if not repo:
                found = _REPO_URL_RE.search(f"{summary} {description}")
                if found:
                    repo = f"https://github.com/{found.group(1)}/{found.group(2)}"
                else:
                    # The ILIKE matched the bare name somewhere in the text. Treat that
                    # as this repository rather than discarding an otherwise valid match.
                    repo = repo_key

            if not repos_match(repo, repo_key):
                continue

            out.append(
                _Candidate(
                    key=key,
                    summary=summary[:400],
                    description=description[:1200],
                    status=str(row.get("status") or ""),
                    repo=repo,
                )
            )
        return out

    def _candidates_from_vectors(
        self, repo_key: str, summary: str, description: str
    ) -> list[_Candidate]:
        """Widen recall via semantic search when the vector stack is reachable.

        Purely additive. Every hit still passes the repository gate, and the
        verdict is made by the LLM regardless, so an unavailable Qdrant or Ollama
        changes recall only - never the decision.
        """
        try:
            from app.similar_ticket_finder import SimilarTicketFinder

            finder = SimilarTicketFinder(self.settings)
            result = finder.find_similar(summary, description) or {}
            hits = result.get("tickets") or []
        except Exception as exc:
            log.debug("Vector candidate retrieval unavailable (%s); using Postgres candidates only", exc)
            return []

        # Search hits carry no repository field, so the repo gate cannot be applied
        # to them directly. Resolve each hit's repo from the tickets table and drop
        # anything that does not provably belong to this repository.
        by_key: dict[str, dict[str, Any]] = {}
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            key = str(hit.get("ticket_key") or hit.get("key") or "").strip()
            if key:
                by_key[key.upper()] = hit
        if not by_key:
            return []

        repos = self._repos_for_keys(list(by_key.keys()))
        out: list[_Candidate] = []
        for key_upper, hit in by_key.items():
            repo = repos.get(key_upper, "")
            if not repos_match(repo, repo_key):
                continue
            out.append(
                _Candidate(
                    key=str(hit.get("ticket_key") or hit.get("key") or "").strip(),
                    summary=str(hit.get("summary") or "")[:400],
                    description=str(hit.get("description") or "")[:1200],
                    status=str(hit.get("status") or ""),
                    repo=repo,
                )
            )
        return out

    def _repos_for_keys(self, keys: list[str]) -> dict[str, str]:
        """Map upper-cased ticket keys to the repository recorded on each ticket."""
        if not self.settings.database_url or not keys:
            return {}
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.settings.database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    """
                    SELECT UPPER(jira_ticket_id) AS key,
                           COALESCE(
                               jira_payload->>'github_repo',
                               jira_payload->>'github_repo_url',
                               ''
                           ) AS repo
                    FROM tickets
                    WHERE UPPER(jira_ticket_id) = ANY(%s)
                    """,
                    (keys,),
                ).fetchall()
            return {str(r["key"]): str(r.get("repo") or "") for r in rows or []}
        except Exception as exc:
            log.debug("Could not resolve repos for vector hits: %s", exc)
            return {}

    # ── adjudication ──────────────────────────────────────────────────────────

    def _adjudicate(
        self, summary: str, description: str, candidates: list[_Candidate]
    ) -> dict[str, Any] | None:
        """Ask the LLM whether any candidate is the same underlying issue.

        Returns None when the call or parse fails, which the caller treats as
        "unknown" rather than "not a duplicate".
        """
        from app.llm_client import build_llm_client

        listing = "\n\n".join(
            f"[{c.key}] (status: {c.status or 'unknown'})\n"
            f"Summary: {c.summary}\n"
            f"Description: {c.description}"
            for c in candidates
        )

        system_prompt = (
            "You decide whether a new engineering ticket duplicates an existing one in the "
            "same repository.\n\n"
            "A duplicate means the SAME UNDERLYING PROBLEM OR WORK, however differently it is "
            "worded. Judge intent and outcome, never vocabulary overlap.\n\n"
            "Treat as duplicates:\n"
            "- The same defect described from different angles (\"login hangs\" vs \"auth session "
            "expires immediately\" for the same broken session handling).\n"
            "- The same work re-raised after being closed or rejected.\n"
            "- A narrower restatement of work an existing ticket already covers.\n\n"
            "Do NOT treat as duplicates:\n"
            "- Different problems that touch the same file, module or feature area.\n"
            "- Tickets that merely share vocabulary, component names or boilerplate.\n"
            "- A genuinely distinct follow-up to an existing ticket.\n"
            "- Different stages of the same feature (implement vs test vs document).\n\n"
            "Confidence calibration:\n"
            "- 0.9-1.0: unmistakably the same issue.\n"
            "- 0.7-0.89: very likely the same, minor scope differences.\n"
            "- 0.4-0.69: plausibly related, a human should decide.\n"
            "- below 0.4: distinct work.\n\n"
            "Return ONLY raw JSON, no code fences and no commentary:\n"
            '{"duplicate_of": "TICKET-KEY or null", "confidence": 0.0, '
            '"reasoning": "one or two sentences on why it is or is not the same underlying issue"}\n'
            "Set duplicate_of to null when nothing matches. Never use emojis."
        )
        user_message = (
            f"NEW TICKET UNDER REVIEW\n"
            f"Summary: {summary}\n"
            f"Description: {description}\n\n"
            f"EXISTING TICKETS IN THE SAME REPOSITORY\n{listing}"
        )

        try:
            client = build_llm_client(self.settings)
            raw = client.complete(
                system_prompt=system_prompt, user_message=user_message, max_tokens=500
            ).strip()
        except Exception as exc:
            log.warning("Duplicate adjudication LLM call failed: %s", exc)
            return None

        cleaned = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                log.warning("Duplicate adjudication returned unparseable output: %s", raw[:200])
                return None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                log.warning("Duplicate adjudication returned unparseable output: %s", raw[:200])
                return None

        return parsed if isinstance(parsed, dict) else None
