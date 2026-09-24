"""Dynamic System Settings and Onboarding Validation Router."""

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from requests.auth import HTTPBasicAuth

from app.app_settings import get_all_settings, get_all_user_settings, set_settings_bulk, set_user_settings_bulk
from app.auth import CurrentUser, require_tab
from app.config import reload_settings, settings
from app.llm_client import build_llm_client
from app.repository_discovery import (
    clone_or_sync_repo,
    discover_graph_repositories,
    fetch_github_org_repos,
    fetch_single_github_repo,
    get_workspace_repos_dir,
    parse_github_repo_slug,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["Settings"])


def _mask_secret(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "********"
    return f"{val[:4]}...{val[-4:]}"


@router.get("/status")
def get_setup_status(_user: CurrentUser = Depends(require_tab("repos"))) -> dict[str, Any]:
    """Check if all mandatory settings are configured for the platform to operate."""
    reload_settings()
    db_items = get_all_user_settings(settings, user_id=getattr(_user, "id", None), user_email=getattr(_user, "email", None))

    missing: list[str] = []

    # 1. Repositories (check if user has synced repositories into workspace)
    repos = discover_graph_repositories(settings, user_id=getattr(_user, "id", None))
    if not repos:
        missing.append("repositories")

    # 2. Jira credentials
    jira_url = db_items.get("jira_base_url")
    jira_email = db_items.get("jira_email")
    jira_token = db_items.get("jira_api_token")
    if not (jira_url and jira_email and jira_token):
        missing.append("jira")

    # 3. LLM Configuration
    provider = db_items.get("llm_provider", "groq").strip().lower()
    openai_key = db_items.get("openai_api_key")
    anthropic_key = db_items.get("anthropic_api_key")
    if provider in ("groq", "openai", "gemini"):
        if not openai_key:
            missing.append("llm")
    elif provider == "anthropic":
        if not anthropic_key:
            missing.append("llm")
    elif provider == "mock":
        pass  # mock requires no key
    else:
        if not openai_key and not anthropic_key:
            missing.append("llm")

    # Setup is complete if user has already marked it done, or has configured settings, or has dismissed it
    is_setup_done = db_items.get("setup_completed") == "true"
    has_any_config = bool(jira_url or db_items.get("openai_api_key") or db_items.get("anthropic_api_key") or repos)

    setup_is_complete = is_setup_done or (has_any_config and len(missing) == 0)

    return {
        "setup_complete": setup_is_complete,
        "missing_sections": missing,
        "repository_configured": "repositories" not in missing,
        "jira_configured": "jira" not in missing,
        "llm_configured": "llm" not in missing,
        "current_provider": provider,
        "repositories_count": len(repos),
        "repositories": [r["name"] for r in repos],
    }


@router.post("/dismiss")
def dismiss_setup_wizard(_user: CurrentUser = Depends(require_tab("repos"))) -> dict[str, Any]:
    """Mark onboarding setup wizard as dismissed/completed for the current user."""
    if hasattr(_user, "id") and _user.id:
        set_user_settings_bulk(settings, _user.id, _user.email, {"setup_completed": "true"})
    set_settings_bulk(settings, {"setup_completed": "true"})
    return {"status": "ok", "setup_completed": True}


@router.get("")
def get_settings_view(_user: CurrentUser = Depends(require_tab("repos"))) -> dict[str, Any]:
    """Return all configurable settings with masked secrets for the authenticated user."""
    reload_settings()
    user_id = getattr(_user, "id", None)
    db_items = get_all_user_settings(settings, user_id=user_id, user_email=getattr(_user, "email", None))

    # User-specific credentials and repo configs should only come from user's own saved settings
    return {
        "github_source_type": db_items.get("github_source_type", "urls"),
        "github_org_or_user": db_items.get("github_org_or_user", ""),
        "github_repo_urls": db_items.get("github_repo_urls", ""),
        "github_token_masked": _mask_secret(db_items.get("github_token", "")),
        "github_has_token": bool(db_items.get("github_token", "")),
        "repository_search_root": db_items.get("repository_search_root", getattr(settings, "repository_search_root", ".")),
        "excluded_repository_names": db_items.get("excluded_repository_names", getattr(settings, "excluded_repository_names", "JIRA-AI")),
        "jira_base_url": db_items.get("jira_base_url", ""),
        "jira_email": db_items.get("jira_email", ""),
        "jira_api_token_masked": _mask_secret(db_items.get("jira_api_token", "")),
        "jira_has_token": bool(db_items.get("jira_api_token", "")),
        "jira_project_key": db_items.get("jira_project_key", db_items.get("jira_project_keys", "")),
        "jira_excluded_project_keys": db_items.get("jira_excluded_project_keys", ""),
        "llm_provider": db_items.get("llm_provider", getattr(settings, "llm_provider", "anthropic")),
        "llm_model": db_items.get("llm_model", getattr(settings, "llm_model", "claude-sonnet-4-6")),
        "openai_base_url": db_items.get("openai_base_url", getattr(settings, "openai_base_url", "")),
        "openai_api_key_masked": _mask_secret(db_items.get("openai_api_key", getattr(settings, "openai_api_key", ""))),
        "openai_has_key": bool(db_items.get("openai_api_key", getattr(settings, "openai_api_key", ""))),
        "anthropic_api_key_masked": _mask_secret(db_items.get("anthropic_api_key", getattr(settings, "anthropic_api_key", ""))),
        "anthropic_has_key": bool(db_items.get("anthropic_api_key", getattr(settings, "anthropic_api_key", ""))),
        "anthropic_model": db_items.get("anthropic_model", getattr(settings, "anthropic_model", "")),
        "slack_bot_token_masked": _mask_secret(db_items.get("slack_bot_token", getattr(settings, "slack_bot_token", ""))),
        "slack_has_token": bool(db_items.get("slack_bot_token", getattr(settings, "slack_bot_token", ""))),
        "slack_channel_id": db_items.get("slack_channel_id", getattr(settings, "slack_channel_id", "")),
        "qdrant_url": db_items.get("qdrant_url", getattr(settings, "qdrant_url", "")),
        "ollama_url": db_items.get("ollama_url", getattr(settings, "ollama_url", "")),
        "n8n_base_url": db_items.get("n8n_base_url", getattr(settings, "n8n_base_url", "")),
        "n8n_api_key_masked": _mask_secret(db_items.get("n8n_api_key", getattr(settings, "n8n_api_key", ""))),
        "n8n_has_key": bool(db_items.get("n8n_api_key", getattr(settings, "n8n_api_key", ""))),
        "zoho_client_id": db_items.get("zoho_client_id", getattr(settings, "zoho_client_id", "")),
        "zoho_client_secret_masked": _mask_secret(db_items.get("zoho_client_secret", getattr(settings, "zoho_client_secret", ""))),
        "zoho_has_client_secret": bool(db_items.get("zoho_client_secret", getattr(settings, "zoho_client_secret", ""))),
        "zoho_refresh_token_masked": _mask_secret(db_items.get("zoho_refresh_token", getattr(settings, "zoho_refresh_token", ""))),
        "zoho_has_refresh_token": bool(db_items.get("zoho_refresh_token", getattr(settings, "zoho_refresh_token", ""))),
        "zoho_org_id": db_items.get("zoho_org_id", getattr(settings, "zoho_org_id", "")),
        "zoho_accounts_base": db_items.get("zoho_accounts_base", getattr(settings, "zoho_accounts_base", "https://accounts.zoho.in")),
        "zoho_desk_base": db_items.get("zoho_desk_base", getattr(settings, "zoho_desk_base", "https://desk.zoho.in")),
    }


@router.post("")
def save_settings(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    """Update settings in database for the authenticated user and reload runtime configuration."""
    to_save: dict[str, str] = {}

    allowed_keys = {
        "setup_completed",
        "github_source_type",
        "github_org_or_user",
        "github_repo_urls",
        "github_token",
        "github_org",
        "repository_search_root",
        "repository_host_root",
        "rca_repo_root",
        "excluded_repository_names",
        "jira_base_url",
        "jira_email",
        "jira_api_token",
        "jira_project_key",
        "jira_project_keys",
        "jira_excluded_project_keys",
        "llm_provider",
        "llm_model",
        "openai_api_key",
        "openai_base_url",
        "anthropic_api_key",
        "anthropic_model",
        "slack_bot_token",
        "slack_channel_id",
        "slack_chat_channel_id",
        "qdrant_url",
        "ollama_url",
        "n8n_base_url",
        "n8n_api_key",
        "zoho_client_id",
        "zoho_client_secret",
        "zoho_refresh_token",
        "zoho_org_id",
        "zoho_accounts_base",
        "zoho_desk_base",
    }

    for k, v in payload.items():
        if k in allowed_keys and v is not None:
            v_str = str(v).strip()
            # Do not overwrite with placeholder masked value
            if "..." in v_str and "masked" in k:
                continue
            if k == "github_org_or_user":
                to_save["github_org_or_user"] = v_str
                to_save["github_org"] = v_str
            elif k == "repository_search_root":
                to_save["repository_search_root"] = v_str
                to_save["repository_host_root"] = v_str
                to_save["rca_repo_root"] = v_str
            elif k == "jira_project_key":
                to_save["jira_project_key"] = v_str
                to_save["jira_project_keys"] = v_str
            elif k == "jira_project_keys":
                to_save["jira_project_key"] = v_str
                to_save["jira_project_keys"] = v_str
            else:
                to_save[k] = v_str

    if to_save:
        if hasattr(_user, "id") and _user.id:
            try:
                set_user_settings_bulk(settings, _user.id, _user.email, to_save)
            except Exception as exc:
                log.warning("Failed to save user settings for user_id=%s: %s", _user.id, exc)
        else:
            set_settings_bulk(settings, to_save)
        reload_settings()
        log.info("Dynamic settings saved and reloaded for user=%s: %s", getattr(_user, "email", "global"), list(to_save.keys()))

    return {"status": "ok", "message": "Settings saved successfully", "saved_keys": list(to_save.keys())}


@router.post("/validate-github")
def validate_github_integration(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    """Test GitHub credentials/URLs and clone/sync target repositories into managed workspace."""
    source_type = str(payload.get("source_type") or "org").strip().lower()
    org_or_user = str(payload.get("github_org_or_user") or "").strip()
    raw_urls = str(payload.get("github_repo_urls") or "").strip()
    token = str(payload.get("github_token") or "").strip()

    # Fallback to saved token if not explicitly provided or masked
    db_items = get_all_user_settings(settings, user_id=getattr(_user, "id", None), user_email=getattr(_user, "email", None))
    if not token or "..." in token:
        token = db_items.get("github_token", getattr(settings, "github_token", ""))

    discovered_meta: list[dict[str, Any]] = []

    try:
        if source_type == "org":
            if not org_or_user:
                return {"valid": False, "error": "GitHub Organization or Username is required.", "repo_count": 0, "repos": []}
            
            # Smart detection: if user entered a full repository URL or owner/repo slug into org input, handle as single repo
            slug = parse_github_repo_slug(org_or_user)
            if slug and ("github.com" in org_or_user or "/" in org_or_user.strip("/")):
                discovered_meta = [fetch_single_github_repo(org_or_user, token=token)]
            else:
                discovered_meta = fetch_github_org_repos(org_or_user, token=token)

            if not discovered_meta:
                return {"valid": False, "error": f"No repositories found under GitHub organization/user '{org_or_user}'.", "repo_count": 0, "repos": []}
        else:
            # Specific URLs or slugs
            if not raw_urls:
                return {"valid": False, "error": "Please provide at least one GitHub repository URL or slug (e.g. 'AonamiTech/trail').", "repo_count": 0, "repos": []}

            # Parse lines or comma-separated URLs
            url_candidates = [u.strip() for u in re.split(r"[\n,]+", raw_urls) if u.strip()]
            if not url_candidates:
                return {"valid": False, "error": "No valid GitHub repository URLs found in input.", "repo_count": 0, "repos": []}

            for candidate in url_candidates:
                meta = fetch_single_github_repo(candidate, token=token)
                discovered_meta.append(meta)

    except Exception as exc:
        log.warning("GitHub repository discovery failed: %s", exc)
        return {"valid": False, "error": str(exc), "repo_count": 0, "repos": []}

    # Clone / Sync discovered repositories into user workspace
    cloned_repos: list[dict[str, Any]] = []
    failed_clones: list[dict[str, str]] = []

    for meta in discovered_meta:
        clone_url = meta.get("clone_url")
        name = meta.get("name")
        branch = meta.get("default_branch", "main")
        if not clone_url or not name:
            continue
        try:
            info = clone_or_sync_repo(
                clone_url=clone_url,
                target_name=name,
                token=token,
                branch=branch,
                user_id=getattr(_user, "id", None),
            )
            cloned_repos.append(info)
        except Exception as clone_exc:
            log.warning("Failed cloning repo %s: %s", name, clone_exc)
            failed_clones.append({"name": name, "error": str(clone_exc)})

    if not cloned_repos and failed_clones:
        return {
            "valid": False,
            "error": f"Failed to clone repositories: {', '.join(f.get('name', '') + ' (' + f.get('error', '') + ')' for f in failed_clones)}",
            "repo_count": 0,
            "repos": [],
        }

    # Save github settings
    to_save = {
        "github_source_type": source_type,
        "github_org_or_user": org_or_user,
        "github_repo_urls": raw_urls,
    }
    if token and "..." not in token:
        to_save["github_token"] = token
        to_save["github_org"] = org_or_user

    if hasattr(_user, "id") and _user.id:
        set_user_settings_bulk(settings, _user.id, _user.email, to_save)
    else:
        set_settings_bulk(settings, to_save)
    reload_settings()

    return {
        "valid": True,
        "repo_count": len(cloned_repos),
        "repos": [r["name"] for r in cloned_repos],
        "cloned_details": cloned_repos,
        "failed_clones": failed_clones,
        "message": f"Successfully connected and synced {len(cloned_repos)} repository(ies) from GitHub.",
    }






@router.post("/test-jira")
def test_jira_connection(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    """Test Jira connection with provided credentials or saved credentials."""
    base_url = (payload.get("jira_base_url") or settings.jira_base_url or "").strip().rstrip("/")
    email = (payload.get("jira_email") or settings.jira_email or "").strip()
    token = (payload.get("jira_api_token") or settings.jira_api_token or "").strip()

    if not base_url or not email or not token:
        return {"success": False, "error": "Jira URL, email, and API token are all required."}

    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = f"https://{base_url}"

    test_url = f"{base_url}/rest/api/3/myself"
    headers = {"Accept": "application/json"}

    try:
        res = requests.get(
            test_url,
            headers=headers,
            auth=HTTPBasicAuth(email, token),
            timeout=10,
        )
        if res.status_code == 200:
            data = res.json()
            return {
                "success": True,
                "display_name": data.get("displayName", "User"),
                "email": data.get("emailAddress", email),
                "account_id": data.get("accountId"),
                "message": f"Connected to Jira successfully as {data.get('displayName')}.",
            }
        if res.status_code == 401:
            return {"success": False, "error": "401 Unauthorized: Invalid Jira email or API token."}
        if res.status_code == 403:
            return {"success": False, "error": "403 Forbidden: Account does not have API access permissions."}
        return {"success": False, "error": f"Jira returned HTTP {res.status_code}: {res.text[:200]}"}
    except requests.exceptions.Timeout:
        return {"success": False, "error": f"Connection timed out connecting to {base_url}."}
    except Exception as exc:
        return {"success": False, "error": f"Failed to connect: {exc}"}


@router.post("/test-llm")
def test_llm_connection(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    """Test LLM provider connection with a fast test prompt."""
    provider = (payload.get("llm_provider") or settings.llm_provider or "groq").strip().lower()
    api_key = (payload.get("api_key") or payload.get("openai_api_key") or payload.get("anthropic_api_key") or "").strip()
    model = (payload.get("llm_model") or payload.get("model") or "").strip()
    base_url = (payload.get("openai_base_url") or "").strip()

    # Create temporary settings object for testing
    from dataclasses import replace
    test_cfg = replace(
        settings,
        llm_provider=provider,
        openai_api_key=api_key or settings.openai_api_key,
        anthropic_api_key=api_key or settings.anthropic_api_key,
        llm_model=model or settings.llm_model,
        openai_base_url=base_url or settings.openai_base_url,
    )

    try:
        client = build_llm_client(test_cfg)
        reply = client.complete("Respond with the exact word 'READY' and nothing else.", "Ping").strip()
        return {
            "success": True,
            "provider": provider,
            "model": test_cfg.llm_model,
            "reply": reply[:100],
            "message": f"Connected to {provider.upper()} successfully!",
        }
    except Exception as exc:
        log.exception("LLM test ping failed")
        return {"success": False, "provider": provider, "error": str(exc)}


@router.post("/test-n8n")
def test_n8n_connection(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    """Test n8n connection with provided base URL and API key."""
    base_url = (payload.get("n8n_base_url") or settings.n8n_base_url or "").strip().rstrip("/")
    api_key = (payload.get("n8n_api_key") or settings.n8n_api_key or "").strip()

    if not base_url:
        return {"success": False, "error": "n8n Base URL is required (e.g. http://localhost:5678)."}
    if not api_key:
        return {"success": False, "error": "n8n API Key is required."}

    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = f"http://{base_url}"

    test_url = f"{base_url}/api/v1/workflows"
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}

    try:
        res = requests.get(test_url, headers=headers, params={"limit": 5}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            workflows = data.get("data", [])
            count = len(workflows)
            return {
                "success": True,
                "message": f"Connected to n8n successfully! Found {count} workflow(s).",
                "workflows_count": count,
            }
        if res.status_code == 401:
            return {"success": False, "error": "401 Unauthorized: n8n rejected the API Key. Verify N8N_API_KEY."}
        if res.status_code == 403:
            return {"success": False, "error": "403 Forbidden: API key lacks permission to read workflows."}
        return {"success": False, "error": f"n8n returned HTTP {res.status_code}: {res.text[:200]}"}
    except requests.exceptions.Timeout:
        return {"success": False, "error": f"Connection timed out connecting to n8n at {base_url}."}
    except Exception as exc:
        return {"success": False, "error": f"Failed to reach n8n: {exc}"}


@router.post("/test-slack")
def test_slack_connection(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    """Test Slack bot connection and dispatch a ping message."""
    db_items = get_all_user_settings(settings, user_id=getattr(_user, "id", None), user_email=getattr(_user, "email", None))
    token = (payload.get("slack_bot_token") or "").strip()
    channel_id = (payload.get("slack_channel_id") or "").strip()

    if not token or "..." in token:
        token = db_items.get("slack_bot_token", getattr(settings, "slack_bot_token", ""))
    if not channel_id:
        channel_id = db_items.get("slack_channel_id", getattr(settings, "slack_channel_id", ""))

    if not token:
        return {"success": False, "error": "Slack Bot Token (xoxb-...) is required."}
    if not channel_id:
        return {"success": False, "error": "Slack Channel ID is required (e.g. C0C3N8E9807)."}

    from dataclasses import replace
    from app.slack_client import SlackClient
    test_cfg = replace(settings, slack_bot_token=token, slack_default_channel_id=channel_id, governor_notify_channel_id=channel_id)
    try:
        client = SlackClient(test_cfg)
        res = client.post_message(
            channel_id=channel_id,
            text="⚡ *AI Governor Health Check* — Slack bot connection verified successfully from the System Configuration panel.",
        )
        if res.get("ok") or res.get("sent"):
            return {
                "success": True,
                "message": f"Successfully connected to Slack and posted test ping to channel {channel_id}!",
                "channel_id": channel_id,
            }
        return {"success": False, "error": f"Slack API error: {res.get('error', 'unknown error')}"}
    except Exception as exc:
        log.exception("Slack test connection failed")
        return {"success": False, "error": str(exc)}



# @router.post("/test-zoho")
# def test_zoho_connection(
#     payload: dict[str, Any],
#     _user: CurrentUser = Depends(require_tab("repos")),
# ) -> dict[str, Any]:
#     """Test Zoho Desk OAuth token refresh and organization connection with provided credentials."""
#     return {"success": False, "error": "Zoho integration is currently disabled."}


