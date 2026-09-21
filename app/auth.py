"""Authentication and per-tab RBAC for the Jira AI admin UI.

This module backs the React SPA login + role-based access control:

* Users live in a Postgres table (``app_users``) with a bcrypt password hash
  and a single ``role`` string.
* Roles are mapped to the set of UI tabs / API capabilities they may use in
  ``ROLE_TABS`` (a "per-tab custom roles" model). ``"*"`` means every tab.
* Login issues a short-lived JWT (PyJWT, HS256). The SPA stores it and sends it
  as ``Authorization: Bearer <token>``.
* Server-to-server automation (n8n, webhooks) can bypass user auth with a shared
  ``X-Service-Key`` header so existing integrations keep working.

The tab keys used across the backend and frontend are:

    repos, jira, insights, logs, testcases, similar, workflows, channels,
    utilization, neo4j, rca, rings, docs, users

``docs`` gates the standalone Documentation portal (its own URL, not a tab).
``rings`` gates the Ring Studio dashboard (diamond-ring image-prompt generator).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Iterable, Optional

import bcrypt
import jwt
import psycopg
from psycopg.rows import dict_row

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from app.config import settings

log = logging.getLogger(__name__)

# ─── Tabs & roles ────────────────────────────────────────────────────────────

# Capability keys. The leading entries are dashboard tabs; "docs" gates the
# standalone Documentation portal; "users" gates the User Management page (no
# full admin rights needed). "*" in a role grants every capability.
# "rings" gates the Ring Studio (diamond-ring image-prompt generator) dashboard.
ALL_TABS = ["repos", "jira", "insights", "logs", "testcases", "similar", "workflows", "channels", "utilization", "neo4j", "rca", "rings", "zoho", "docs", "users"]

# Per-tab custom roles. Edit this map (or override with the AUTH_ROLE_TABS env
# var as JSON) to change which role can reach which capability.
DEFAULT_ROLE_TABS: dict[str, list[str]] = {
    "admin": ["*"],
    "developer": ["repos", "logs", "jira", "testcases", "similar", "neo4j", "docs"],
    "qa": ["jira", "insights", "testcases", "similar", "utilization", "docs"],
    "viewer": ["jira", "insights", "logs", "utilization"],
    # Single-purpose roles: land on (and only see) one area.
    "documentation": ["docs"],
    "usermgr": ["users"],
    # RCA role: only the Root Cause Analysis tab (Ticket ID → full RCA). Admin
    # also has it via "*"; no other role does, by design.
    "rca": ["rca"],
    # Designer role: lands on (and only sees) the Ring Studio — the diamond-ring
    # image-prompt generator. Admin also has it via "*"; no other role does.
    "designer": ["rings"],
    # Support role: lands on (and only sees) the Zoho Tickets tab — look a
    # customer up and view their Zoho Desk tickets. Admin also has it via "*".
    "support": ["zoho"],
}


def _load_role_tabs() -> dict[str, list[str]]:
    raw = os.getenv("AUTH_ROLE_TABS", "").strip()
    if not raw:
        return dict(DEFAULT_ROLE_TABS)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("AUTH_ROLE_TABS must be a JSON object")
        cleaned: dict[str, list[str]] = {}
        for role, tabs in parsed.items():
            if not isinstance(tabs, list):
                raise ValueError(f"tabs for role '{role}' must be a list")
            cleaned[str(role)] = [str(t) for t in tabs]
        # Admin is always a superuser even if omitted.
        cleaned.setdefault("admin", ["*"])
        return cleaned
    except Exception as exc:  # noqa: BLE001 - fall back to defaults on bad config
        log.warning("Invalid AUTH_ROLE_TABS (%s); using defaults", exc)
        return dict(DEFAULT_ROLE_TABS)


ROLE_TABS = _load_role_tabs()


def tabs_for_role(role: str) -> list[str]:
    """Resolve the concrete list of tab keys a role can access."""
    granted = ROLE_TABS.get(role, [])
    if "*" in granted:
        return list(ALL_TABS)
    # Preserve canonical order and drop unknown tab keys.
    return [t for t in ALL_TABS if t in set(granted)]


def has_tab(role: str, tab: str) -> bool:
    granted = ROLE_TABS.get(role, [])
    return "*" in granted or tab in granted


def known_roles() -> list[str]:
    return sorted(ROLE_TABS.keys())


# ─── Password hashing ────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ─── JWT ─────────────────────────────────────────────────────────────────────

def _jwt_secret() -> str:
    secret = getattr(settings, "jwt_secret", "") or os.getenv("JWT_SECRET", "")
    if not secret:
        # Deterministic dev fallback so the app boots without config, but log it.
        log.warning("JWT_SECRET not set; using an insecure development secret")
        secret = "insecure-dev-secret-change-me"
    return secret


def create_access_token(email: str, role: str) -> str:
    expire_minutes = int(getattr(settings, "jwt_expire_minutes", 0) or 720)
    now = _dt.datetime.now(_dt.timezone.utc)
    payload = {
        "sub": email,
        "role": role,
        "iat": now,
        "exp": now + _dt.timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])


# ─── Database access ─────────────────────────────────────────────────────────

class AuthConfigError(RuntimeError):
    """Raised when authentication is requested but Postgres is not configured."""


def _connect():
    if not settings.database_url:
        raise AuthConfigError("DATABASE_URL is not configured; auth is unavailable")
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def ensure_auth_schema() -> None:
    """Create the users table and seed the bootstrap admin (idempotent)."""
    if not settings.database_url:
        log.warning("DATABASE_URL not set; skipping auth schema setup")
        return
    try:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_users (
                    id            SERIAL PRIMARY KEY,
                    email         TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL DEFAULT 'viewer',
                    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.commit()
        _seed_admin()
    except AuthConfigError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Failed to ensure auth schema")


def _seed_admin() -> None:
    email = (getattr(settings, "admin_email", "") or os.getenv("ADMIN_EMAIL", "")).strip().lower()
    password = getattr(settings, "admin_password", "") or os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        log.info("ADMIN_EMAIL / ADMIN_PASSWORD not set; no bootstrap admin seeded")
        return
    # Opt-in: re-sync the bootstrap admin's password/role on startup. Useful when
    # the admin was seeded earlier with a different password (the normal seeder
    # never touches an existing user).
    reset = (os.getenv("ADMIN_RESET_PASSWORD", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM app_users WHERE email = %s", (email,)
        ).fetchone()
        if existing:
            if reset:
                conn.execute(
                    "UPDATE app_users SET password_hash = %s, role = 'admin', is_active = TRUE "
                    "WHERE id = %s",
                    (hash_password(password), existing["id"]),
                )
                conn.commit()
                log.warning("ADMIN_RESET_PASSWORD set: reset bootstrap admin %s", email)
            else:
                log.info("Bootstrap admin %s already exists; leaving it unchanged", email)
            return
        conn.execute(
            "INSERT INTO app_users (email, password_hash, role, is_active) "
            "VALUES (%s, %s, 'admin', TRUE)",
            (email, hash_password(password)),
        )
        conn.commit()
        log.info("Seeded bootstrap admin user %s", email)


def get_user_by_email(email: str) -> Optional[dict]:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, email, password_hash, role, is_active, created_at "
            "FROM app_users WHERE email = %s",
            (email.strip().lower(),),
        ).fetchone()


def get_user_by_id(user_id: int) -> Optional[dict]:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, email, password_hash, role, is_active, created_at "
            "FROM app_users WHERE id = %s",
            (user_id,),
        ).fetchone()


def list_users() -> list[dict]:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, email, role, is_active, created_at "
            "FROM app_users ORDER BY created_at ASC, id ASC"
        ).fetchall()


def create_user(email: str, password: str, role: str, is_active: bool = True) -> dict:
    email = email.strip().lower()
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO app_users (email, password_hash, role, is_active) "
            "VALUES (%s, %s, %s, %s) "
            "RETURNING id, email, role, is_active, created_at",
            (email, hash_password(password), role, is_active),
        ).fetchone()
        conn.commit()
        return row


def update_user(
    user_id: int,
    *,
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    password: Optional[str] = None,
) -> Optional[dict]:
    sets: list[str] = []
    params: list = []
    if role is not None:
        sets.append("role = %s")
        params.append(role)
    if is_active is not None:
        sets.append("is_active = %s")
        params.append(is_active)
    if password:
        sets.append("password_hash = %s")
        params.append(hash_password(password))
    if not sets:
        return get_user_by_id(user_id)
    params.append(user_id)
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE app_users SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, email, role, is_active, created_at",
            tuple(params),
        ).fetchone()
        conn.commit()
        return row


def delete_user(user_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM app_users WHERE id = %s", (user_id,))
        conn.commit()
        return cur.rowcount > 0


# ─── Pydantic models ─────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    is_active: bool = True
    permissions: list[str] = Field(default_factory=list)


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    role: str = "viewer"
    is_active: bool = True


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6)


class CurrentUser(BaseModel):
    id: int
    email: str
    role: str
    is_active: bool = True
    is_service: bool = False


def _to_user_out(row: dict) -> UserOut:
    role = row["role"]
    return UserOut(
        id=row["id"],
        email=row["email"],
        role=role,
        is_active=row.get("is_active", True),
        permissions=tabs_for_role(role),
    )


# ─── Dependencies ────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def _service_request(request: Request) -> bool:
    key = getattr(settings, "service_api_key", "") or os.getenv("SERVICE_API_KEY", "")
    if not key:
        return False
    return request.headers.get("X-Service-Key") == key


def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> CurrentUser:
    if _service_request(request):
        return CurrentUser(id=0, email="service-account", role="admin", is_service=True)

    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(creds.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        user = get_user_by_email(email)
    except AuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if user is None or not user.get("is_active", True):
        raise HTTPException(status_code=401, detail="User not found or disabled")
    return CurrentUser(
        id=user["id"], email=user["email"], role=user["role"], is_active=user["is_active"]
    )


def require_tab(*tabs: str):
    """Dependency factory: allow the request if the user can access ANY of ``tabs``."""

    def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.is_service:
            return user
        if any(has_tab(user.role, tab) for tab in tabs):
            return user
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user.role}' is not allowed to access this resource",
        )

    return _dep


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


# ─── Router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    email = (payload.email or "").strip().lower()
    password = payload.password or ""
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    try:
        user = get_user_by_email(email)
    except AuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if user is None:
        # First-time user auto-registration
        try:
            with _connect() as conn:
                row_count = conn.execute("SELECT count(*) as c FROM app_users").fetchone()
                is_first = (row_count["c"] == 0) if row_count else False
                new_role = "admin" if is_first else "developer"
                inserted = conn.execute(
                    """
                    INSERT INTO app_users (email, password_hash, role, is_active)
                    VALUES (%s, %s, %s, TRUE)
                    RETURNING id, email, password_hash, role, is_active, created_at
                    """,
                    (email, hash_password(password), new_role),
                ).fetchone()
                conn.commit()
                user = inserted
                log.info("Auto-registered new first-time user: %s (role: %s)", email, new_role)
        except Exception as exc:
            log.exception("Failed to auto-register user %s", email)
            raise HTTPException(status_code=500, detail=f"Failed to create user account: {exc}") from exc
    else:
        if not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid password for this account.")
        if not user.get("is_active", True):
            raise HTTPException(status_code=403, detail="Account is disabled. Please contact an administrator.")

    token = create_access_token(user["email"], user["role"])
    return TokenResponse(access_token=token, user=_to_user_out(user))


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: LoginRequest) -> TokenResponse:
    email = (payload.email or "").strip().lower()
    password = payload.password or ""
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    try:
        user = get_user_by_email(email)
    except AuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if user is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in.")

    try:
        with _connect() as conn:
            row_count = conn.execute("SELECT count(*) as c FROM app_users").fetchone()
            is_first = (row_count["c"] == 0) if row_count else False
            new_role = "admin" if is_first else "developer"
            inserted = conn.execute(
                """
                INSERT INTO app_users (email, password_hash, role, is_active)
                VALUES (%s, %s, %s, TRUE)
                RETURNING id, email, password_hash, role, is_active, created_at
                """,
                (email, hash_password(password), new_role),
            ).fetchone()
            conn.commit()
            user = inserted
            log.info("Registered new user: %s (role: %s)", email, new_role)
    except Exception as exc:
        log.exception("Failed to register user %s", email)
        raise HTTPException(status_code=500, detail=f"Failed to create user account: {exc}") from exc

    token = create_access_token(user["email"], user["role"])
    return TokenResponse(access_token=token, user=_to_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(get_current_user)) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        permissions=tabs_for_role(user.role),
    )


@router.get("/roles")
def roles(_: CurrentUser = Depends(require_tab("users"))) -> dict:
    return {"roles": known_roles(), "tabs": ALL_TABS, "role_tabs": ROLE_TABS}


@router.get("/users", response_model=list[UserOut])
def admin_list_users(_: CurrentUser = Depends(require_tab("users"))) -> list[UserOut]:
    return [_to_user_out(u) for u in list_users()]


@router.post("/users", response_model=UserOut, status_code=201)
def admin_create_user(
    payload: CreateUserRequest, _: CurrentUser = Depends(require_tab("users"))
) -> UserOut:
    if payload.role not in ROLE_TABS:
        raise HTTPException(status_code=400, detail=f"Unknown role '{payload.role}'")
    if get_user_by_email(payload.email):
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    row = create_user(payload.email, payload.password, payload.role, payload.is_active)
    return _to_user_out(row)


@router.patch("/users/{user_id}", response_model=UserOut)
def admin_update_user(
    user_id: int,
    payload: UpdateUserRequest,
    actor: CurrentUser = Depends(require_tab("users")),
) -> UserOut:
    if payload.role is not None and payload.role not in ROLE_TABS:
        raise HTTPException(status_code=400, detail=f"Unknown role '{payload.role}'")
    if user_id == actor.id and payload.is_active is False:
        raise HTTPException(status_code=400, detail="You cannot disable your own account")
    row = update_user(
        user_id, role=payload.role, is_active=payload.is_active, password=payload.password
    )
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return _to_user_out(row)


@router.delete("/users/{user_id}", status_code=204)
def admin_delete_user(
    user_id: int, actor: CurrentUser = Depends(require_tab("users"))
) -> None:
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if not delete_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")


TokenResponse.model_rebuild()
