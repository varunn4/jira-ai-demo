import { useState } from "react";
import { NavLink, useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import { hasAnyDashboardTab } from "../lib/tabs";
import { apiFetch } from "../api";
import logo from "../assets/logo.png";
import SettingsModal from "./SettingsModal.jsx";
import { Button } from "./ui/button";
import {
  Settings,
  LogOut,
  User,
  LayoutDashboard,
  BookOpen,
  Users,
  RefreshCw,
} from "lucide-react";

export default function Header({ title = "AI Admin", onSettingsSaved }) {
  const { user, logout, hasTab } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const showDashboard = hasAnyDashboardTab(hasTab);
  const canConfigure = hasTab("repos") || user?.role === "admin";
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncStatus, setSyncStatus] = useState(null);

  async function onSyncRepos() {
    setSyncing(true);
    setSyncStatus(null);
    try {
      const saved = await apiFetch("/api/settings");
      const res = await apiFetch("/api/settings/validate-github", {
        method: "POST",
        body: {
          source_type: saved.github_source_type || "org",
          github_org_or_user: saved.github_org_or_user || "",
          github_repo_urls: saved.github_repo_urls || "",
        },
      });
      setSyncStatus(
        res.valid
          ? { ok: true, msg: `Synced ${res.repo_count} repositories from GitHub` }
          : { ok: false, msg: res.error || "GitHub sync failed" }
      );
      if (res.valid && onSettingsSaved) onSettingsSaved();
    } catch (err) {
      setSyncStatus({ ok: false, msg: err.message || "GitHub sync failed" });
    } finally {
      setSyncing(false);
    }
  }

  function onLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <>
      <header className="app-header">
        <div className="app-header-left">
          <div className="app-brand">
            <img className="app-logo" src={logo} alt="Logo" />
            <span className="app-brand-title">{title}</span>
          </div>
          <nav className="app-nav">
            {showDashboard && (
              <NavLink
                to="/dashboard/repositories"
                className={() =>
                  location.pathname.startsWith("/dashboard") ? "active" : ""
                }
              >
                <LayoutDashboard size={14} />
                <span>Dashboard</span>
              </NavLink>
            )}
            {hasTab("docs") && (
              <NavLink to="/docs-portal">
                <BookOpen size={14} />
                <span>Documentation</span>
              </NavLink>
            )}
            {hasTab("users") && (
              <NavLink to="/users">
                <Users size={14} />
                <span>Users</span>
              </NavLink>
            )}
          </nav>
        </div>
        <div className="app-header-right">
          {canConfigure && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              icon={RefreshCw}
              iconSize={14}
              onClick={onSyncRepos}
              loading={syncing}
              loadingText="Refreshing..."
              disabled={syncing}
              title={syncStatus?.msg || "Sync repositories from GitHub"}
              className={`h-8 px-3 text-xs font-semibold hover:bg-accent/30 hover:text-foreground ${
                syncStatus && !syncStatus.ok ? "text-destructive" : ""
              }`}
            >
              Refresh
            </Button>
          )}
          {canConfigure && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              icon={Settings}
              iconSize={14}
              onClick={() => setSettingsOpen(true)}
              title="Configure repositories, Jira, and AI models"
              className="h-8 px-3 text-xs font-semibold"
            >
              Settings
            </Button>
          )}
          {user && (
            <span className="user-pill">
              <User size={13} style={{ color: "var(--muted-foreground)" }} />
              <b>{user.email}</b>
              <span className="role-tag">{user.role}</span>
            </span>
          )}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            icon={LogOut}
            iconSize={14}
            onClick={onLogout}
            className="h-8 px-3 text-xs font-medium text-muted-foreground hover:text-foreground"
          >
            Sign out
          </Button>
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

