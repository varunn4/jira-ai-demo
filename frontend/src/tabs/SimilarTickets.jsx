import { useState } from "react";
import { apiFetch } from "../api";
import { fmtDate } from "../lib/format";

const METHOD_LABELS = {
  hybrid_rrf: "Hybrid semantic (RRF)",
  semantic: "Dense semantic",
  keyword_fallback: "Keyword fallback",
  none: "No results",
};
const METHOD_COLORS = {
  hybrid_rrf: "var(--ok)",
  semantic: "var(--ok)",
  keyword_fallback: "var(--warn)",
  none: "var(--warn)",
};

export default function SimilarTickets() {
  const [form, setForm] = useState({ summary: "", description: "", projectKey: "" });
  const [status, setStatus] = useState({ msg: "", cls: "" });
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState(null);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  async function search() {
    const summary = form.summary.trim();
    if (!summary) {
      setStatus({ msg: "Summary is required", cls: "error" });
      return;
    }
    setBusy(true);
    setData(null);
    setStatus({ msg: "Searching for similar tickets…", cls: "running" });
    try {
      const res = await apiFetch("/analyze-ticket/similar", {
        method: "POST",
        body: {
          summary,
          description: form.description.trim() || null,
          project_key: form.projectKey.trim() || null,
        },
      });
      setData(res);
      setStatus({
        msg: res.total_found
          ? `Found ${res.total_found} similar ticket${res.total_found !== 1 ? "s" : ""}`
          : "No matches found",
        cls: res.total_found ? "ok" : "",
      });
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="tc-layout">
      <div className="tc-form-col">
        <p className="tc-section-label">New ticket details</p>
        <div className="tc-field">
          <label className="tc-label">Summary <span className="tc-required">*</span></label>
          <input className="tc-input" type="text" placeholder="Short description of the new ticket" value={form.summary} onChange={(e) => set("summary", e.target.value)} />
        </div>
        <div className="tc-field">
          <label className="tc-label">
            Description <span className="tc-optional">(optional but improves results)</span>
          </label>
          <textarea className="tc-textarea" rows={6} placeholder="Full ticket description…" value={form.description} onChange={(e) => set("description", e.target.value)} />
        </div>

        <p className="tc-section-label" style={{ marginTop: 18 }}>Search filters</p>
        <div className="tc-field-row">
          <div className="tc-field">
            <label className="tc-label">Project key <span className="tc-optional">(optional)</span></label>
            <input className="tc-input" type="text" placeholder="e.g. RFC" value={form.projectKey} onChange={(e) => set("projectKey", e.target.value)} />
          </div>
        </div>
        <button style={{ marginTop: 18 }} disabled={busy} onClick={search}>
          Find Similar Tickets
        </button>
        <div className={`status-bar ${status.cls}`} style={{ marginTop: 10 }}>{status.msg}</div>
      </div>

      <div className="tc-result-col">
        {!data ? (
          <div className="tc-placeholder">
            Fill in a ticket summary and click <strong>Find Similar Tickets</strong>.
          </div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
              <span className="tc-stat"><span>{data.total_found}</span> tickets found</span>
              <span className="tc-stat" style={{ color: METHOD_COLORS[data.search_method] ?? "var(--warn)" }}>
                {METHOD_LABELS[data.search_method] ?? data.search_method}
              </span>
            </div>
            {!data.tickets || data.tickets.length === 0 ? (
              <p style={{ color: "var(--muted)", fontSize: 14 }}>
                No ticket above 65% match found. Try removing the project key.
              </p>
            ) : (
              data.tickets.map((t) => <SimCard key={t.ticket_key} t={t} />)
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SimCard({ t }) {
  const [expanded, setExpanded] = useState(false);
  const score = t.similarity_score || 0;
  const scoreLabel = score === 0 ? "keyword" : `${Math.round(score * 100)}% match`;
  const scoreClass = score === 0 ? "kw" : score >= 0.75 ? "high" : score >= 0.5 ? "medium" : "low";
  const statusKey = (t.status || "").toLowerCase().replace(/\s+/g, "-");
  const typeKey = (t.issue_type || "").toLowerCase();
  const desc = (t.description || "").trim();

  const footer = [
    t.assignee_name ? ["Assignee", t.assignee_name] : null,
    t.reporter_name ? ["Reporter", t.reporter_name] : null,
    t.updated_at ? ["Updated", fmtDate(t.updated_at)] : null,
    t.created_at ? ["Created", fmtDate(t.created_at)] : null,
  ].filter(Boolean);

  return (
    <div className="sim-card">
      <div className="sim-card-header">
        <div>
          <span className="sim-card-key">{t.ticket_key}</span>
          <div className="sim-card-summary">{t.summary || ""}</div>
        </div>
        <span className={`sim-score ${scoreClass}`}>{scoreLabel}</span>
      </div>
      <div className="sim-card-meta">
        {t.status && <span className={`sim-chip status-${statusKey}`}>{t.status}</span>}
        {t.issue_type && <span className={`sim-chip type-${typeKey}`}>{t.issue_type}</span>}
        {t.priority && <span className="sim-chip">{t.priority}</span>}
        {(t.labels || []).map((l, i) => (
          <span key={i} className="sim-chip">{l}</span>
        ))}
      </div>
      {desc && (
        <>
          <div className={`sim-description${expanded ? " expanded" : ""}`}>{desc}</div>
          <button className="sim-expand-btn" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "Show less" : "Show more"}
          </button>
        </>
      )}
      {footer.length > 0 && (
        <div className="sim-footer">
          {footer.map(([label, value], i) => (
            <span key={i}>
              {label}: <b>{value}</b>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
