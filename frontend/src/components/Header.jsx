import { useState } from "react";
import { NavLink, useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import { hasAnyDashboardTab } from "../lib/tabs";
import logo from "../assets/logo.jpeg";
import SettingsModal from "./SettingsModal.jsx";

export default function Header({ title = "AI Admin", onSettingsSaved }) {
  const { user, logout, hasTab } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const showDashboard = hasAnyDashboardTab(hasTab);
  const canConfigure = hasTab("repos") || user?.role === "admin";
  const [settingsOpen, setSettingsOpen] = useState(false);

  function onLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <>
      <header className="app-header">
        <div className="app-header-left">
          <h1>
            <img className="app-logo" src={logo} alt="" />
            {title}
          </h1>
          <nav className="app-nav">
            {showDashboard && (
              <NavLink
                to="/dashboard/repositories"
                className={() =>
                  location.pathname.startsWith("/dashboard") ? "active" : ""
                }
              >
                Dashboard
              </NavLink>
            )}
            {hasTab("docs") && <NavLink to="/docs-portal">Documentation</NavLink>}
            {hasTab("users") && <NavLink to="/users">Users</NavLink>}
          </nav>
        </div>
        <div className="app-header-right">
          {canConfigure && (
            <button
              type="button"
              className="settings-nav-btn"
              onClick={() => setSettingsOpen(true)}
              title="Configure repositories, Jira, and AI models"
            >
              ⚙️ Settings
            </button>
          )}
          {user && (
            <span className="user-pill">
              <b>{user.email}</b>
              <span className="role-tag">{user.role}</span>
            </span>
          )}
          <button className="logout-btn" onClick={onLogout}>
            Sign out
          </button>
        </div>
      </header>

      {canConfigure && (
        <SettingsModal
          isOpen={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            if (onSettingsSaved) onSettingsSaved();
          }}
        />
      )}
    </>
  );
}

