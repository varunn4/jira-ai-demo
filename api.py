"""FastAPI entry point for Jira ticket analysis, workflows, and administration."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app import auth
from app.config import settings
from app.db import init_db
from app.repo_tree_integration import (
    initialize_repo_tree,
    register_repo_tree_routes,
    shutdown_repo_tree,
)
from app.routers import (
    analytics,
    docs,
    graph,
    jira,
    plugins,
    rca,
    settings as settings_router,
    system,
    workflows,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
    force=True,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Initialize RepoTree
    initialize_repo_tree()

    # Centralized database schema initialization
    try:
        init_db(settings)
    except Exception:
        log.exception("Centralized database schema initialization failed")

    # Auth schema initialization
    try:
        auth.ensure_auth_schema()
    except Exception:
        log.exception("Auth schema initialization failed")

    # Load dynamic overrides from DB
    try:
        settings.apply_dynamic_overrides()
    except Exception:
        log.exception("Dynamic settings load failed")

    # Document usage schema
    try:
        from app.repo_doc_usage import ensure_doc_usage_schema

        ensure_doc_usage_schema()
    except Exception:
        log.exception("Document usage schema initialization failed")

    try:
        yield
    finally:
        shutdown_repo_tree()


app = FastAPI(
    title="Jira AI Ticket Analyzer",
    version="0.2.0",
    description="Jira ticket analysis, Slack review workflow, and graph DB administration.",
    docs_url=None,
    lifespan=lifespan,
)

# ─── Register Routers ────────────────────────────────────────────────────────
register_repo_tree_routes(app)
app.include_router(auth.router)
app.include_router(settings_router.router)
app.include_router(system.router)
app.include_router(workflows.router)
app.include_router(jira.router)
app.include_router(rca.router)
app.include_router(graph.router)
app.include_router(docs.router)
app.include_router(analytics.router)
# app.include_router(plugins.router)  # Ring Studio & Zoho Desk (Commented out)

# ─── Static & Assets ─────────────────────────────────────────────────────────
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "app" / "static"),
    name="static",
)

# ─── React SPA (built by Vite into frontend/dist) ────────────────────────────
FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"
if (FRONTEND_DIST / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="spa-assets",
    )

_SPA_MISSING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Jira AI Admin</title></head><body style="font-family:sans-serif;padding:40px">
<h1>Frontend not built</h1>
<p>The React SPA has not been built yet. Run:</p>
<pre>python scripts/build_frontend.py</pre>
<p>Then reload this page. The API itself is running normally.</p>
</body></html>"""


def _serve_spa() -> Response:
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return HTMLResponse(_SPA_MISSING_HTML, status_code=200)


@app.get("/docs", include_in_schema=False)
def swagger_ui_html() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
    )


# ─── Admin UI (React SPA) Root & Catch-all ───────────────────────────────────

@app.get("/", include_in_schema=False)
def spa_root() -> Response:
    return _serve_spa()


@app.get("/graph-admin", include_in_schema=False)
def spa_graph_admin() -> Response:
    return _serve_spa()


_API_PATH_PREFIXES = (
    "api/", "graph-admin", "auth/", "static/", "assets/", "analyze-ticket",
    "workflow", "scan/", "repomix/", "testcases/", "jobs", "repo-tree",
    "prompts", "chat", "health", "openapi.json", "rca/",
    # "ring-studio", "zoho/",  # (Commented out)
)


@app.get("/{full_path:path}", include_in_schema=False)
def spa_catch_all(full_path: str) -> Response:
    # Unknown API paths should 404 as JSON-ish, not silently return the SPA shell.
    if full_path.startswith(_API_PATH_PREFIXES):
        raise HTTPException(status_code=404, detail="Not found")
    return _serve_spa()
