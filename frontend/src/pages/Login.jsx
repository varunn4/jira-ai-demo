import { useState } from "react";
import { useNavigate, useLocation, Navigate } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import logo from "../assets/logo.jpeg";

export default function Login() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (user) {
    return <Navigate to="/dashboard/repositories" replace />;
  }

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(email.trim(), password);
      const dest = location.state?.from || "/dashboard/repositories";
      navigate(dest, { replace: true });
    } catch (err) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>
          <img className="app-logo" src={logo} alt="" />
          AI Governor Platform
        </h1>
        <p className="sub">Enterprise Jira & Codebase Intelligence</p>

        <div style={{
          background: "rgba(56, 189, 248, 0.08)",
          border: "1px solid rgba(56, 189, 248, 0.2)",
          borderRadius: "8px",
          padding: "10px 12px",
          fontSize: "12px",
          color: "var(--muted, #94a3b8)",
          lineHeight: "1.4",
          marginBottom: "16px",
          textAlign: "left"
        }}>
          💡 <b>First time?</b> Enter your email and chosen password (min. 6 characters) to automatically initialize your user account.
        </div>

        <div className="tc-field">
          <label className="tc-label">Work Email</label>
          <input
            className="tc-input"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="engineer@company.com"
            required
          />
        </div>
        <div className="tc-field">
          <label className="tc-label">Password</label>
          <input
            className="tc-input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="•••••••• (min. 6 characters)"
            minLength={6}
            required
          />
        </div>

        {error && <div className="login-error" style={{ marginBottom: "12px" }}>{error}</div>}

        <button type="submit" disabled={busy} style={{ width: "100%", marginTop: "4px" }}>
          {busy ? "Authenticating…" : "Sign In / Continue"}
        </button>

        <div style={{ marginTop: "18px", paddingTop: "14px", borderTop: "1px dashed var(--line)", textAlign: "center" }}>
          <button
            type="button"
            className="action-btn"
            style={{ width: "100%", fontSize: "12px", minHeight: "34px", opacity: 0.85 }}
            onClick={() => {
              setEmail("admin@yourcompany.com");
              setPassword("admin123");
              setError("");
            }}
          >
            🔑 Fill Default Admin Credentials
          </button>
        </div>
      </form>
    </div>
  );
}

