import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./auth.jsx";
import Login from "./pages/Login.jsx";
import Home from "./pages/Home.jsx";
import Documentation from "./pages/Documentation.jsx";
import Users from "./pages/Users.jsx";
import { ProtectedRoute, RequireTab } from "./components/ProtectedRoute.jsx";

export default function App() {
  const { loading } = useAuth();
  if (loading) {
    return <div className="app-loading">Loading…</div>;
  }
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      <Route path="/" element={<Navigate to="/dashboard/repositories" replace />} />
      <Route
        path="/dashboard"
        element={<Navigate to="/dashboard/repositories" replace />}
      />
      <Route
        path="/dashboard/:tab"
        element={
          <ProtectedRoute>
            <Home />
          </ProtectedRoute>
        }
      />
      <Route path="/graph-admin" element={<Navigate to="/dashboard/repositories" replace />} />

      {/* Documentation lives at its own URL — not a tab in the main page. */}
      <Route
        path="/docs-portal"
        element={
          <RequireTab tab="docs">
            <Documentation />
          </RequireTab>
        }
      />

      <Route
        path="/users"
        element={
          <RequireTab tab="users">
            <Users />
          </RequireTab>
        }
      />

      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
