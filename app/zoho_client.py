"""Zoho Desk REST client — customer lookup + ticket visibility.

This module powers the "Zoho Tickets" admin dashboard tab (capability key
``zoho``). Given a customer's email or phone, it resolves the matching Zoho Desk
contact and lists that contact's tickets, so an operator can validate customer
ticket visibility before the same APIs are wired into the customer portal.

The OAuth refresh token must be granted the scopes
``Desk.tickets.READ,Desk.contacts.READ,Desk.search.READ`` — the ``/search``
endpoints below need ``Desk.search.READ`` specifically, and omitting it yields a
403 ``SCOPE_MISMATCH``. Regenerate the token with ``scripts/zoho_oauth_setup.py``.

It wraps these Zoho Desk API touch-points described in the design doc:

1. OAuth token refresh (``/oauth/v2/token``) — an in-memory access token cached
   until shortly before it expires, refreshed from the long-lived refresh token.
2. Contact search (``/api/v1/contacts/search``) — map customer ↔ Zoho contact.
3. Contact tickets (``/api/v1/contacts/{id}/tickets``) — the customer's tickets.
4. (fallback) ticket search by email (``/api/v1/tickets/search``) when no
   contact record exists but tickets were raised with that email.
5. Ticket detail (``/api/v1/tickets/{id}``) plus its conversation threads
   (``/api/v1/tickets/{id}/threads`` and ``.../threads/{threadId}``) — backs the
   detail view opened by clicking a ticket row.

Everything is stateless apart from the cached access token. When credentials are
not configured the client reports ``is_configured() == False`` and the API layer
returns a friendly "not configured" response instead of raising.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any, Optional

import requests
from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)

# Fields we ask Zoho to return for each ticket, and expose to the UI.
_TICKET_FIELDS = ["id", "ticketNumber", "subject", "status", "priority", "createdTime", "modifiedTime"]

# Refresh the access token this many seconds before its stated expiry to avoid
# racing the boundary on a slow request.
_TOKEN_SKEW_SECONDS = 120

# Zoho's thread list carries only summaries; the body of each message needs a
# separate GET per thread. Cap how many we expand so a long-running ticket can't
# turn one detail view into hundreds of upstream calls.
_MAX_THREADS = 25


class ZohoError(Exception):
    """Raised for configuration or upstream Zoho Desk failures."""


class ZohoTicket(BaseModel):
    # Snake_case field names ARE the API contract: _ticket_from_row maps the raw
    # Zoho camelCase keys in explicitly, and the React tab reads these names. (No
    # aliases — FastAPI serializes response models by alias by default, which
    # would otherwise emit createdTime/ticketNumber and blank the UI columns.)
    id: str
    ticket_number: Optional[str] = None
    subject: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    created_time: Optional[str] = None
    modified_time: Optional[str] = None


class ZohoContact(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class ZohoTicketDetail(ZohoTicket):
    """A ticket plus the descriptive fields only the single-ticket GET returns."""

    description: Optional[str] = None
    channel: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    assignee: Optional[str] = None
    due_date: Optional[str] = None
    web_url: Optional[str] = None


class ZohoThread(BaseModel):
    """One message in a ticket's conversation.

    ``content`` is plain text: Zoho returns customer-authored HTML, which we
    never hand to the browser as markup (see _html_to_text).
    """

    id: str
    channel: Optional[str] = None
    direction: Optional[str] = None  # "in" (from customer) | "out" (agent reply)
    author: Optional[str] = None
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    created_time: Optional[str] = None
    has_attachment: bool = False


class TicketDetailResponse(BaseModel):
    configured: bool
    ticket: Optional[ZohoTicketDetail] = None
    threads: list[ZohoThread] = Field(default_factory=list)
    threads_truncated: bool = False
    message: Optional[str] = None


class CustomerTicketsRequest(BaseModel):
    """Lookup by email and/or phone — at least one must be provided."""

    email: Optional[str] = None
    phone: Optional[str] = None


class CustomerTicketsResponse(BaseModel):
    configured: bool
    contact: Optional[ZohoContact] = None
    tickets: list[ZohoTicket] = Field(default_factory=list)
    message: Optional[str] = None


class ZohoClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()

    # ── Configuration ────────────────────────────────────────────────────────
    def is_configured(self) -> bool:
        s = self.settings
        return bool(s.zoho_client_id and s.zoho_client_secret and s.zoho_refresh_token and s.zoho_org_id)

    # ── OAuth ────────────────────────────────────────────────────────────────
    def _access_token(self) -> str:
        """Return a valid access token, refreshing it if missing/near expiry."""
        with self._lock:
            if self._token and time.time() < self._token_expiry - _TOKEN_SKEW_SECONDS:
                return self._token

            s = self.settings
            log.info("Refreshing Zoho access token")
            resp = requests.post(
                f"{s.zoho_accounts_base}/oauth/v2/token",
                params={
                    "refresh_token": s.zoho_refresh_token,
                    "client_id": s.zoho_client_id,
                    "client_secret": s.zoho_client_secret,
                    "grant_type": "refresh_token",
                },
                timeout=self.settings.external_request_timeout_seconds,
            )
            if resp.status_code != 200:
                raise ZohoError(f"Zoho token refresh failed ({resp.status_code}): {resp.text[:200]}")
            data = resp.json()
            token = data.get("access_token")
            if not token:
                # Zoho returns 200 with an "error" key on invalid refresh tokens.
                raise ZohoError(f"Zoho token refresh returned no access_token: {data.get('error', data)}")
            self._token = token
            self._token_expiry = time.time() + int(data.get("expires_in", 3600))
            return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Zoho-oauthtoken {self._access_token()}",
            "orgId": self.settings.zoho_org_id,
        }

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        url = f"{self.settings.zoho_desk_base}{path}"
        resp = requests.get(
            url,
            headers=self._headers(),
            params=params or {},
            timeout=self.settings.external_request_timeout_seconds,
        )
        # 204/404 → treat as "no data" rather than an error (Zoho uses 204 for
        # empty search results on some endpoints).
        if resp.status_code in (204, 404):
            return {}
        if resp.status_code != 200:
            raise ZohoError(f"Zoho GET {path} failed ({resp.status_code}): {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            return {}

    # ── Domain calls ─────────────────────────────────────────────────────────
    def search_contact(self, email: Optional[str] = None, phone: Optional[str] = None) -> Optional[ZohoContact]:
        """Resolve a customer to a Zoho Desk contact by email, then phone."""
        params: dict[str, Any] = {}
        if email:
            params["email"] = email.strip()
        elif phone:
            params["phone"] = phone.strip()
        else:
            return None

        data = self._get("/api/v1/contacts/search", params=params)
        rows = data.get("data") or []
        if not rows:
            return None
        row = rows[0]
        return ZohoContact(
            id=str(row.get("id")),
            name=(f"{row.get('firstName', '') or ''} {row.get('lastName', '') or ''}".strip() or row.get("email")),
            email=row.get("email"),
            phone=row.get("phone") or row.get("mobile"),
        )

    def tickets_for_contact(self, contact_id: str, limit: int = 50) -> list[ZohoTicket]:
        data = self._get(
            f"/api/v1/contacts/{contact_id}/tickets",
            params={"limit": limit, "sortBy": "-createdTime"},
        )
        return [_ticket_from_row(r) for r in (data.get("data") or [])]

    def tickets_by_email(self, email: str, limit: int = 50) -> list[ZohoTicket]:
        """Fallback: search tickets directly by requester email."""
        data = self._get("/api/v1/tickets/search", params={"email": email.strip(), "limit": limit})
        return [_ticket_from_row(r) for r in (data.get("data") or [])]

    def ticket_detail(self, ticket_id: str) -> TicketDetailResponse:
        """Full flow for one ticket: its fields + expanded conversation threads."""
        if not self.is_configured():
            return TicketDetailResponse(
                configured=False,
                message="Zoho Desk is not configured. Set ZOHO_CLIENT_ID, "
                "ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN and ZOHO_ORG_ID.",
            )

        row = self._get(f"/api/v1/tickets/{ticket_id}", params={"include": "contacts,assignee"})
        if not row.get("id"):
            return TicketDetailResponse(configured=True, message="Ticket not found in Zoho Desk.")

        threads, truncated = self._ticket_threads(ticket_id)
        return TicketDetailResponse(
            configured=True,
            ticket=_ticket_detail_from_row(row),
            threads=threads,
            threads_truncated=truncated,
            message=None if threads else "No conversation threads on this ticket yet.",
        )

    def _ticket_threads(self, ticket_id: str) -> tuple[list[ZohoThread], bool]:
        """List a ticket's threads, then fetch each one's body.

        The list endpoint returns summaries only, so every thread costs a second
        GET. A thread whose detail call fails degrades to its summary rather than
        sinking the whole view.
        """
        data = self._get(f"/api/v1/tickets/{ticket_id}/threads", params={"limit": _MAX_THREADS + 1})
        rows = data.get("data") or []
        truncated = len(rows) > _MAX_THREADS
        if truncated:
            # Keep the most recent ones; Zoho lists threads oldest-first.
            rows = rows[-_MAX_THREADS:]

        threads: list[ZohoThread] = []
        for row in rows:
            thread_id = str(row.get("id") or "")
            if not thread_id:
                continue
            detail: dict[str, Any] = {}
            try:
                detail = self._get(f"/api/v1/tickets/{ticket_id}/threads/{thread_id}")
            except ZohoError as exc:
                log.warning("Zoho thread %s of ticket %s failed to load: %s", thread_id, ticket_id, exc)
            threads.append(_thread_from_row({**row, **detail}))
        return threads, truncated

    def customer_tickets(self, email: Optional[str] = None, phone: Optional[str] = None) -> CustomerTicketsResponse:
        """Full flow: resolve contact → list their tickets (email fallback)."""
        if not self.is_configured():
            return CustomerTicketsResponse(
                configured=False,
                message="Zoho Desk is not configured. Set ZOHO_CLIENT_ID, "
                "ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN and ZOHO_ORG_ID.",
            )
        if not (email or phone):
            raise ZohoError("Provide a customer email or phone number.")

        contact = self.search_contact(email=email, phone=phone)
        if contact:
            tickets = self.tickets_for_contact(contact.id)
            msg = None if tickets else "No tickets found for this customer."
            return CustomerTicketsResponse(configured=True, contact=contact, tickets=tickets, message=msg)

        # No contact record — fall back to a direct ticket search by email.
        if email:
            tickets = self.tickets_by_email(email)
            if tickets:
                return CustomerTicketsResponse(configured=True, contact=None, tickets=tickets, message=None)

        return CustomerTicketsResponse(
            configured=True,
            contact=None,
            tickets=[],
            message="No matching customer or tickets were found.",
        )


def _ticket_from_row(row: dict[str, Any]) -> ZohoTicket:
    return ZohoTicket(
        id=str(row.get("id")),
        ticket_number=row.get("ticketNumber"),
        subject=row.get("subject"),
        status=row.get("status"),
        priority=row.get("priority"),
        created_time=row.get("createdTime"),
        modified_time=row.get("modifiedTime"),
    )


def _ticket_detail_from_row(row: dict[str, Any]) -> ZohoTicketDetail:
    assignee = row.get("assignee") or {}
    department = row.get("department") or {}
    return ZohoTicketDetail(
        **_ticket_from_row(row).model_dump(),
        description=_html_to_text(row.get("description")),
        channel=row.get("channel"),
        email=row.get("email"),
        phone=row.get("phone"),
        department=department.get("name") if isinstance(department, dict) else None,
        assignee=(
            f"{assignee.get('firstName', '') or ''} {assignee.get('lastName', '') or ''}".strip()
            or assignee.get("email")
            if isinstance(assignee, dict)
            else None
        ),
        due_date=row.get("dueDate"),
        web_url=row.get("webUrl"),
    )


def _thread_from_row(row: dict[str, Any]) -> ZohoThread:
    author = row.get("author") or {}
    to_addresses = row.get("to")
    if isinstance(to_addresses, list):
        # Zoho returns either a plain string or a list of {address, name} objects.
        to_addresses = ", ".join(
            (a.get("address") or "") if isinstance(a, dict) else str(a) for a in to_addresses
        ).strip(", ")
    return ZohoThread(
        id=str(row.get("id")),
        channel=row.get("channel"),
        direction=row.get("direction"),
        author=(author.get("name") or author.get("email")) if isinstance(author, dict) else None,
        from_address=row.get("fromEmailAddress") or row.get("from"),
        to_address=to_addresses or None,
        summary=row.get("summary"),
        # plainText is Zoho's own text rendering when present; otherwise flatten
        # the HTML body ourselves.
        content=_html_to_text(row.get("content")) if not row.get("plainText") else row.get("plainText"),
        created_time=row.get("createdTime"),
        has_attachment=bool(row.get("hasAttach")),
    )


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping <script>/<style> bodies entirely."""

    _SKIP = {"script", "style", "head"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _html_to_text(html: Optional[str]) -> Optional[str]:
    """Flatten Zoho's HTML message bodies to plain text.

    Thread bodies are customer-authored HTML arriving over email, so rendering
    them as markup in the admin dashboard would be a stored-XSS vector. The UI
    shows this text verbatim (white-space: pre-wrap) instead.
    """
    if not html:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup — fall back to a blunt tag strip
        log.debug("Falling back to regex tag strip for a Zoho HTML body")
        return re.sub(r"<[^>]+>", " ", html).strip() or None
    text = "".join(parser.parts)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse the runs of blank lines that block-tag breaks leave behind.
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip() or None
