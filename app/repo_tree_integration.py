"""In-process RepoTree integration for the JIRA-AI API service.

RepoTree is bundled into JIRA-AI as the local `repo_architect` package plus a
`repo_tree/` config/workspace directory. This module initializes that bundled
copy and exposes its routes without running a second uvicorn service.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

log = logging.getLogger(__name__)


_repo_tree_source_path: Path | None = None
_repo_tree_import_error: Exception | None = None
_repo_tree_init_error: Exception | None = None
_repo_tree_initialized = False

_repo_tree_app: Any = None
_repo_tree_state: Any = None
_load_config: Any = None
_job_store_cls: Any = None
_llm_client_cls: Any = None
_generate_request_cls: Any = None
_generate_testcases_endpoint: Any = None


def _candidate_source_paths() -> list[Path]:
    current_file = Path(__file__).resolve()
    project_root = current_file.parents[1]
    candidates: list[Path] = []

    candidates.extend(
        [
            project_root,
            project_root / "repo_tree" / "src",
        ]
    )

    env_src = os.getenv("REPO_TREE_SRC_PATH", "").strip()
    if env_src:
        candidates.append(Path(env_src).expanduser())

    env_root = os.getenv("REPO_TREE_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root).expanduser() / "src")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _discover_source_path() -> Path | None:
    global _repo_tree_source_path
    if _repo_tree_source_path is not None:
        return _repo_tree_source_path

    for source_path in _candidate_source_paths():
        if (source_path / "repo_architect" / "api" / "app.py").exists():
            _repo_tree_source_path = source_path.resolve()
            if str(_repo_tree_source_path) not in sys.path:
                sys.path.insert(0, str(_repo_tree_source_path))
            return _repo_tree_source_path

    return None


def _import_repo_tree() -> bool:
    global _repo_tree_import_error
    global _repo_tree_app, _repo_tree_state, _load_config
    global _job_store_cls, _llm_client_cls
    global _generate_request_cls, _generate_testcases_endpoint

    if _repo_tree_app is not None:
        return True

    source_path = _discover_source_path()
    if source_path is None:
        _repo_tree_import_error = FileNotFoundError(
            "Bundled RepoTree source not found. Set REPO_TREE_SRC_PATH to the directory containing repo_architect."
        )
        return False

    try:
        from repo_architect.api.app import app as repo_tree_app
        from repo_architect.api.app import generate_testcases
        from repo_architect.api.app import state as repo_tree_state
        from repo_architect.api.jobs import JobStore
        from repo_architect.api.models import GenerateTestCasesRequest
        from repo_architect.config import load_config
        from repo_architect.llm import LLMClient
    except Exception as exc:  # pragma: no cover - depends on runtime deps
        _repo_tree_import_error = exc
        log.warning("RepoTree import failed: %s", exc)
        return False

    _repo_tree_app = repo_tree_app
    _repo_tree_state = repo_tree_state
    _load_config = load_config
    _job_store_cls = JobStore
    _llm_client_cls = LLMClient
    _generate_request_cls = GenerateTestCasesRequest
    _generate_testcases_endpoint = generate_testcases
    _repo_tree_import_error = None
    return True


def _repo_tree_root() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    if (project_root / "repo_tree").exists():
        return (project_root / "repo_tree").resolve()

    env_root = os.getenv("REPO_TREE_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()

    source_path = _discover_source_path()
    if source_path is not None:
        if (source_path / "repo_tree").exists():
            return (source_path / "repo_tree").resolve()
        return source_path.parent.resolve()

    return (Path(__file__).resolve().parents[1] / "repo_tree").resolve()


def _config_path() -> Path:
    env_path = os.getenv("REPO_TREE_CONFIG_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    root = _repo_tree_root()
    default_config = root / "config" / "repos.yaml"
    if default_config.exists():
        return default_config.resolve()

    return Path("config/repos.yaml").resolve()


def _workspace_dir() -> Path:
    env_path = os.getenv("REPO_TREE_WORKSPACE_DIR", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (_repo_tree_root() / "workspace").resolve()


def initialize_repo_tree() -> bool:
    """Load RepoTree config, job store, and LLM client for in-process use."""
    global _repo_tree_initialized, _repo_tree_init_error

    if _repo_tree_initialized:
        return True

    if not _import_repo_tree():
        return False

    config_path = _config_path()
    workspace_dir = _workspace_dir()
    root = _repo_tree_root()

    if not config_path.exists():
        _repo_tree_init_error = FileNotFoundError(
            f"RepoTree config not found at {config_path}. Set REPO_TREE_CONFIG_PATH."
        )
        log.warning("%s", _repo_tree_init_error)
        return False

    cwd = Path.cwd()
    try:
        from app.config import settings as app_settings
        from app.repository_discovery import discover_graph_repositories
        from repo_architect.config import Config, RepoConfig

        cfg = None
        if config_path.exists():
            try:
                os.chdir(root)
                cfg = _load_config(config_path)
            except Exception as exc:
                log.debug("repos.yaml load skipped (%s), falling back to dynamic repo discovery", exc)

        if cfg is None or not getattr(cfg, "repos", None):
            discovered = discover_graph_repositories(app_settings)
            repo_configs = []
            seen_names = set()
            for r in discovered:
                name = r.get("name")
                p = r.get("container_path") or r.get("path")
                if name and p and name not in seen_names:
                    seen_names.add(name)
                    p_path = Path(p)
                    if p_path.exists():
                        try:
                            repo_configs.append(
                                RepoConfig(
                                    name=name,
                                    path=p_path,
                                    description=f"Git repository {name}",
                                )
                            )
                        except Exception as exc:
                            log.debug("Skipping dynamic repo %s: %s", name, exc)

            cfg = Config(
                model=app_settings.llm_model or "claude-sonnet-4-6",
                use_skeleton=True,
                repos=repo_configs,
            )

        cfg.workspace_dir = workspace_dir
        cfg.ensure_dirs()

        _repo_tree_state.config = cfg
        _repo_tree_state.job_store = _job_store_cls(cfg.workspace_dir / "jobs.json")
        try:
            _repo_tree_state.llm = _llm_client_cls(
                model=cfg.model,
                max_tokens=cfg.max_output_tokens,
            )
        except Exception as exc:
            log.debug("RepoTree internal LLM init deferred: %s", exc)
    except Exception as exc:  # pragma: no cover - integration startup guard
        _repo_tree_init_error = exc
        log.exception("RepoTree initialization failed")
        return False
    finally:
        os.chdir(cwd)

    _repo_tree_initialized = True
    _repo_tree_init_error = None
    log.info(
        "RepoTree integrated in-process: config=%s workspace=%s repos=%d",
        config_path,
        workspace_dir,
        len(_repo_tree_state.config.repos),
    )
    return True


def shutdown_repo_tree() -> None:
    """Stop RepoTree background workers when the JIRA-AI service shuts down."""
    global _repo_tree_initialized

    if not _import_repo_tree():
        return

    store = getattr(_repo_tree_state, "job_store", None)
    executor = getattr(store, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=False)

    _repo_tree_state.config = None
    _repo_tree_state.job_store = None
    _repo_tree_state.llm = None
    _repo_tree_initialized = False


def register_repo_tree_routes(app: FastAPI) -> bool:
    """Register RepoTree API routes on the JIRA-AI FastAPI app.

    The root RepoTree paths are preserved:
      /scan/initial, /scan/nightly, /repomix/reindex, /testcases/generate,
      /jobs, and /jobs/{job_id}.
    """
    if getattr(app.state, "repo_tree_routes_registered", False):
        return True

    if not _import_repo_tree():
        return False

    registered = 0
    for route in _repo_tree_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == "/health":
            continue
        app.router.routes.append(route)
        registered += 1

    app.add_api_route(
        "/repo-tree/health",
        repo_tree_health,
        methods=["GET"],
        tags=["repo-tree"],
        summary="RepoTree integration health.",
    )
    app.add_exception_handler(FileNotFoundError, _repo_tree_file_not_found)
    app.state.repo_tree_routes_registered = True
    log.info("Registered %d RepoTree routes on the JIRA-AI app", registered)
    return True


def repo_tree_status() -> dict[str, Any]:
    """Return integration status suitable for JIRA-AI /health."""
    available = _import_repo_tree()
    status_payload: dict[str, Any] = {
        "mode": "in_process" if available else "unavailable",
        "source_path": str(_repo_tree_source_path) if _repo_tree_source_path else None,
        "config_path": str(_config_path()) if available else None,
        "workspace_dir": str(_workspace_dir()) if available else None,
        "initialized": _repo_tree_initialized,
    }
    if _repo_tree_import_error is not None:
        status_payload["import_error"] = str(_repo_tree_import_error)
    if _repo_tree_init_error is not None:
        status_payload["init_error"] = str(_repo_tree_init_error)
    if _repo_tree_initialized and _repo_tree_state.config is not None:
        status_payload["repos_configured"] = len(_repo_tree_state.config.repos)
        status_payload["model"] = _repo_tree_state.config.model
    return status_payload


def repo_tree_health() -> dict[str, Any]:
    if not initialize_repo_tree():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=repo_tree_status(),
        )

    return {
        "status": "ok",
        "integration": "in_process",
        "repos_configured": len(_repo_tree_state.config.repos),
        "model": _repo_tree_state.config.model,
        "workspace_dir": str(_repo_tree_state.config.workspace_dir),
    }


def repo_tree_runtime() -> Any:
    """Return the initialized in-process RepoTree state (exposes `.config` and `.llm`).

    Raises RuntimeError if RepoTree could not be initialized.
    """
    if not initialize_repo_tree():
        raise RuntimeError(f"RepoTree is not initialized: {repo_tree_status()}")
    return _repo_tree_state


def generate_testcases_in_process(payload: dict[str, Any]) -> dict[str, Any]:
    """Call RepoTree's testcase endpoint function without an HTTP hop."""
    if not initialize_repo_tree():
        raise RuntimeError(f"RepoTree is not initialized: {repo_tree_status()}")

    try:
        request = _generate_request_cls.model_validate(payload)
        response = _generate_testcases_endpoint(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    if hasattr(response, "model_dump"):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    raise RuntimeError(f"Unexpected RepoTree response type: {type(response).__name__}")


async def _repo_tree_file_not_found(request: Any, exc: FileNotFoundError) -> JSONResponse:
    path = getattr(getattr(request, "url", None), "path", "")
    hint = (
        " Run /scan/initial first."
        if path.startswith(("/scan", "/repomix", "/testcases", "/jobs", "/repo-tree"))
        else ""
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"missing file: {exc}.{hint}"},
    )
