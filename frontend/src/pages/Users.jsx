import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { useAuth } from "../auth.jsx";
import Header from "../components/Header.jsx";

const fmtTokens = (n) => Number(n || 0).toLocaleString();

// Costs are stored in USD; displayed in INR using the backend's USD_TO_INR rate.
let usdToInr = 95.75;
const fmtCost = (n) =>
  `₹${(Number(n || 0) * usdToInr).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

export default function Users() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState([]);
  const [roleTabs, setRoleTabs] = useState({});
  const [usage, setUsage] = useState({}); // email -> { generations, input_tokens, output_tokens, cost_usd }
  const [usageTotals, setUsageTotals] = useState({});
  const [banner, setBanner] = useState({ msg: "", cls: "" });
  const [form, setForm] = useState({ email: "", password: "", role: "viewer" });
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [usersData, rolesData] = await Promise.all([
        apiFetch("/auth/users"),
        apiFetch("/auth/roles"),
      ]);
      setUsers(usersData);
      setRoles(rolesData.roles || []);
      setRoleTabs(rolesData.role_tabs || {});
      if (rolesData.roles?.length && !rolesData.roles.includes(form.role)) {
        setForm((f) => ({ ...f, role: rolesData.roles[0] }));
      }
    } catch (err) {
      setBanner({ msg: err.message, cls: "error" });
    }
    // Document-generation usage is best-effort; never block user management on it.
    try {
      const u = await apiFetch("/graph-admin/repo-docs/usage?limit=1");
      if (u.usd_to_inr) usdToInr = u.usd_to_inr;
      const map = {};
      (u.by_user || []).forEach((row) => {
        if (row.user_email) map[row.user_email] = row;
      });
      setUsage(map);
      setUsageTotals(u.totals || {});
    } catch {
      /* usage unavailable (e.g. no DB) — leave columns blank */
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function createUser(e) {
    e.preventDefault();
    setBusy(true);
    setBanner({ msg: "", cls: "" });
    try {
      await apiFetch("/auth/users", {
        method: "POST",
        body: { email: form.email.trim(), password: form.password, role: form.role, is_active: true },
      });
      setBanner({ msg: `Created ${form.email.trim()}`, cls: "ok" });
      setForm((f) => ({ ...f, email: "", password: "" }));
      load();
    } catch (err) {
      setBanner({ msg: err.message, cls: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function changeRole(u, role) {
    try {
      await apiFetch(`/auth/users/${u.id}`, { method: "PATCH", body: { role } });
      load();
    } catch (err) {
      setBanner({ msg: err.message, cls: "error" });
    }
  }

  async function toggleActive(u) {
    try {
      await apiFetch(`/auth/users/${u.id}`, { method: "PATCH", body: { is_active: !u.is_active } });
      load();
    } catch (err) {
      setBanner({ msg: err.message, cls: "error" });
    }
  }

  async function resetPassword(u) {
    const pw = window.prompt(`New password for ${u.email} (min 6 chars):`);
    if (!pw) return;
    try {
      await apiFetch(`/auth/users/${u.id}`, { method: "PATCH", body: { password: pw } });
      setBanner({ msg: `Password updated for ${u.email}`, cls: "ok" });
    } catch (err) {
      setBanner({ msg: err.message, cls: "error" });
    }
  }

  async function removeUser(u) {
    if (!window.confirm(`Delete user ${u.email}? This cannot be undone.`)) return;
    try {
      await apiFetch(`/auth/users/${u.id}`, { method: "DELETE" });
      load();
    } catch (err) {
      setBanner({ msg: err.message, cls: "error" });
    }
  }

  return (
    <>
      <Header title="AI Admin" />
      <div className="page">
        <h2>User management</h2>
        <p className="sub">
          Create users and assign roles. Each role grants access to a fixed set of tabs (per-tab RBAC).
        </p>

        {banner.msg && <div className={`banner ${banner.cls}`}>{banner.msg}</div>}

        <form
          onSubmit={createUser}
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(200px, 1.8fr) minmax(160px, 1.4fr) minmax(130px, 1.2fr) auto",
            alignItems: "end",
            gap: "12px",
            background: "var(--card)",
            border: "1px solid var(--line)",
            borderRadius: "var(--r)",
            padding: "16px 18px",
            marginBottom: "20px",
            boxShadow: "var(--shadow-xs)",
          }}
        >
          <div className="tc-field" style={{ margin: 0 }}>
            <label className="tc-label" style={{ fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.04em", color: "var(--muted)" }}>Email</label>
            <input
              className="tc-input"
              type="email"
              name="new_user_create_email"
              autoComplete="off"
              data-lpignore="true"
              required
              value={form.email}
              onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
              placeholder="user@example.com"
              style={{ height: 38, minHeight: 38, padding: "6px 12px", fontSize: 13 }}
            />
          </div>
          <div className="tc-field" style={{ margin: 0 }}>
            <label className="tc-label" style={{ fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.04em", color: "var(--muted)" }}>Password</label>
            <input
              className="tc-input"
              type="password"
              name="new_user_create_password"
              autoComplete="new-password"
              data-lpignore="true"
              required
              minLength={6}
              value={form.password}
              onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
              placeholder="min 6 characters"
              style={{ height: 38, minHeight: 38, padding: "6px 12px", fontSize: 13 }}
            />
          </div>
          <div className="tc-field" style={{ margin: 0 }}>
            <label className="tc-label" style={{ fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.04em", color: "var(--muted)" }}>Role</label>
            <select
              className="tc-select"
              value={form.role}
              onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
              style={{ height: 38, minHeight: 38, padding: "6px 28px 6px 10px", fontSize: 13 }}
            >
              {roles.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={busy}
            style={{
              width: "auto",
              height: 38,
              minHeight: 38,
              padding: "0 22px",
              fontSize: 13,
              fontWeight: 650,
              whiteSpace: "nowrap",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            {busy ? "Adding..." : "+ Add User"}
          </button>
        </form>

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", margin: "16px 0 10px", flexWrap: "wrap", gap: 10 }}>
          <p className="tc-section-label" style={{ margin: 0 }}>
            Platform Usage & Cost Intelligence
          </p>
          <div className="tc-stats" style={{ margin: 0 }}>
            <span className="tc-stat"><span>{fmtTokens(usageTotals.generations)}</span> docs</span>
            <span className="tc-stat"><span>{fmtTokens(usageTotals.reused_count)}</span> reused</span>
            <span className="tc-stat"><span>{fmtTokens(usageTotals.input_tokens)}</span> input</span>
            <span className="tc-stat"><span>{fmtTokens(usageTotals.output_tokens)}</span> output</span>
            <span className="tc-stat"><span>{fmtCost(usageTotals.cost_usd)}</span> total cost</span>
          </div>
        </div>

        <table style={{ width: "100%", tableLayout: "fixed" }}>
          <thead>
            <tr>
              <th style={{ width: "24%" }}>User</th>
              <th style={{ width: "14%" }}>Role</th>
              <th style={{ width: "7%", textAlign: "right" }}>Docs</th>
              <th style={{ width: "10%", textAlign: "right" }}>Input Tok</th>
              <th style={{ width: "10%", textAlign: "right" }}>Output Tok</th>
              <th style={{ width: "9%", textAlign: "right" }}>Cost</th>
              <th style={{ width: "8%", textAlign: "center" }}>Status</th>
              <th style={{ width: "18%", textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const usg = usage[u.email] || {};
              const isSelf = u.id === me?.id;
              const isCorpAdmin = typeof u.email === "string" && u.email.toLowerCase().endsWith("@aonamitech.com");
              return (
                <tr key={u.id}>
                  <td>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <b style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 200 }} title={u.email}>
                        {u.email}
                      </b>
                      {isSelf && <span className="role-tag" style={{ fontSize: 10, padding: "1px 6px" }}>you</span>}
                      {isCorpAdmin && (
                        <span
                          className="role-tag"
                          style={{
                            fontSize: 10,
                            padding: "1px 6px",
                            background: "rgba(37, 99, 235, 0.12)",
                            color: "var(--primary, #2563eb)",
                            borderColor: "rgba(37, 99, 235, 0.3)",
                            fontWeight: 650,
                          }}
                          title="Corporate domain account with automatic Admin access"
                        >
                          org admin
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 240, marginTop: 2 }} title={(u.permissions || roleTabs[u.role] || []).join(", ")}>
                      {(u.permissions || roleTabs[u.role] || []).join(", ") || "—"}
                    </div>
                  </td>
                  <td>
                    <select
                      className="tc-select"
                      value={isCorpAdmin ? "admin" : u.role}
                      onChange={(e) => changeRole(u, e.target.value)}
                      disabled={isSelf || isCorpAdmin}
                      title={isCorpAdmin ? "Accounts with @aonamitech.com domain are always admins" : "Change role"}
                      style={{ height: 30, minHeight: 30, padding: "2px 24px 2px 8px", fontSize: 12, width: "100%", maxWidth: 120 }}
                    >
                      {roles.map((r) => (
                        <option key={r} value={r}>{r}</option>
                      ))}
                      {!roles.includes(u.role) && <option value={u.role}>{u.role}</option>}
                    </select>
                  </td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{fmtTokens(usg.generations)}</td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{fmtTokens(usg.input_tokens)}</td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{fmtTokens(usg.output_tokens)}</td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>{fmtCost(usg.cost_usd)}</td>
                  <td style={{ textAlign: "center" }}>
                    <span className={`badge ${u.is_active ? "ok" : "err"}`} style={{ fontSize: 10.5, padding: "2px 8px" }}>
                      {u.is_active ? "active" : "disabled"}
                    </span>
                  </td>
                  <td>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 5 }}>
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => resetPassword(u)}
                        style={{ width: "auto", height: 28, minHeight: 28, padding: "0 8px", fontSize: 11.5, fontWeight: 600 }}
                        title="Reset user password"
                      >
                        Reset PW
                      </button>
                      {!isSelf && (
                        <>
                          <button
                            type="button"
                            className="secondary"
                            onClick={() => toggleActive(u)}
                            style={{ width: "auto", height: 28, minHeight: 28, padding: "0 8px", fontSize: 11.5, fontWeight: 600 }}
                          >
                            {u.is_active ? "Disable" : "Enable"}
                          </button>
                          <button
                            type="button"
                            className="danger"
                            onClick={() => removeUser(u)}
                            style={{ width: "auto", height: 28, minHeight: 28, padding: "0 8px", fontSize: 11.5, fontWeight: 600, background: "#ef4444" }}
                          >
                            Delete
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
