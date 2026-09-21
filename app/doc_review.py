"""Review of PRD / Tech-design docs linked in a Jira ticket — with change
detection (MoM follow-up).

Behaviour:
  * Extract labelled links ("PRD: <url>", "TechDoc: <url>") from the ticket.
  * For each link, fetch the document text and hash it (sha256).
  * Compare against the last-stored hash in `doc_reviews (jira_ticket_id, doc_url)`:
      - hash unchanged AND a prior review exists  -> SKIP the LLM, reuse the
        stored review (so the consolidated comment stays complete).
      - hash changed / new                        -> re-review with the LLM.
  * Only re-post the Jira comment if at least one document actually changed.
  * Persist the new hash + review + comment id per (ticket, doc_url) via upsert.

This means ordinary ticket edits (status, assignee, labels, etc.) no longer
trigger a re-review — only a real change to the linked document does.

Requires the `content_hash` column + `(jira_ticket_id, doc_url)` unique key from
`migrations/2026_doc_reviews_content_hash.sql`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_TIMEOUT = 30
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── Google credentials for reading org-restricted PRD/TechDoc Google Docs ─────
# Docs shared only to the organisation are invisible to the public export URL.
# Two authenticated paths are supported, tried in this order:
#
#   1. OAuth user credentials (PRIMARY). An authorised org user grants the app
#      Drive read access once (see scripts/google_oauth_setup.py); the resulting
#      offline *refresh token* lets Workflow 6 read exactly what that user can,
#      non-interactively. Use this when the org docs are not reachable via a
#      service account (no domain-wide delegation, or the SA simply can't see
#      them — which is the case here).
#        GOOGLE_OAUTH_CLIENT_ID      OAuth 2.0 client id     (Google Cloud console)
#        GOOGLE_OAUTH_CLIENT_SECRET  OAuth 2.0 client secret
#        GOOGLE_OAUTH_REFRESH_TOKEN  the offline refresh token, OR set
#        GOOGLE_OAUTH_TOKEN_FILE     path to a file holding it (bare token or a
#                                    JSON blob with a "refresh_token" field).
#
#   2. Service account + domain-wide delegation (FALLBACK). A service account
#      that *impersonates* an org user. Requires Workspace admin to authorise the
#      SA's client id for the Drive scope.
#        GOOGLE_SA_CREDENTIALS_JSON  path to the SA key file, or the JSON inline.
#        GOOGLE_IMPERSONATE_USER     org mailbox the SA acts as, e.g. bot@acme.com.
#
# If neither is configured the public-export URL is used (works only for docs
# shared "anyone with the link").
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
GOOGLE_OAUTH_TOKEN_FILE = os.environ.get("GOOGLE_OAUTH_TOKEN_FILE", "")
GOOGLE_OAUTH_TOKEN_URI = os.environ.get(
    "GOOGLE_OAUTH_TOKEN_URI", "https://oauth2.googleapis.com/token"
)

GOOGLE_SA_CREDENTIALS_JSON = os.environ.get("GOOGLE_SA_CREDENTIALS_JSON", "")
GOOGLE_IMPERSONATE_USER = os.environ.get("GOOGLE_IMPERSONATE_USER", "")
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

COMMENT_MARKER = "AI-GOVERNOR-DOCREVIEW-V1"

LINK_PATTERN = re.compile(
    r"(?P<label>PRD|Tech\s*Doc|Technical\s*Design(?:\s*Doc)?|Design\s*Doc)"
    r"\s*[:\-]\s*(?P<url>https?://\S+)",
    re.IGNORECASE,
)

# Keyword used to recognise a PRD/TechDoc link when it is rendered as hyperlinked
# text (anchor text or the text immediately preceding the link), e.g. the word
# "TechDoc" linking to a URL rather than a raw "TechDoc: https://…" string.
LABEL_HINT = re.compile(
    r"(PRD|Tech\s*Doc|Technical\s*Design(?:\s*Doc)?|Design\s*Doc)",
    re.IGNORECASE,
)


def _normalise_label(raw_label: str) -> str:
    raw = raw_label.lower().replace(" ", "")
    return "PRD" if raw.startswith("prd") else "TechDoc"

REVIEW_SYSTEM_PROMPT = (
    "You are a senior engineering reviewer at a fintech company. You are given the "
    "text of a product or technical design document linked from a Jira ticket. "
    "Review it concisely and concretely. Cover: completeness, clarity, missing "
    "edge cases / NFRs, security & compliance gaps, testability, and any "
    "ambiguous requirements. Output Jira wiki markup with these sections: "
    "*Summary* (2-3 lines), *Strengths* (bullets), *Gaps & Risks* (bullets, most "
    "important first), *Recommended changes* (numbered, actionable). Do not invent "
    "facts not present in the document; if a critical section is absent, say so."
)


@dataclass
class DocLink:
    label: str
    url: str
    text: str = ""
    error: str = ""
    content_hash: str = ""
    review: str = ""
    changed: bool = False          # content differs from last stored hash
    reviewed_now: bool = False     # LLM was actually called this run


@dataclass
class CommentPostResult:
    comment_id: str | None = None
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Link extraction
# ─────────────────────────────────────────────────────────────────────────────
def extract_doc_links(description: Any) -> list[DocLink]:
    """Collect PRD/TechDoc links in either form, deduplicated by URL:

      * raw link    — "PRD: https://…" / "TechDoc - https://…" in plain text,
                      including Jira wiki-markup / smart links written as
                      "Tech Doc : [<url>|<url>|smart-link]";
      * hyperlinked  — the word "TechDoc" (or "PRD:" before it) linking to a URL,
                      stored in Jira ADF as a `link` mark or an
                      `inlineCard`/`blockCard` smart-link node.
    """
    links: list[DocLink] = []
    seen: set[str] = set()

    def add(label: str, url: str) -> None:
        url = url.rstrip(").,;>]")
        if not url or url in seen:
            return
        seen.add(url)
        links.append(DocLink(label=label, url=url))

    # 1. Hyperlinked text / cards — read link marks + smart-link cards out of ADF.
    for label, url in _adf_hyperlinks(description):
        add(label, url)

    # 2. Raw links — regex over the flattened text, after unwrapping Jira wiki
    #    markup links so a "Tech Doc : [<url>|…|smart-link]" still matches (the
    #    raw pattern expects the URL right after the label, not a "[").
    flat = _unwrap_wiki_links(_description_to_text(description))
    for m in LINK_PATTERN.finditer(flat):
        add(_normalise_label(m.group("label")), m.group("url"))

    return links


def _unwrap_wiki_links(text: str) -> str:
    """Replace Jira wiki-markup links — ``[visible|url]``, ``[visible|url|smart-link]``
    or ``[url]`` — with the bare target URL, so a "Tech Doc : [<url>]" smart link
    matches the raw LINK_PATTERN (which expects the URL right after the label)."""
    if "[" not in text:
        return text

    def repl(m: re.Match) -> str:
        for part in m.group(1).split("|"):
            part = part.strip()
            if part.startswith(("http://", "https://")):
                return f" {part} "
        return m.group(0)

    return re.sub(r"\[([^\[\]]+)\]", repl, text)


def _adf_hyperlinks(description: Any) -> list[tuple[str, str]]:
    """Walk an ADF description and return ``(label, href)`` for each link whose
    anchor text — or the text token immediately before it — names a PRD/TechDoc.

    Unlabelled hyperlinks (e.g. a bare "click here" link with no PRD/TechDoc cue
    nearby) are intentionally ignored: there is no reliable signal they point to a
    reviewable document.
    """
    if not isinstance(description, (dict, list)):
        return []

    # Flatten to ordered (text, href) inline tokens so the label-proximity rule
    # is independent of how deeply the link is nested.
    tokens: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ntype = node.get("type")
            if ntype == "text":
                href = ""
                for mark in node.get("marks", []) or []:
                    if mark.get("type") == "link":
                        href = (mark.get("attrs") or {}).get("href", "") or href
                tokens.append((node.get("text", ""), href))
            elif ntype in ("inlineCard", "blockCard"):
                # Jira smart-links render as a card node with no text; the URL is
                # on attrs.url (or attrs.data.url). Emit a textless href token so
                # the preceding "Tech Doc :" label still tags it.
                attrs = node.get("attrs") or {}
                url = attrs.get("url") or (attrs.get("data") or {}).get("url", "")
                if url:
                    tokens.append(("", url))
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(description)

    results: list[tuple[str, str]] = []
    prev_text = ""
    for text, href in tokens:
        if href:
            source = text if LABEL_HINT.search(text) else prev_text
            m = LABEL_HINT.search(source)
            if m:
                results.append((_normalise_label(m.group(1)), href))
        if text.strip():
            prev_text = text
    return results


def _description_to_text(description: Any) -> str:
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        parts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text":
                    parts.append(node.get("text", ""))
                for mark in node.get("marks", []) or []:
                    href = (mark.get("attrs") or {}).get("href")
                    if href:
                        parts.append(href)
                for child in node.get("content", []) or []:
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(description)
        return "\n".join(parts)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Document fetch + hashing
# ─────────────────────────────────────────────────────────────────────────────
def _gdoc_id(url: str) -> str:
    m = re.search(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else ""


def _normalise_gdoc(url: str) -> str:
    doc_id = _gdoc_id(url)
    if doc_id:
        return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    return url


# Lazy Drive client singleton. ``_drive_unavailable`` latches once we know no
# authenticated path is configured / the libs are missing, so we don't retry the
# (failing) import on every link.
_drive_service: Any = None
_drive_unavailable = False
_FALLBACK = "__fallback__"  # sentinel: API path opted out, use public export


def _oauth_refresh_token() -> str:
    """Resolve the OAuth refresh token from the inline env var or token file."""
    if GOOGLE_OAUTH_REFRESH_TOKEN:
        return GOOGLE_OAUTH_REFRESH_TOKEN.strip()
    if GOOGLE_OAUTH_TOKEN_FILE and os.path.isfile(GOOGLE_OAUTH_TOKEN_FILE):
        try:
            with open(GOOGLE_OAUTH_TOKEN_FILE, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            log.exception("could not read GOOGLE_OAUTH_TOKEN_FILE %s", GOOGLE_OAUTH_TOKEN_FILE)
            return ""
        # Accept either a bare token or a JSON blob with a "refresh_token" field.
        try:
            return str(json.loads(raw).get("refresh_token", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            return raw
    return ""


def _build_oauth_drive() -> Any:
    """Build a Drive client from stored OAuth user credentials, or return None if
    the OAuth path is not fully configured. google-auth refreshes the short-lived
    access token automatically from the refresh token on each request."""
    refresh_token = _oauth_refresh_token()
    if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and refresh_token):
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri=GOOGLE_OAUTH_TOKEN_URI,
        scopes=GOOGLE_SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _build_sa_drive() -> Any:
    """Build a Drive client from a service account with domain-wide delegation,
    or return None if the SA path is not configured."""
    if not GOOGLE_SA_CREDENTIALS_JSON or not GOOGLE_IMPERSONATE_USER:
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if os.path.isfile(GOOGLE_SA_CREDENTIALS_JSON):
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SA_CREDENTIALS_JSON, scopes=GOOGLE_SCOPES
        )
    else:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_SA_CREDENTIALS_JSON), scopes=GOOGLE_SCOPES
        )
    creds = creds.with_subject(GOOGLE_IMPERSONATE_USER)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _drive():
    """Cached Drive client, preferring OAuth user credentials and falling back to
    a service account. Latches unavailable after the first attempt so we don't
    retry a failing import/config on every link."""
    global _drive_service, _drive_unavailable
    if _drive_service is not None or _drive_unavailable:
        return _drive_service
    try:
        import googleapiclient.discovery  # noqa: F401 - probe the libs are present
    except ImportError:
        log.warning(
            "google-api-python-client/google-auth not installed; "
            "org-restricted Google Docs cannot be read"
        )
        _drive_unavailable = True
        return None
    for name, builder in (("oauth", _build_oauth_drive), ("service account", _build_sa_drive)):
        try:
            service = builder()
        except Exception:  # noqa: BLE001 - never block a review on Google setup
            log.exception("failed to initialise Google Drive client via %s", name)
            continue
        if service is not None:
            log.info("Google Drive client initialised via %s", name)
            _drive_service = service
            return _drive_service
    log.info("no Google Drive credentials configured; using public export fallback")
    _drive_unavailable = True
    return None


def _google_error_reason(exc: Any) -> tuple[str, str]:
    """Best-effort (reason, message) from a googleapiclient HttpError. ``reason``
    is the machine code (e.g. "accessNotConfigured"), ``message`` the human text."""
    content = getattr(exc, "content", None)
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8", "replace")
    if not isinstance(content, str):
        return "", str(getattr(exc, "error_details", "") or exc)
    try:
        err = (json.loads(content) or {}).get("error", {})
    except json.JSONDecodeError:
        return "", content
    reason = ""
    errors = err.get("errors")
    if isinstance(errors, list) and errors:
        reason = (errors[0] or {}).get("reason", "") or ""
    if not reason and isinstance(err.get("status"), str):
        reason = err["status"]  # e.g. "PERMISSION_DENIED"
    return reason, err.get("message", "") or ""


def _fetch_gdoc_via_api(doc_id: str, limit_chars: int) -> tuple[str, str]:
    """Read a Google Doc as text/plain via the Drive API (impersonated SA).

    Returns ``(text, "")`` on success, ``(text, _FALLBACK)`` to defer to the
    public-export path, or ``(text, error)`` for a real access failure.
    """
    service = _drive()
    if service is None:
        return "", _FALLBACK
    try:
        data = service.files().export(fileId=doc_id, mimeType="text/plain").execute()
    except Exception as exc:  # noqa: BLE001 - googleapiclient HttpError + transport
        status = getattr(getattr(exc, "resp", None), "status", None)
        reason, message = _google_error_reason(exc)
        # "accessNotConfigured" / "apiNotActivated" means the Drive API is not
        # enabled in the OAuth client's Cloud project — a server config problem,
        # NOT a per-document sharing issue. Don't block the doc on it: log loudly
        # and defer to the public-export fallback so public docs still work.
        if reason in ("accessNotConfigured", "apiNotActivated") or "has not been used in project" in message:
            log.error(
                "Google Drive API is not enabled for the OAuth client's project — "
                "enable it at https://console.cloud.google.com/apis/library/drive.googleapis.com "
                "for the project owning GOOGLE_OAUTH_CLIENT_ID. Detail: %s",
                message or exc,
            )
            return "", _FALLBACK
        if status in (401, 403):
            account = GOOGLE_IMPERSONATE_USER or "the authorised OAuth user"
            return "", (
                f"document is not shared with the review account ({account})"
            )
        if status == 404:
            return "", "document not found or not accessible to the review account"
        log.warning("Drive export failed for %s: %s", doc_id, exc)
        return "", _FALLBACK
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    text = text.strip()[:limit_chars]
    if not re.search(r"[A-Za-z0-9]", text):
        return "", "document is empty or has no reviewable text"
    return text, ""


def fetch_doc_text(url: str, limit_chars: int = 60_000) -> tuple[str, str]:
    # Prefer the authenticated Drive API for Google Docs so org-restricted docs
    # are readable; fall back to the public export only when the API opts out.
    doc_id = _gdoc_id(url)
    if doc_id:
        text, err = _fetch_gdoc_via_api(doc_id, limit_chars)
        if err != _FALLBACK:
            return text, err
    fetch_url = _normalise_gdoc(url)
    try:
        resp = requests.get(fetch_url, timeout=JIRA_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return "", f"fetch failed: {exc}"
    if resp.status_code in (401, 403):
        return "", "document is not publicly accessible (auth required)"
    if resp.status_code >= 400:
        return "", f"fetch failed: HTTP {resp.status_code}"
    ctype = resp.headers.get("Content-Type", "")
    if "html" in ctype and "google.com" in fetch_url:
        return "", "document is not publicly accessible (auth required)"
    text = resp.text
    if "html" in ctype:
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    text = text.strip()[:limit_chars]
    if not re.search(r"[A-Za-z0-9]", text):
        return "", "document is empty or has no reviewable text"
    return text, ""


def _hash_text(text: str) -> str:
    # Normalise whitespace so trivial reflow doesn't read as a change.
    normalised = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# doc_reviews persistence (change-detection cache)
# ─────────────────────────────────────────────────────────────────────────────
def _connect():
    import psycopg  # local import: module is usable without a DB present

    return psycopg.connect(DATABASE_URL)


def ensure_doc_reviews_schema() -> None:
    """Idempotent guard so the app can self-heal if the migration didn't run."""
    if not DATABASE_URL:
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE doc_reviews ADD COLUMN IF NOT EXISTS content_hash text")
            cur.execute("ALTER TABLE doc_reviews ADD COLUMN IF NOT EXISTS review_count integer NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE doc_reviews ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT now()")
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname = 'doc_reviews_ticket_url_key') THEN "
                "ALTER TABLE doc_reviews ADD CONSTRAINT doc_reviews_ticket_url_key "
                "UNIQUE (jira_ticket_id, doc_url); END IF; END $$;"
            )
            conn.commit()
    except Exception:  # noqa: BLE001 - never block a review on schema self-heal
        log.exception("ensure_doc_reviews_schema failed; run the SQL migration manually")


