import { Navigate } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import { hasAnyDashboardTab } from "../lib/tabs";
import AdminDashboard from "./AdminDashboard.jsx";

// Smart landing page: sends each role to its "main page".
// - has any dashboard tab  -> the dashboard
// - documentation-only      -> the Documentation portal
// - user-management-only     -> the Users page
export default function Home() {
  const { hasTab } = useAuth();

  if (hasAnyDashboardTab(hasTab)) return <AdminDashboard />;
  if (hasTab("docs")) return <Navigate to="/docs-portal" replace />;
  if (hasTab("users")) return <Navigate to="/users" replace />;
  // No access at all — show the dashboard's empty state.
  return <AdminDashboard />;
}
