import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { fmtDate } from "../lib/format";

export default function Logs() {
  const [jobs, setJobs] = useState([]);
  const [fetchLogs, setFetchLogs] = useState([]);
  const [error, setError] = useState("");

  async function load() {
    setError("");
    try {
      const [jobsData, fetchData] = await Promise.all([
        apiFetch("/graph-admin/jobs?limit=50"),
        apiFetch("/graph-admin/fetch-logs?limit=100"),
      ]);
      setJobs(Array.isArray(jobsData) ? jobsData : []);
      if (fetchData.error) setError(fetchData.error);
      setFetchLogs(fetchData.logs || []);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14 }}>
        <span style={{ fontSize: 13, color: "var(--muted)" }}>Recent Qdrant jobs &amp; Jira fetch log</span>
        <button className="secondary" style={{ width: "auto", minHeight: "unset", padding: "6px 14px", fontSize: 13 }} onClick={load}>
          Refresh
        </button>
      </div>

      <p style={{ fontSize: 13, fontWeight: 700, color: "var(--muted)", margin: "0 0 8px" }}>Qdrant Jobs</p>
      <table>
        <thead>
          <tr>
            <th style={{ width: "10%" }}>Job ID</th>
            <th style={{ width: "14%" }}>Action</th>
            <th style={{ width: "10%" }}>Status</th>
            <th style={{ width: "8%" }}>Repos</th>
            <th style={{ width: "8%" }}>Jira</th>
            <th style={{ width: "10%" }}>Embeddings</th>
            <th style={{ width: "14%" }}>Started</th>
            <th style={{ width: "14%" }}>Completed</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {jobs.length === 0 ? (
            <tr>
              <td colSpan={9} style={{ color: "var(--muted)" }}>
                No jobs yet
              </td>
            </tr>
          ) : (
            jobs.map((j) => {
              const p = j.progress || {};
              const t = j.totals || {};
              const cls = j.status === "completed" ? "ok" : j.status === "failed" ? "err" : "run";
              return (
                <tr key={j.job_id}>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>{(j.job_id || "").slice(0, 8)}</td>
                  <td>{j.action || "—"}</td>
                  <td><span className={`badge ${cls}`}>{j.status || "—"}</span></td>
                  <td>{`${p.repositories_done || 0}/${t.repositories || 0}`}</td>
                  <td>{`${p.jira_tickets_done || 0}/${t.jira_tickets || 0}`}</td>
                  <td>{`${p.embedding_documents_done || 0}/${t.embedding_documents || 0}`}</td>
                  <td>{fmtDate(j.started_at)}</td>
                  <td>{fmtDate(j.completed_at)}</td>
                  <td style={{ color: "var(--danger)", fontSize: 12 }}>{j.error || ""}</td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>

      <p style={{ fontSize: 13, fontWeight: 700, color: "var(--muted)", margin: "18px 0 8px" }}>Jira Fetch Log</p>
      <table>
        <thead>
          <tr>
            <th style={{ width: "12%" }}>Project</th>
            <th style={{ width: "10%" }}>Tickets</th>
            <th style={{ width: "9%" }}>From Cache</th>
            <th style={{ width: "10%" }}>Duration ms</th>
            <th style={{ width: "16%" }}>Fetched At</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {fetchLogs.length === 0 ? (
            <tr>
              <td colSpan={6} style={{ color: "var(--muted)" }}>
                No fetch log entries yet
              </td>
            </tr>
          ) : (
            fetchLogs.map((l, i) => (
              <tr key={i}>
                <td>{l.project_key}</td>
                <td>{l.ticket_count}</td>
                <td>{l.from_cache ? "✓" : ""}</td>
                <td>{l.duration_ms}</td>
                <td>{fmtDate(l.fetched_at)}</td>
                <td style={{ color: "var(--danger)", fontSize: 12 }}>{l.error || ""}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
      {error && <div style={{ color: "var(--danger)", fontSize: 13, marginTop: 8 }}>{error}</div>}
    </div>
  );
}
