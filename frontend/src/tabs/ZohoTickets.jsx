// Zoho Tickets — enter a customer's email/phone, fetch and display their Zoho
// Desk tickets. Backs the "zoho" capability. The same POST /zoho/tickets API is
// intended to be reused by the customer portal (see the design doc).
import { useEffect, useState } from "react";
import { apiFetch } from "../api.js";
import { fmtDate } from "../lib/format.js";
import { Button } from "../components/ui/button.jsx";
import {
  Ticket,
  CheckCircle2,
  AlertCircle,
  Settings,
  X,
  XCircle,
  ExternalLink,
  Lock,
  Zap,
  Save,
  Paperclip,
  HelpCircle,
  Search,
  KeyRound,
} from "lucide-react";

// Zoho status → badge class. Anything unmapped falls back to a neutral pill.
function statusClass(status) {
  const s = (status || "").toLowerCase();
  if (s === "closed") return "badge idle";
  if (s === "open") return "badge run";
  if (s === "on hold" || s === "escalated") return "badge warn";
  return "badge ok";
}

function priorityClass(priority) {
  const p = (priority || "").toLowerCase();
  if (p === "high" || p === "urgent") return "badge err";
  if (p === "medium") return "badge warn";
  return "badge idle";
}

export default function ZohoTickets() {
  const [configured, setConfigured] = useState(null); // null = unknown yet
  const [showConfigModal, setShowConfigModal] = useState(false);
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null); // { configured, contact, tickets, message }
  const [openTicket, setOpenTicket] = useState(null); // row clicked → detail modal

  const checkStatus = () => {
    apiFetch("/zoho/status")
      .then((d) => setConfigured(!!d.configured))
      .catch(() => setConfigured(false));
  };

  // Probe configuration once so we can warn before the operator wastes a lookup.
  useEffect(() => {
    checkStatus();
  }, []);

  const fetchTickets = async (e) => {
    e?.preventDefault();
    if (!email.trim() && !phone.trim()) {
      setError("Enter a customer email or phone number.");
      return;
    }
    setLoading(true);
    setError("");
    setResult(null);
    setOpenTicket(null);
    try {
      const data = await apiFetch("/zoho/tickets", {
        method: "POST",
        body: { email: email.trim() || null, phone: phone.trim() || null },
      });
      setResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const tickets = result?.tickets || [];

  return (
    <div className="zoho-tab">
      <header className="zoho-hero" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: "12px" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "4px" }}>
            <h2 style={{ margin: 0, display: "flex", alignItems: "center", gap: "8px" }}>
              <Ticket size={22} color="var(--primary)" />
              Zoho Tickets
            </h2>
            {configured === true && (
              <span className="badge ok" style={{ fontSize: "11px", fontWeight: 700, padding: "2px 8px", display: "inline-flex", alignItems: "center", gap: "4px" }}>
                <CheckCircle2 size={12} /> Connected
              </span>
            )}
            {configured === false && (
              <span className="badge warn" style={{ fontSize: "11px", fontWeight: 700, padding: "2px 8px", display: "inline-flex", alignItems: "center", gap: "4px" }}>
                <AlertCircle size={12} /> Not Configured
              </span>
            )}
          </div>
          <p className="muted" style={{ margin: 0, maxWidth: "600px" }}>
            Enter a customer's email or phone number to look them up in Zoho Desk and view their support tickets and current statuses.
          </p>
        </div>

        <Button
          type="button"
          variant="outline"
          size="sm"
          icon={Settings}
          iconSize={14}
          onClick={() => setShowConfigModal(true)}
        >
          Configure Credentials
        </Button>
      </header>

      {configured === false && (
        <div className="error-banner" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "10px", marginTop: "12px" }}>
          <div>
            <strong>Zoho Desk is not configured:</strong> Please configure your Zoho OAuth Client ID, Secret, Refresh Token, and Org ID.
          </div>
          <Button
            type="button"
            variant="default"
            size="sm"
            icon={Settings}
            iconSize={14}
            onClick={() => setShowConfigModal(true)}
          >
            Configure Now
          </Button>
        </div>
      )}

      <form className="zoho-form card" onSubmit={fetchTickets}>
        <div className="zoho-fields">
          <label className="zoho-field">
            <span>Customer email</span>
            <input
              className="tc-input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="customer@example.com"
              autoComplete="off"
            />
          </label>
          <label className="zoho-field">
            <span>Phone (optional)</span>
            <input
              className="tc-input"
              type="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+91…"
              autoComplete="off"
            />
          </label>
          <Button
            type="submit"
            variant="primary"
            loading={loading}
            loadingText="Fetching…"
            icon={Search}
            iconSize={14}
            disabled={loading}
          >
            Fetch Tickets
          </Button>
        </div>
      </form>

      {error ? <div className="error-banner">{error}</div> : null}

      {result && (
        <section className="zoho-results card">
          {result.contact && (
            <div className="zoho-contact">
              <div className="zoho-contact-name">{result.contact.name || "Customer"}</div>
              <div className="muted">
                {[result.contact.email, result.contact.phone].filter(Boolean).join(" · ")}
              </div>
            </div>
          )}

          {tickets.length > 0 ? (
            <div className="zoho-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Ticket</th>
                    <th>Subject</th>
                    <th>Status</th>
                    <th>Priority</th>
                    <th>Created</th>
                    <th>Last Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {tickets.map((t) => (
                    <tr
                      key={t.id}
                      className="zoho-row"
                      onClick={() => setOpenTicket(t)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setOpenTicket(t);
                        }
                      }}
                      tabIndex={0}
                      role="button"
                      title="View conversation"
                    >
                      <td>{t.ticket_number ? `#${t.ticket_number}` : t.id}</td>
                      <td>{t.subject || "—"}</td>
                      <td>
                        <span className={statusClass(t.status)}>{t.status || "—"}</span>
                      </td>
                      <td>
                        <span className={priorityClass(t.priority)}>{t.priority || "—"}</span>
                      </td>
                      <td>{fmtDate(t.created_time)}</td>
                      <td>{fmtDate(t.modified_time)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">{result.message || "No tickets found for this customer."}</p>
          )}
        </section>
      )}

      {openTicket && (
        <TicketDetailModal ticket={openTicket} onClose={() => setOpenTicket(null)} />
      )}

      {showConfigModal && (
        <ZohoConfigModal
          onClose={() => setShowConfigModal(false)}
          onSaved={() => {
            checkStatus();
            setShowConfigModal(false);
          }}
        />
      )}
    </div>
  );
}

