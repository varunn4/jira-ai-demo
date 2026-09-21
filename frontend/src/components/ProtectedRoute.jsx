import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth.jsx";

export function ProtectedRoute({ children }) {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return children;
}

export function RequireTab({ tab, children }) {
  const { user, hasTab } = useAuth();
  const location = useLocation();
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (!hasTab(tab)) {
    return <Forbidden />;
  }
  return children;
}

export function RequireAdmin({ children }) {
  const { user, isAdmin } = useAuth();
  const location = useLocation();
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (!isAdmin) {
    return <Forbidden />;
  }
  return children;
}

function Forbidden() {
  return (
    <div className="app-loading" style={{ flexDirection: "column", gap: 8 }}>
      <h2 style={{ margin: 0 }}>403 — Access denied</h2>
      <p>Your role does not have permission to view this page.</p>
      <a href="/">Back to dashboard</a>
    </div>
  );
}