def _load_existing(ticket: str, urls: list[str]) -> dict[str, dict[str, Any]]:
    """url -> {content_hash, llm_review, comment_id} for prior reviews."""
    if not DATABASE_URL or not urls:
        return {}
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT doc_url, content_hash, llm_review, comment_id "
                "FROM doc_reviews WHERE jira_ticket_id = %s AND doc_url = ANY(%s)",
                (ticket, urls),
            )
            return {
                row[0]: {"content_hash": row[1], "llm_review": row[2], "comment_id": row[3]}
                for row in cur.fetchall()
            }
    except Exception:  # noqa: BLE001
        log.exception("_load_existing failed")
        return {}


def _upsert_review(ticket: str, link: DocLink, comment_id: str | None) -> None:
    if not DATABASE_URL:
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO doc_reviews
                    (jira_ticket_id, doc_url, doc_type, llm_review, comment_id,
                     content_hash, review_count, scanned_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 1, now(), now())
                ON CONFLICT (jira_ticket_id, doc_url) DO UPDATE SET
                    doc_type     = EXCLUDED.doc_type,
                    llm_review   = EXCLUDED.llm_review,
                    comment_id   = EXCLUDED.comment_id,
                    content_hash = EXCLUDED.content_hash,
                    review_count = doc_reviews.review_count + 1,
                    scanned_at   = now(),
                    updated_at   = now()
                """,
                (ticket, link.url, link.label, link.review, comment_id, link.content_hash),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("_upsert_review failed for %s %s", ticket, link.url)


def _touch_scanned(ticket: str, url: str) -> None:
    """Mark an unchanged doc as checked-now without bumping review_count."""
    if not DATABASE_URL:
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE doc_reviews SET scanned_at = now() "
                "WHERE jira_ticket_id = %s AND doc_url = %s",
                (ticket, url),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("_touch_scanned failed")


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer
# ─────────────────────────────────────────────────────────────────────────────
class DocReviewer:
    def __init__(self, settings: Any = None, prompt_store: Any = None, llm_client: Any = None):
        self._llm = llm_client
        self._settings = settings

    def _llm_complete(self, system: str, user: str) -> str:
        if self._llm is not None:
            return self._llm.complete(system, user).strip()
        from app.config import settings as app_settings
        from app.llm_client import build_llm_client

        client = build_llm_client(self._settings or app_settings)
        return client.complete(system, user).strip()

    def review(self, issue_key: str, description: Any) -> dict[str, Any]:
        ensure_doc_reviews_schema()
        links = extract_doc_links(description)
        if not links:
            return {"issueKey": issue_key, "reviewed": 0, "unchanged": 0,
                    "commentPosted": False, "reason": "no PRD/TechDoc links found"}

        existing = _load_existing(issue_key, [l.url for l in links])

        # 1. Fetch + classify each link as changed / unchanged / error.
        for link in links:
            link.text, link.error = fetch_doc_text(link.url)
            if link.error:
                continue  # leave cache untouched; surface the error in the comment
            link.content_hash = _hash_text(link.text)
            prior = existing.get(link.url)
            if prior and prior.get("content_hash") == link.content_hash and prior.get("llm_review"):
                # Unchanged: reuse the stored review, no LLM call.
                link.changed = False
                link.review = prior["llm_review"]
                _touch_scanned(issue_key, link.url)
            else:
                link.changed = True  # new doc or content differs -> review below

        changed = [l for l in links if l.changed]
        errored = [l for l in links if l.error]

        # 2. If nothing changed, skip the LLM and the comment entirely.
        if not changed:
            reason = (
                "no reviewable PRD/TechDoc content found"
                if errored and len(errored) == len(links)
                else "no document changes since last review"
            )
            return {
                "issueKey": issue_key,
                "reviewed": 0,
                "unchanged": len([l for l in links if not l.error and not l.changed]),
                "skipped": [{"url": l.url, "error": l.error} for l in errored],
                "commentPosted": False,
                "reason": reason,
            }

        # 3. Review only the changed docs.
        for link in changed:
            user_prompt = (
                f"Ticket: {issue_key}\nDocument type: {link.label}\nSource URL: {link.url}\n\n"
                f"--- DOCUMENT TEXT START ---\n{link.text}\n--- DOCUMENT TEXT END ---"
            )
            link.review = self._llm_complete(REVIEW_SYSTEM_PROMPT, user_prompt)
            link.reviewed_now = True

        # 4. Rebuild the consolidated comment from ALL links (fresh + cached).
        blocks: list[str] = []
        for link in links:
            if link.error:
                blocks.append(
                    f"h4. {link.label} review — [{link.label}|{link.url}]\n"
                    f"{{panel:bgColor=#FFEBE6}}Could not review automatically: {link.error}.{{panel}}"
                )
            else:
                tag = " _(updated)_" if link.reviewed_now else " _(unchanged)_"
                blocks.append(f"h4. {link.label} review{tag} — [{link.label}|{link.url}]\n{link.review}")

        comment_body = (
            f"h3. 📝 AI Governor — Document Review ({len(links)})\n"
            f"_Automated review of PRD / Tech-design docs linked on {issue_key}._\n"
            f"{{anchor:{COMMENT_MARKER}}}\n\n" + "\n\n----\n\n".join(blocks)
        )
        comment_result = _upsert_comment(issue_key, comment_body)

        # 5. Persist new hash + review for the changed docs.
        for link in changed:
            _upsert_review(issue_key, link, comment_result.comment_id)

        return {
            "issueKey": issue_key,
            "reviewed": len(changed),
            "unchanged": len([l for l in links if not l.error and not l.changed]),
            "skipped": [{"url": l.url, "error": l.error} for l in errored],
            "commentPosted": bool(comment_result.comment_id),
            "reason": comment_result.error,
        }


def _upsert_comment(issue_key: str, body: str) -> CommentPostResult:
    """Create or update the marker-anchored review comment; return its id."""
    if not JIRA_BASE_URL or not JIRA_EMAIL or not JIRA_API_TOKEN:
        return CommentPostResult(error="Jira credentials are not configured")

    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    base = f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment"
    existing_id = None
    try:
        listing = requests.get(f"{base}?maxResults=100", auth=auth,
                               headers={"Accept": "application/json"}, timeout=JIRA_TIMEOUT)
        if listing.status_code >= 400:
            detail = _jira_error_detail(listing)
            log.warning(
                "Jira comment lookup failed for %s: HTTP %s %s",
                issue_key,
                listing.status_code,
                detail,
            )
            return CommentPostResult(
                error=f"Jira comment lookup failed: HTTP {listing.status_code}: {detail}"
            )

        for c in listing.json().get("comments", []):
            if COMMENT_MARKER in (c.get("body") or ""):
                existing_id = c.get("id")
                break

        method = requests.put if existing_id else requests.post
        url = f"{base}/{existing_id}" if existing_id else base
        resp = method(url, auth=auth,
                      headers={"Accept": "application/json", "Content-Type": "application/json"},
                      json={"body": body}, timeout=JIRA_TIMEOUT)
        if resp.status_code < 400:
            comment_id = (resp.json().get("id") if resp.content else None) or existing_id
            if comment_id:
                return CommentPostResult(comment_id=comment_id)
            return CommentPostResult(error="Jira accepted the comment request but returned no comment id")

        detail = _jira_error_detail(resp)
        log.warning(
            "Jira comment upsert failed for %s: HTTP %s %s",
            issue_key,
            resp.status_code,
            detail,
        )
        return CommentPostResult(
            comment_id=existing_id,
            error=f"Jira comment upsert failed: HTTP {resp.status_code}: {detail}",
        )
    except requests.RequestException as exc:
        log.warning("Jira comment upsert request failed for %s: %s", issue_key, exc)
        return CommentPostResult(comment_id=existing_id, error=f"Jira comment request failed: {exc}")


def _jira_error_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(data, dict):
        messages = data.get("errorMessages")
        if isinstance(messages, list) and messages:
            return "; ".join(str(msg) for msg in messages)
        errors = data.get("errors")
        if errors:
            return str(errors)
    return resp.text[:500]
