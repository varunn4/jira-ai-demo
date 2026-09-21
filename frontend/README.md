# Jira AI Admin UI (React + RBAC)

React single-page app that replaces the old embedded-HTML admin page. It is
served by the FastAPI backend from `frontend/dist` at the site root (`/`).

## Stack

- React 18 + React Router 6
- Vite 5 build
- JWT auth + per-tab RBAC (enforced both client- and server-side)

## Structure

```
src/
  main.jsx              app bootstrap (Router + AuthProvider)
  App.jsx               route table
  auth.jsx              AuthContext: login/logout, current user, hasTab()
  api.js                fetch wrapper (adds JWT, handles 401, blob downloads)
  components/
    Header.jsx          top bar + nav (Dashboard / Documentation / Users)
    ProtectedRoute.jsx  ProtectedRoute / RequireTab / RequireAdmin guards
    JobSidebar.jsx      graph-job trigger controls
    JobProgress.jsx     stats cards + progress bars
  pages/
    Login.jsx
    AdminDashboard.jsx  sidebar + tab nav + active tab
    Documentation.jsx   standalone /docs-portal page (NOT a tab)
    Users.jsx           admin-only user management
  tabs/
    Repositories.jsx JiraTickets.jsx Insights.jsx Logs.jsx
    TestCases.jsx SimilarTickets.jsx
  lib/
    markdown.js         test-case / markdown renderer (port of the old JS)
    graphJob.js         job stats/progress derivation
    format.js           date / ETA helpers
```

## Routes

| Path           | Access                         | Purpose                          |
| -------------- | ------------------------------ | -------------------------------- |
| `/login`       | public                         | Sign in                          |
| `/`            | any authenticated user         | Dashboard (6 RBAC-filtered tabs) |
| `/docs-portal` | role with the `docs` tab       | Documentation portal (separate)  |
| `/users`       | `admin` only                   | User & role management           |

`Documentation` is intentionally **not** a dashboard tab — it has its own URL.

## Develop

```bash
npm install
npm run dev          # http://localhost:5173, proxies API to 127.0.0.1:8000
```

Run the FastAPI app (`uvicorn api:app --reload`) on port 8000 in parallel.

## Build (served by FastAPI)

```bash
npm run build        # -> frontend/dist
# or from repo root:
./scripts/build_frontend.sh
```

Then start FastAPI and open `http://127.0.0.1:8000/`.
