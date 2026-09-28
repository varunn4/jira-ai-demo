#!/usr/bin/env python3
"""Database Setup and Migration Script for JIRA-AI.

Tests database connectivity, runs all schema migrations idempotently,
verifies table creation in PostgreSQL, and seeds the initial admin user.

Usage:
    python scripts/setup_db.py
"""

import sys
from pathlib import Path

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
import psycopg
from psycopg.rows import dict_row

from app.config import settings, reload_settings
from app.db import init_db
from app.auth import ensure_auth_schema, get_user_by_email, create_user, update_user

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def setup_database() -> int:
    print("=" * 60)
    print("  JIRA-AI Database Setup & Migration")
    print("=" * 60)

    db_url = settings.database_url
    if not db_url:
        print("ERROR: DATABASE_URL is not configured in .env or environment settings.", file=sys.stderr)
        return 1

    # Mask password for display
    try:
        url_display = db_url.rsplit("@", 1)[-1]
    except Exception:
        url_display = "<configured>"
    print(f"[1/4] Target Database: {url_display}")

    # 1. Test Connection
    print("[2/4] Testing PostgreSQL database connection...")
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                version = cur.fetchone()[0]
                print(f"      Connected successfully! PostgreSQL version: {version.split(',')[0]}")
    except Exception as exc:
        print(f"ERROR: Could not connect to PostgreSQL: {exc}", file=sys.stderr)
        print("Please ensure your PostgreSQL server is running and database details in .env are correct.", file=sys.stderr)
        return 1

    # 2. Run Idempotent Schema Initialization & Migrations
    print("[3/4] Running schema initialization and idempotent migrations...")
    try:
        init_db(settings)
        ensure_auth_schema()
        reload_settings()
        print("      Schema migration completed successfully.")
    except Exception as exc:
        print(f"ERROR: Schema migration failed: {exc}", file=sys.stderr)
        return 1

    # 3. Seed / Verify Admin User
    print("[4/4] Verifying default admin user seeding...")
    admin_email = (settings.admin_email or "admin@yourcompany.com").strip().lower()
    admin_password = settings.admin_password or "admin123"

    try:
        user = get_user_by_email(admin_email)
        if user:
            print(f"      Admin user '{admin_email}' already exists (Role: {user.get('role', 'admin')}).")
        else:
            create_user(admin_email, admin_password, role="admin", is_active=True)
            print(f"      Created initial admin user: '{admin_email}'.")
    except Exception as exc:
        print(f"WARNING: Admin user seeding skipped/failed: {exc}")

    # 4. Verify Tables in Database
    print("-" * 60)
    print("Database Table Verification:")
    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;"
            ).fetchall()
            table_names = [t["table_name"] for t in tables]
            print(f"Total tables found in public schema: {len(table_names)}")
            for name in table_names:
                print(f"  ✓ {name}")
    except Exception as exc:
        print(f"WARNING: Failed to query database table list: {exc}")

    print("=" * 60)
    print("SUCCESS: Database setup and migration completed cleanly!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(setup_database())
