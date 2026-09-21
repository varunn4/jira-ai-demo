import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { fmtDate } from "../lib/format";

// Aggregated utilization analytics for the AI Governor system. Reads
// /graph-admin/utilization; every section is optional (available flag) so the
// view degrades gracefully when a data source has no table yet.
//
// Every stat box (and the ticket-status rows) is clickable: it drills into
// /graph-admin/utilization/tickets to list the tickets that make up that number,
// each linking out to Jira.
export default function Utilization() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [drill, setDrill] = useState(null); // { metric, status, title }

  async function load() {
    setError("");
    setLoading(true);
    try {
      setData(await apiFetch("/graph-admin/utilization"));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tc = data?.test_cases || {};
  const dr = data?.doc_reviews || {};
  const tk = data?.tickets || {};
  const tr = data?.transitions || {};
  const dd = data?.due_date || {};
  const doc = data?.documentation || {};
  const conv = data?.conversations || {};

  // metric: backend key; status: optional jira_ticket_cache status filter.
  const openDrill = (metric, title, status = "") => setDrill({ metric, title, status });

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "var(--muted)" }}>
          AI Governor utilization — test cases, document reviews, ticket states &amp; transitions
        </span>
        <button
          className="secondary"
          style={{ width: "auto", minHeight: "unset", padding: "6px 14px", fontSize: 13 }}
          onClick={load}
        >
          Refresh
        </button>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          Tip: click any box to see the tickets behind it.
        </span>
      </div>

      {data && !data.database_configured && (
        <div style={{ color: "var(--muted)", fontSize: 14, padding: "24px 0" }}>
          Database is not configured. Set <code>DATABASE_URL</code> (or the <code>DB_*</code> vars)
          in the server environment to enable utilization analytics.
        </div>
      )}

      {data?.database_configured && (
        <>
          <div className="stats-grid">
            <Stat value={tc.total_test_cases} label="Test Cases Built" onClick={() => openDrill("test_cases", "Tickets with test cases")} />
            <Stat value={tc.tickets_covered} label="Tickets w/ Test Cases" onClick={() => openDrill("test_cases", "Tickets with test cases")} />
            <Stat value={tc.avg_per_ticket} label="Avg Cases / Ticket" onClick={() => openDrill("test_cases", "Tickets with test cases")} />
            <Stat value={dr.docs_reviewed} label="PRD/BRD Docs Reviewed" onClick={() => openDrill("doc_reviews", "Tickets with reviewed docs")} />
            <Stat value={dr.total_reviews} label="Doc Review Runs" onClick={() => openDrill("doc_reviews", "Tickets with reviewed docs")} />
            <Stat value={tr.total} label="State Transitions" onClick={() => openDrill("transitions", "Tickets with state transitions")} />
            <Stat value={tk.total} label="Tickets Tracked" onClick={() => openDrill("tickets", "Cached Jira tickets")} />
            <Stat value={doc.generated} label="Docs Generated" onClick={() => openDrill("documentation", "Generated documents (by repo)")} />
            <Stat value={conv.threads} label="Slack Q&A Threads" onClick={() => openDrill("conversations", "Tickets with Slack Q&A threads")} />
          </div>

          <div
            style={{
              marginTop: 18,
              fontSize: 12.5,
              color: "var(--muted)",
              background: "var(--card)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              padding: "10px 14px",
            }}
          >
            Test cases are AI-<strong>generated</strong> artifacts — there is no automated
            pass/fail tracking. QA sign-off is the developer moving the ticket out of QA
            (e.g. <em>QA → Ready for Deployment</em>), which appears under{" "}
            <strong>State transitions</strong> below.
          </div>

          <Breakdown
            title="Document reviews by type (PRD / TechDoc / BRD)"
            rows={dr.by_type}
            cols={[["doc_type", "Document type"], ["docs", "Docs"], ["reviews", "Review runs"]]}
            empty={dr.available === false ? "No doc_reviews table found." : "No documents reviewed yet."}
          />

          <Breakdown
            title="Ticket state distribution (current snapshot)"
            rows={tk.by_status}
            cols={[["status", "Status"], ["count", "Tickets"]]}
            empty={tk.available === false ? "No jira_ticket_cache table found." : "No tickets cached yet."}
            onRowClick={(row) => openDrill("tickets", `Tickets in “${row.status}”`, row.status)}
          />

          <Breakdown
            title="Tickets by project"
            rows={tk.by_project}
            cols={[["project", "Project"], ["count", "Tickets"]]}
            empty="—"
            hideWhenEmpty
          />

          <Breakdown
            title="State transitions (from → to)"
            rows={tr.by_transition}
            cols={[["from_status", "From"], ["to_status", "To"], ["count", "Moves"]]}
            empty={
              tr.available === false
                ? "Transition logging is not set up yet."
                : "No transitions recorded yet — they accrue once the Status Transition Logger workflow is active."
            }
          />

          {dd.available && (
            <Breakdown
              title="Due-date alerts fired (by remaining-time band)"
              rows={[
                { band: "< 75% time left", count: dd.alerts?.lt_75pct },
                { band: "< 50% time left", count: dd.alerts?.lt_50pct },
                { band: "< 25% time left", count: dd.alerts?.lt_25pct },
                { band: "Breached / overdue", count: dd.alerts?.breached },
              ]}
              cols={[["band", "Band"], ["count", "Tickets alerted"]]}
              empty="—"
            />
          )}

          {tr.recent?.length > 0 && (
            <div style={{ marginTop: 22 }}>
              <span className="tc-section-title">Recent transitions</span>
              <table>
                <thead>
                  <tr>
                    <th style={{ width: "16%" }}>Ticket</th>
                    <th>From</th>
                    <th>To</th>
                    <th>Assignee</th>
                    <th style={{ width: "18%" }}>When</th>
                  </tr>
                </thead>
                <tbody>
                  {tr.recent.map((r, i) => (
                    <tr key={i}>
                      <td>{r.jira_ticket_id}</td>
                      <td style={{ color: "var(--muted)" }}>{r.from_status || "—"}</td>
                      <td>
                        <span className="badge ok">{r.to_status}</span>
                      </td>
                      <td>{r.assignee_name || "—"}</td>
                      <td style={{ fontSize: 12, color: "var(--muted)" }}>{fmtDate(r.changed_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {loading && !data && <div style={{ color: "var(--muted)", fontSize: 13, marginTop: 8 }}>Loading…</div>}
      {error && <div style={{ color: "var(--danger)", fontSize: 13, marginTop: 8 }}>{error}</div>}

      {drill && <DrillModal drill={drill} onClose={() => setDrill(null)} />}
    </div>
  );
}

function Stat({ value, label, onClick }) {
  return (
    <div
      className="stat-card"
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      title={onClick ? "Click to see the tickets behind this number" : undefined}
      style={onClick ? { cursor: "pointer" } : undefined}
    >
      <div className="stat-value">{value ?? 0}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function Breakdown({ title, rows, cols, empty, hideWhenEmpty, onRowClick }) {
  const list = rows || [];
  if (hideWhenEmpty && list.length === 0) return null;
  return (
    <div style={{ marginTop: 22 }}>
      <span className="tc-section-title">{title}</span>
      {list.length === 0 ? (
        <div style={{ color: "var(--muted)", fontSize: 13, padding: "6px 0" }}>{empty}</div>
      ) : (
        <table>
          <thead>
            <tr>
              {cols.map(([key, label]) => (
                <th key={key}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list.map((row, i) => (
              <tr
                key={i}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                style={onRowClick ? { cursor: "pointer" } : undefined}
                title={onRowClick ? "Click to list these tickets" : undefined}
              >
                {cols.map(([key]) => (
                  <td key={key}>{row[key] ?? "—"}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Drill-down overlay: fetches and lists the tickets (or repos) behind a stat.
function DrillModal({ drill, onClose }) {
  const [res, setRes] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ metric: drill.metric });
    if (drill.status) params.set("status", drill.status);
    apiFetch(`/graph-admin/utilization/tickets?${params.toString()}`)
      .then((d) => alive && setRes(d))
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [drill.metric, drill.status]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const items = res?.items || [];
  const base = (res?.jira_base_url || "").replace(/\/$/, "");
  const isRepo = res?.kind === "repo";

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        padding: "6vh 16px",
        zIndex: 1000,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--card, #fff)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          width: "min(640px, 100%)",
          maxHeight: "82vh",
          display: "flex",
          flexDirection: "column",
          boxShadow: "0 12px 40px rgba(0,0,0,0.25)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "14px 18px",
            borderBottom: "1px solid var(--line)",
          }}
        >
          <div>
            <div style={{ fontWeight: 600, fontSize: 15 }}>{drill.title}</div>
            {res && (
              <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 2 }}>
                {res.total} {isRepo ? "repos/docs" : "tickets"}
                {res.truncated ? ` · showing first ${items.length}` : ""}
                {res.status_source === "live" ? " · live status from Jira" : ""}
              </div>
            )}
          </div>
          <button
            className="secondary"
            style={{ width: "auto", minHeight: "unset", padding: "4px 12px", fontSize: 13 }}
            onClick={onClose}
          >
            Close
          </button>
        </div>

        <div style={{ overflowY: "auto", padding: "8px 0" }}>
          {loading && <div style={{ color: "var(--muted)", fontSize: 13, padding: "16px 18px" }}>Loading…</div>}
          {error && <div style={{ color: "var(--danger)", fontSize: 13, padding: "16px 18px" }}>{error}</div>}
          {!loading && !error && items.length === 0 && (
            <div style={{ color: "var(--muted)", fontSize: 13, padding: "16px 18px" }}>
              No tickets found for this metric.
            </div>
          )}
          {!loading &&
            items.map((it, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  gap: 10,
                  padding: "8px 18px",
                  borderTop: i === 0 ? "none" : "1px solid var(--line)",
                  fontSize: 13.5,
                }}
              >
                {isRepo || !base ? (
                  <span style={{ fontWeight: 600, whiteSpace: "nowrap" }}>{it.id}</span>
                ) : (
                  <a
                    href={`${base}/browse/${encodeURIComponent(it.id)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ fontWeight: 600, whiteSpace: "nowrap", color: "var(--accent, #5b5bd6)" }}
                  >
                    {it.id}
                  </a>
                )}
                {it.summary && (
                  <span style={{ color: "var(--muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {it.summary}
                  </span>
                )}
                <span style={{ marginLeft: "auto", display: "flex", gap: 8, whiteSpace: "nowrap" }}>
                  {it.status && <span className="badge">{it.status}</span>}
                  {it.extra && <span style={{ fontSize: 12, color: "var(--muted)" }}>{it.extra}</span>}
                </span>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}
