"""Tiny key-value settings store for runtime-editable config.

Backs admin-panel toggles that must survive restarts without a redeploy (e.g.
the WF7 RFT-estimate sprint filter). One self-healing table ``app_settings``;
values are plain strings, callers parse as needed.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import Settings

LOGGER = logging.getLogger(__name__)


def _connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.database_url, row_factory=dict_row)


def ensure_app_settings_schema(settings: Settings) -> None:
    if not settings.database_url:
        return
    with _connect(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.commit()


def get_setting(settings: Settings, key: str, default: Optional[str] = None) -> Optional[str]:
    if not settings.database_url:
        return default
    try:
        with _connect(settings) as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s", (key,)
            ).fetchone()
        if row and row.get("value") is not None:
            return row["value"]
    except Exception as exc:  # noqa: BLE001 - never let config reads crash a request
        LOGGER.debug("get_setting(%s) failed: %s", key, exc)
    return default


def set_setting(settings: Settings, key: str, value: str) -> None:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required to persist settings")
    ensure_app_settings_schema(settings)
    with _connect(settings) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (key, value),
        )
        conn.commit()


def get_all_settings(settings: Settings) -> dict[str, str]:
    """Return all key-value pairs stored in app_settings table."""
    if not settings.database_url:
        return {}
    ensure_app_settings_schema(settings)
    try:
        with _connect(settings) as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
            return {r["key"]: r["value"] for r in rows if r.get("key") and r.get("value") is not None}
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("get_all_settings failed: %s", exc)
        return {}


def set_settings_bulk(settings: Settings, items: dict[str, str]) -> None:
    """Persist multiple key-value settings in a single transaction."""
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required to persist settings")
    if not items:
        return
    ensure_app_settings_schema(settings)
    with _connect(settings) as conn:
        for key, value in items.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (key, str(value)),
            )
        conn.commit()


USER_SPECIFIC_KEYS = {
    "setup_completed",
    "github_source_type",
    "github_org_or_user",
    "github_repo_urls",
    "github_token",
    "github_org",
    "jira_base_url",
    "jira_email",
    "jira_api_token",
    "jira_project_key",
    "jira_project_keys",
    "jira_excluded_project_keys",
}


def get_all_user_settings(settings: Settings, user_id: Optional[int] = None, user_email: Optional[str] = None) -> dict[str, str]:
    """Return key-value settings for a specific user, isolating user-specific credentials from global leakages."""
    base_settings = get_all_settings(settings)
    if not settings.database_url or not user_id:
        return base_settings

    # If user_id is provided, strip user-specific keys from global base so one user never sees another's repos/tokens
    cleaned_base = {k: v for k, v in base_settings.items() if k not in USER_SPECIFIC_KEYS}

    try:
        with _connect(settings) as conn:
            # Ensure table exists
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_user_settings (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
                    user_email TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(user_id, key)
                );
                """
            )
            rows = conn.execute(
                "SELECT key, value FROM app_user_settings WHERE user_id = %s",
                (user_id,),
            ).fetchall()
            user_overrides = {r["key"]: r["value"] for r in rows if r.get("key") and r.get("value") is not None}
            cleaned_base.update(user_overrides)
            return cleaned_base
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("get_all_user_settings failed for user_id=%s: %s", user_id, exc)
        return cleaned_base


def set_user_settings_bulk(settings: Settings, user_id: int, user_email: str, items: dict[str, str]) -> None:
    """Persist multiple user-scoped key-value settings in a single transaction."""
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required to persist user settings")
    if not items:
        return

    with _connect(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_user_settings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES app_users(id) ON DELETE CASCADE,
                user_email TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(user_id, key)
            );
            """
        )
        for key, value in items.items():
            conn.execute(
                """
                INSERT INTO app_user_settings (user_id, user_email, key, value, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (user_id, user_email, key, str(value)),
            )
        conn.commit()


