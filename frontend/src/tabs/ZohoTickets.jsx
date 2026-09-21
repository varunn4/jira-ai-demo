// Zoho Tickets — enter a customer's email/phone, fetch and display their Zoho
// Desk tickets. Backs the "zoho" capability. The same POST /zoho/tickets API is
// intended to be reused by the customer portal (see the design doc).
import { useEffect, useState } from "react";
import { apiFetch } from "../api.js";
import { fmtDate } from "../lib/format.js";

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
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null); // { configured, contact, tickets, message }
  const [openTicket, setOpenTicket] = useState(null); // row clicked → detail modal

  // Probe configuration once so we can warn before the operator wastes a lookup.
  useEffect(() => {
    apiFetch("/zoho/status")
      .then((d) => setConfigured(!!d.configured))
      .catch(() => setConfigured(false));
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
      <header className="zoho-hero">
        <h2>🎫 Zoho Tickets</h2>
        <p className="muted">
          Enter a customer's email or phone number to look them up in Zoho Desk
          and view their support tickets and current statuses.
        </p>
      </header>

      {configured === false && (
        <div className="error-banner">
          Zoho Desk is not configured. Set <code>ZOHO_CLIENT_ID</code>,{" "}
          <code>ZOHO_CLIENT_SECRET</code>, <code>ZOHO_REFRESH_TOKEN</code> and{" "}
          <code>ZOHO_ORG_ID</code> in the server environment.
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
          <button type="submit" disabled={loading}>
            {loading ? "Fetching…" : "Fetch Tickets"}
          </button>
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
          <button type="button" className="secondary" onClick={onClose} aria-label="Close">
            ✕
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
                  {t.has_attachment ? " · 📎" : ""}
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
            <a href={detail.web_url} target="_blank" rel="noreferrer">
              Open in Zoho Desk ↗
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
