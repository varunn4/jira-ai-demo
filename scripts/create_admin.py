#!/usr/bin/env python3
"""Create or reset an admin (or any) user for the Jira AI admin UI.

The startup seeder only inserts the bootstrap admin if it does not already
exist, so it will NOT change the password of an admin created on an earlier
deploy. Use this script to create a user or reset an existing one's password
and role.

Run from the repository root (so `app` is importable):

    python scripts/create_admin.py                       # uses ADMIN_EMAIL / ADMIN_PASSWORD from env/.env
    python scripts/create_admin.py user@x.com 'Secret123' admin
    python scripts/create_admin.py user@x.com 'Secret123' qa

Inside Docker:

    docker compose exec jira-ai-api python scripts/create_admin.py user@x.com 'Secret123' admin
"""

import os
import sys
from pathlib import Path

# Allow running as `python scripts/create_admin.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import (  # noqa: E402
    ROLE_TABS,
    ensure_auth_schema,
    get_user_by_email,
    create_user,
    update_user,
    tabs_for_role,
)
from app.config import settings  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    email = (args[0] if len(args) > 0 else settings.admin_email or os.getenv("ADMIN_EMAIL", "")).strip().lower()
    password = args[1] if len(args) > 1 else (settings.admin_password or os.getenv("ADMIN_PASSWORD", ""))
    role = (args[2] if len(args) > 2 else "admin").strip()

    if not settings.database_url:
        print("ERROR: DATABASE_URL is not configured; cannot manage users.", file=sys.stderr)
        return 2
    if not email or not password:
        print("ERROR: provide an email and password (or set ADMIN_EMAIL/ADMIN_PASSWORD).", file=sys.stderr)
        return 2
    if role not in ROLE_TABS:
        print(f"ERROR: unknown role '{role}'. Known roles: {', '.join(sorted(ROLE_TABS))}", file=sys.stderr)
        return 2

    # Make sure the table exists before we touch it.
    ensure_auth_schema()

    existing = get_user_by_email(email)
    if existing:
        update_user(existing["id"], role=role, is_active=True, password=password)
        action = "reset"
    else:
        create_user(email, password, role, is_active=True)
        action = "created"

    print(f"OK: {action} user {email} with role '{role}' (tabs: {', '.join(tabs_for_role(role)) or 'none'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