// Configuration Modal directly in Zoho tab for quick setup and testing
function ZohoConfigModal({ onClose, onSaved }) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState("");
  const [saveError, setSaveError] = useState("");

  const [formData, setFormData] = useState({
    zoho_client_id: "",
    zoho_client_secret: "",
    zoho_refresh_token: "",
    zoho_org_id: "",
    zoho_accounts_base: "https://accounts.zoho.in",
    zoho_desk_base: "https://desk.zoho.in",
  });

  const [meta, setMeta] = useState({
    zoho_has_client_secret: false,
    zoho_has_refresh_token: false,
  });

  const [editSecrets, setEditSecrets] = useState({
    zoho_client_secret: false,
    zoho_refresh_token: false,
  });

  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    apiFetch("/api/settings")
      .then((data) => {
        if (!alive || !data) return;
        setFormData({
          zoho_client_id: data.zoho_client_id || "",
          zoho_client_secret: "",
          zoho_refresh_token: "",
          zoho_org_id: data.zoho_org_id || "",
          zoho_accounts_base: data.zoho_accounts_base || "https://accounts.zoho.in",
          zoho_desk_base: data.zoho_desk_base || "https://desk.zoho.in",
        });
        setMeta({
          zoho_has_client_secret: data.zoho_has_client_secret,
          zoho_has_refresh_token: data.zoho_has_refresh_token,
        });
        setEditSecrets({
          zoho_client_secret: !data.zoho_has_client_secret,
          zoho_refresh_token: !data.zoho_has_refresh_token,
        });
      })
      .catch((err) => setSaveError(`Failed to load settings: ${err.message}`))
      .finally(() => alive && setLoading(false));

    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const handleChange = (field, val) => {
    setFormData((prev) => ({ ...prev, [field]: val }));
    setSaveSuccess("");
    setSaveError("");
    setTestResult(null);
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await apiFetch("/api/settings/test-zoho", {
        method: "POST",
        body: {
          zoho_client_id: formData.zoho_client_id,
          zoho_client_secret: formData.zoho_client_secret,
          zoho_refresh_token: formData.zoho_refresh_token,
          zoho_org_id: formData.zoho_org_id,
          zoho_accounts_base: formData.zoho_accounts_base,
          zoho_desk_base: formData.zoho_desk_base,
        },
      });
      setTestResult(res);
    } catch (err) {
      setTestResult({ success: false, error: err.message });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveSuccess("");
    setSaveError("");

    const payload = {
      zoho_client_id: formData.zoho_client_id,
      zoho_org_id: formData.zoho_org_id,
      zoho_accounts_base: formData.zoho_accounts_base,
      zoho_desk_base: formData.zoho_desk_base,
    };

    if (editSecrets.zoho_client_secret || formData.zoho_client_secret) {
      payload.zoho_client_secret = formData.zoho_client_secret;
    }
    if (editSecrets.zoho_refresh_token || formData.zoho_refresh_token) {
      payload.zoho_refresh_token = formData.zoho_refresh_token;
    }

    try {
      await apiFetch("/api/settings", {
        method: "POST",
        body: payload,
      });
      setSaveSuccess("Zoho Desk credentials saved and applied!");
      if (onSaved) {
        setTimeout(() => onSaved(), 500);
      }
    } catch (err) {
      setSaveError(`Failed to save settings: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-container" style={{ maxWidth: "680px" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title" style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Settings size={18} />
              Configure Zoho Desk Credentials
            </h2>
            <p className="modal-subtitle">
              Manage OAuth 2.0 credentials to search customer tickets and conversation histories.
            </p>
          </div>
          <button className="modal-close-btn" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="modal-body">
          {loading ? (
            <div className="app-loading" style={{ height: "180px" }}>
              Loading credentials...
            </div>
          ) : (
            <>
              {saveSuccess && (
                <div className="callout callout-success" style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <CheckCircle2 size={15} /> {saveSuccess}
                </div>
              )}
              {saveError && (
                <div className="callout callout-danger" style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <XCircle size={15} /> {saveError}
                </div>
              )}

              {/* Guide Banner */}
              <div className="callout callout-info" style={{ marginBottom: "14px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                    <KeyRound size={15} />
                    <strong>Zoho OAuth Setup:</strong> Requires Self Client credentials with Desk scopes.
                  </span>
                  <button
                    type="button"
                    className="btn-text-action"
                    onClick={() => setShowHelp(!showHelp)}
                    style={{ fontSize: "12px", color: "var(--accent-strong)", fontWeight: 650, display: "inline-flex", alignItems: "center", gap: "4px" }}
                  >
                    <HelpCircle size={13} />
                    {showHelp ? "Hide Guide" : "View Guide"}
                  </button>
                </div>
                {showHelp && (
                  <div style={{ marginTop: "10px", fontSize: "12px", lineHeight: 1.5, borderTop: "1px dashed var(--line)", paddingTop: "8px" }}>
                    <p style={{ margin: "0 0 6px" }}>
                      1. Open{" "}
                      <a href="https://api-console.zoho.in" target="_blank" rel="noreferrer" className="link-highlight" style={{ display: "inline-flex", alignItems: "center", gap: "3px" }}>
                        Zoho API Console (India) <ExternalLink size={11} />
                      </a>{" "}
                      or{" "}
                      <a href="https://api-console.zoho.com" target="_blank" rel="noreferrer" className="link-highlight" style={{ display: "inline-flex", alignItems: "center", gap: "3px" }}>
                        Global Console (.com) <ExternalLink size={11} />
                      </a>
                    </p>
                    <p style={{ margin: "0 0 6px" }}>
                      2. Create a <strong>Self Client</strong> and note the <em>Client ID</em> and <em>Client Secret</em>.
                    </p>
                    <p style={{ margin: "0 0 6px" }}>
                      3. Under "Generate Code", enter the scopes:
                      <br />
                      <code>Desk.tickets.READ,Desk.contacts.READ,Desk.search.READ</code>
                    </p>
                    <p style={{ margin: 0 }}>
                      4. Exchange the code for a permanent <strong>Refresh Token</strong> and paste it below.
                    </p>
                  </div>
                )}
              </div>

              <div className="form-grid-2">
                <div className="form-group">
                  <label className="field-label">Zoho Client ID</label>
                  <input
                    type="text"
                    name="zoho_client_id_field"
                    autoComplete="off"
                    data-lpignore="true"
                    className="field-input"
                    placeholder="1000.XXXXXXXXXXXXXXXXXXXXXXXXXX"
                    value={formData.zoho_client_id}
                    onChange={(e) => handleChange("zoho_client_id", e.target.value)}
                  />
                </div>
                <div className="form-group">
                  <label className="field-label">Zoho Org ID (Organization / Portal ID)</label>
                  <input
                    type="text"
                    name="zoho_org_id_field"
                    autoComplete="off"
                    data-lpignore="true"
                    className="field-input"
                    placeholder="e.g. 60021345678"
                    value={formData.zoho_org_id}
                    onChange={(e) => handleChange("zoho_org_id", e.target.value)}
                  />
                </div>
              </div>

              <div className="form-grid-2">
                <div className="form-group">
                  <label className="field-label">Zoho Client Secret</label>
                  {meta.zoho_has_client_secret && !editSecrets.zoho_client_secret ? (
                    <div className="secret-saved-row">
                      <span className="secret-indicator" style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
                        <Lock size={12} /> Secret configured in database
                      </span>
                      <button
                        type="button"
                        className="btn-text-action"
                        onClick={() => setEditSecrets((p) => ({ ...p, zoho_client_secret: true }))}
                      >
                        Change Secret
                      </button>
                    </div>
                  ) : (
                    <input
                      type="password"
                      name="zoho_client_secret_input"
                      autoComplete="new-password"
                      data-lpignore="true"
                      className="field-input"
                      placeholder="Enter Zoho Client Secret..."
                      value={formData.zoho_client_secret}
                      onChange={(e) => handleChange("zoho_client_secret", e.target.value)}
                    />
                  )}
                </div>

                <div className="form-group">
                  <label className="field-label">Zoho Refresh Token</label>
                  {meta.zoho_has_refresh_token && !editSecrets.zoho_refresh_token ? (
                    <div className="secret-saved-row">
                      <span className="secret-indicator" style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
                        <Lock size={12} /> Token configured in database
                      </span>
                      <button
                        type="button"
                        className="btn-text-action"
                        onClick={() => setEditSecrets((p) => ({ ...p, zoho_refresh_token: true }))}
                      >
                        Change Token
                      </button>
                    </div>
                  ) : (
                    <input
                      type="password"
                      name="zoho_refresh_token_input"
                      autoComplete="new-password"
                      data-lpignore="true"
                      className="field-input"
                      placeholder="1000.xxxxxxxxxxxxxxxxxxxxxxxx..."
                      value={formData.zoho_refresh_token}
                      onChange={(e) => handleChange("zoho_refresh_token", e.target.value)}
                    />
                  )}
                </div>
              </div>

              <div className="form-grid-2">
                <div className="form-group">
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <label className="field-label" style={{ marginBottom: 0 }}>Zoho Accounts Base URL</label>
                    <div style={{ display: "flex", gap: "4px" }}>
                      <button
                        type="button"
                        className="btn-text-action"
                        style={{ fontSize: "11px" }}
                        onClick={() => handleChange("zoho_accounts_base", "https://accounts.zoho.in")}
                      >
                        .in
                      </button>
                      <button
                        type="button"
                        className="btn-text-action"
                        style={{ fontSize: "11px" }}
                        onClick={() => handleChange("zoho_accounts_base", "https://accounts.zoho.com")}
                      >
                        .com
                      </button>
                    </div>
                  </div>
                  <input
                    type="text"
                    name="zoho_accounts_base_input"
                    className="field-input"
                    style={{ marginTop: "4px" }}
                    placeholder="https://accounts.zoho.in"
                    value={formData.zoho_accounts_base}
                    onChange={(e) => handleChange("zoho_accounts_base", e.target.value)}
                  />
                </div>

                <div className="form-group">
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <label className="field-label" style={{ marginBottom: 0 }}>Zoho Desk Base URL</label>
                    <div style={{ display: "flex", gap: "4px" }}>
                      <button
                        type="button"
                        className="btn-text-action"
                        style={{ fontSize: "11px" }}
                        onClick={() => handleChange("zoho_desk_base", "https://desk.zoho.in")}
                      >
                        .in
                      </button>
                      <button
                        type="button"
                        className="btn-text-action"
                        style={{ fontSize: "11px" }}
                        onClick={() => handleChange("zoho_desk_base", "https://desk.zoho.com")}
                      >
                        .com
                      </button>
                    </div>
                  </div>
                  <input
                    type="text"
                    name="zoho_desk_base_input"
                    className="field-input"
                    style={{ marginTop: "4px" }}
                    placeholder="https://desk.zoho.in"
                    value={formData.zoho_desk_base}
                    onChange={(e) => handleChange("zoho_desk_base", e.target.value)}
                  />
                </div>
              </div>

              <div style={{ marginTop: "12px", display: "flex", gap: "10px", alignItems: "center" }}>
                <button
                  type="button"
                  className="action-btn"
                  onClick={handleTest}
                  disabled={testing}
                  style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                >
                  {testing ? "Testing Zoho..." : <><Zap size={14} /> Test Zoho Connection</>}
                </button>
              </div>

              {testResult && (
                <div
                  className={`validation-box ${testResult.success ? "box-success" : "box-danger"}`}
                  style={{ marginTop: "12px" }}
                >
                  {testResult.success ? (
                    <div>
                      <div className="box-title" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><CheckCircle2 size={16} color="var(--ok)" /> Zoho Desk Connected Successfully</div>
                      <div className="box-sub">{testResult.message}</div>
                    </div>
                  ) : (
                    <div>
                      <div className="box-title" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><XCircle size={16} color="var(--danger)" /> Zoho Desk Connection Failed</div>
                      <div className="box-sub">{testResult.error}</div>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="button" className="btn-primary" onClick={handleSave} disabled={saving} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {saving ? "Saving..." : <><Save size={14} /> Save Zoho Credentials</>}
          </button>
        </div>
      </div>
    </div>
  );
}

// Detail overlay: the clicked ticket's fields plus its conversation threads,
// fetched on open from GET /zoho/tickets/{id}.
function TicketDetailModal({ ticket, onClose }) {
  const [res, setRes] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    apiFetch(`/zoho/tickets/${encodeURIComponent(ticket.id)}`)
      .then((d) => alive && setRes(d))
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [ticket.id]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Fall back to the row's own fields until the detail call lands.
  const detail = res?.ticket || ticket;
  const threads = res?.threads || [];

  return (
    <div className="zoho-modal-backdrop" onClick={onClose}>
      <div
        className="zoho-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Ticket detail"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="zoho-modal-head">
          <div>
            <div className="zoho-modal-title">{detail.subject || "Ticket"}</div>
            <div className="zoho-modal-sub">
              {detail.ticket_number ? `#${detail.ticket_number}` : detail.id}
              {detail.channel ? ` · ${detail.channel}` : ""}
              {detail.department ? ` · ${detail.department}` : ""}
            </div>
          </div>
          <button type="button" className="secondary" onClick={onClose} aria-label="Close" style={{ display: "inline-flex", alignItems: "center" }}>
            <X size={14} />
          </button>
        </header>

        <div className="zoho-modal-body">
          <div className="zoho-meta">
            <Meta label="Status" value={<span className={statusClass(detail.status)}>{detail.status || "—"}</span>} />
            <Meta label="Priority" value={<span className={priorityClass(detail.priority)}>{detail.priority || "—"}</span>} />
            <Meta label="Assignee" value={detail.assignee || "Unassigned"} />
            <Meta label="Created" value={fmtDate(detail.created_time)} />
            <Meta label="Last updated" value={fmtDate(detail.modified_time)} />
            {detail.due_date ? <Meta label="Due" value={fmtDate(detail.due_date)} /> : null}
          </div>

          {detail.description ? (
            <section className="zoho-desc">
              <h4>Description</h4>
              <p className="zoho-text">{detail.description}</p>
            </section>
          ) : null}

          <h4 className="zoho-threads-head">
            Conversation
            {res ? <span className="muted"> · {threads.length} message{threads.length === 1 ? "" : "s"}</span> : null}
            {res?.threads_truncated ? <span className="muted"> (most recent shown)</span> : null}
          </h4>

          {loading && <p className="muted">Loading conversation…</p>}
          {error ? <div className="error-banner">{error}</div> : null}
          {!loading && !error && threads.length === 0 && (
            <p className="muted">{res?.message || "No conversation threads on this ticket."}</p>
          )}

          {threads.map((t) => (
            <article key={t.id} className={`zoho-thread ${t.direction === "out" ? "out" : "in"}`}>
              <div className="zoho-thread-head">
                <span className="zoho-thread-author">{t.author || t.from_address || "Unknown sender"}</span>
                <span className="muted">
                  {t.direction === "out" ? "Agent reply" : "From customer"}
                  {t.channel ? ` · ${t.channel}` : ""}
                  {t.has_attachment ? (
                    <>
                      {" · "}
                      <Paperclip size={12} style={{ verticalAlign: "-2px" }} aria-label="Has attachment" />
                    </>
                  ) : null}
                  {` · ${fmtDate(t.created_time)}`}
                </span>
              </div>
              {t.to_address ? <div className="zoho-thread-to muted">To: {t.to_address}</div> : null}
              <p className="zoho-text">{t.content || t.summary || "(no message body)"}</p>
            </article>
          ))}
        </div>

        {detail.web_url ? (
          <footer className="zoho-modal-foot">
            <a href={detail.web_url} target="_blank" rel="noreferrer" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              Open in Zoho Desk <ExternalLink size={13} />
            </a>
          </footer>
        ) : null}
      </div>
    </div>
  );
}

function Meta({ label, value }) {
  return (
    <div className="zoho-meta-item">
      <span className="zoho-meta-label">{label}</span>
      <span className="zoho-meta-value">{value}</span>
    </div>
  );
}
