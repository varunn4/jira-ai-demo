"""Dynamic System Settings and Onboarding Validation Router."""

import logging
import os
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from requests.auth import HTTPBasicAuth

from app.app_settings import get_all_settings, get_all_user_settings, set_settings_bulk, set_user_settings_bulk
from app.auth import CurrentUser, require_tab
from app.config import reload_settings, settings
from app.llm_client import build_llm_client
from app.repository_discovery import discover_graph_repositories

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

    # 1. Repositories path
    repo_root = db_items.get("repository_search_root", settings.repository_search_root).strip() if db_items.get("repository_search_root") else ""
    if not repo_root or repo_root == ".":
        # Check if any git repo exists under current root
        repos = discover_graph_repositories(settings)
        if not repos:
            missing.append("repositories")
    else:
        p = Path(repo_root).expanduser().resolve()
        if not p.is_dir():
            missing.append("repositories")

    # 2. Jira credentials
    jira_url = db_items.get("jira_base_url", settings.jira_base_url)
    jira_email = db_items.get("jira_email", settings.jira_email)
    jira_token = db_items.get("jira_api_token", settings.jira_api_token)
    if not (jira_url and jira_email and jira_token):
        missing.append("jira")

    # 3. LLM Configuration
    provider = db_items.get("llm_provider", settings.llm_provider).strip().lower()
    openai_key = db_items.get("openai_api_key", settings.openai_api_key)
    anthropic_key = db_items.get("anthropic_api_key", settings.anthropic_api_key)
    if provider == "groq" or provider == "openai":
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

    # Setup is complete only if all mandatory connection sections are configured
    return {
        "setup_complete": len(missing) == 0,
        "missing_sections": missing,
        "repository_configured": "repositories" not in missing,
        "jira_configured": "jira" not in missing,
        "llm_configured": "llm" not in missing,
        "current_provider": provider,
        "current_repo_root": repo_root,
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
    db_items = get_all_user_settings(settings, user_id=getattr(_user, "id", None), user_email=getattr(_user, "email", None))

    return {
        "repository_search_root": db_items.get("repository_search_root", getattr(settings, "repository_search_root", ".")),
        "excluded_repository_names": db_items.get("excluded_repository_names", getattr(settings, "excluded_repository_names", "JIRA-AI")),
        "jira_base_url": db_items.get("jira_base_url", getattr(settings, "jira_base_url", "")),
        "jira_email": db_items.get("jira_email", getattr(settings, "jira_email", "")),
        "jira_api_token_masked": _mask_secret(db_items.get("jira_api_token", getattr(settings, "jira_api_token", ""))),
        "jira_has_token": bool(db_items.get("jira_api_token", getattr(settings, "jira_api_token", ""))),
        "jira_project_key": db_items.get("jira_project_key", db_items.get("jira_project_keys", getattr(settings, "jira_project_keys", ""))),
        "jira_excluded_project_keys": db_items.get("jira_excluded_project_keys", getattr(settings, "jira_excluded_project_keys", "")),
        "llm_provider": db_items.get("llm_provider", getattr(settings, "llm_provider", "anthropic")),
        "llm_model": db_items.get("llm_model", getattr(settings, "llm_model", "claude-sonnet-4-6")),
        "openai_base_url": db_items.get("openai_base_url", getattr(settings, "openai_base_url", "")),
        "openai_api_key_masked": _mask_secret(db_items.get("openai_api_key", getattr(settings, "openai_api_key", ""))),
        "openai_has_key": bool(db_items.get("openai_api_key", getattr(settings, "openai_api_key", ""))),
        "anthropic_api_key_masked": _mask_secret(db_items.get("anthropic_api_key", getattr(settings, "anthropic_api_key", ""))),
        "anthropic_has_key": bool(db_items.get("anthropic_api_key", getattr(settings, "anthropic_api_key", ""))),
        "anthropic_model": db_items.get("anthropic_model", getattr(settings, "anthropic_model", "")),
        "slack_bot_token_masked": _mask_secret(db_items.get("slack_bot_token", getattr(settings, "slack_bot_token", ""))),
        "slack_channel_id": db_items.get("slack_channel_id", getattr(settings, "slack_channel_id", "")),
        "qdrant_url": db_items.get("qdrant_url", getattr(settings, "qdrant_url", "")),
        "ollama_url": db_items.get("ollama_url", getattr(settings, "ollama_url", "")),
        "n8n_base_url": db_items.get("n8n_base_url", getattr(settings, "n8n_base_url", "")),
        "n8n_api_key_masked": _mask_secret(db_items.get("n8n_api_key", getattr(settings, "n8n_api_key", ""))),
        "n8n_has_key": bool(db_items.get("n8n_api_key", getattr(settings, "n8n_api_key", ""))),
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
    }

    for k, v in payload.items():
        if k in allowed_keys and v is not None:
            v_str = str(v).strip()
            # Do not overwrite with placeholder masked value
            if "..." in v_str and "masked" in k:
                continue
            if k == "repository_search_root":
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
        set_settings_bulk(settings, to_save)
        reload_settings()
        log.info("Dynamic settings saved and reloaded for user=%s: %s", getattr(_user, "email", "global"), list(to_save.keys()))

    return {"status": "ok", "message": "Settings saved successfully", "saved_keys": list(to_save.keys())}


@router.post("/validate-repo-path")
def validate_repo_path(
    payload: dict[str, Any],
    _user: CurrentUser = Depends(require_tab("repos")),
) -> dict[str, Any]:
    """Test a repository path on disk and return discovered git repositories."""
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        return {"valid": False, "error": "Path cannot be empty", "repo_count": 0, "repos": []}

    is_docker = os.path.exists("/.dockerenv") or Path("/host-repos").exists()

    # Detect Windows-style drive letter on a Linux / Docker host
    if is_docker and (len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha()):
        # Check if the folder exists inside /host-repos by basename or relative path
        folder_name = Path(raw_path.replace("\\", "/")).name
        alt_host_path = Path("/host-repos") / folder_name
        if alt_host_path.is_dir():
            path_obj = alt_host_path
        else:
            return {
                "valid": False,
                "error": (
                    f"You entered a Windows host path ('{raw_path}'). "
                    "Because Jira AI is running inside a Docker Linux container, it cannot directly access "
                    "Windows drive letters outside Docker's mounted volume. "
                    "Inside Docker, your mounted projects directory is at '/host-repos'. "
                    "To index repositories: clone/place them inside this project folder (accessed via '/host-repos'), "
                    "or set HOST_REPO_DIR in .env to your projects folder."
                ),
                "repo_count": 0,
                "repos": [],
            }
    else:
        try:
            path_obj = Path(raw_path).expanduser().resolve()
            # If path doesn't exist but is a relative name under /host-repos in Docker
            if is_docker and not path_obj.exists() and (Path("/host-repos") / raw_path.lstrip("/\\")).exists():
                path_obj = (Path("/host-repos") / raw_path.lstrip("/\\")).resolve()
        except Exception as exc:
            return {"valid": False, "error": f"Invalid path syntax: {exc}", "repo_count": 0, "repos": []}

    if not path_obj.exists():
        return {
            "valid": False,
            "error": f"Directory '{path_obj}' does not exist on the server/host.",
            "repo_count": 0,
            "repos": [],
        }

    if not path_obj.is_dir():
        return {
            "valid": False,
            "error": f"'{path_obj}' is a file, not a directory.",
            "repo_count": 0,
            "repos": [],
        }

    # Discover git repositories
    repos_found: list[dict[str, str]] = []
    excluded = {
        name.strip().lower()
        for name in settings.excluded_repository_names.split(",")
        if name.strip()
    }

    # Check if directory itself is a git repository
    if (path_obj / ".git").is_dir():
        if path_obj.name.lower() not in excluded:
            repos_found.append({"name": path_obj.name, "path": str(path_obj)})
    else:
        try:
            for child in sorted(path_obj.iterdir()):
                if child.is_dir() and (child / ".git").is_dir():
                    if child.name.lower() not in excluded:
                        repos_found.append({"name": child.name, "path": str(child)})
        except PermissionError:
            return {"valid": False, "error": f"Permission denied reading '{path_obj}'.", "repo_count": 0, "repos": []}

    if not repos_found:
        return {
            "valid": True,
            "repo_count": 0,
            "repos": [],
            "resolved_path": str(path_obj),
            "message": f"Directory exists at '{path_obj}', but contains no Git repositories (no subfolders with '.git').",
        }

    return {
        "valid": True,
        "repo_count": len(repos_found),
        "repos": [r["name"] for r in repos_found],
        "resolved_path": str(path_obj),
        "message": f"Successfully found {len(repos_found)} Git repository(ies) in '{path_obj}'.",
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

