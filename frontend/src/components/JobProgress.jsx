import { useState } from "react";

export default function JobProgress({ stats, job }) {
  const [showLogs, setShowLogs] = useState(false);
  if (!stats) return null;

  const logs = (job && job.logs) || [];
  const isRunning = job?.status === "running" || job?.status === "in_progress";
  const isDone = job?.status === "completed" || job?.status === "done";
  const isFailed = job?.status === "failed" || job?.status === "error";

  return (
    <div className={`job-progress-card ${job?.status || "pending"}`}>
      <div className="job-progress-header">
        <div className="job-title-group">
          <span className={`job-status-badge ${job?.status || "pending"}`}>
            {isRunning && <span className="pulse-dot" />}
            {job?.status ? job.status.toUpperCase() : "QUEUED"}
          </span>
          <span className="job-action-label">Action: <strong>{stats.statAction}</strong></span>
          {job?.job_id && (
            <span className="job-id-label">ID: <code>{String(job.job_id).slice(0, 8)}</code></span>
          )}
        </div>
        {logs.length > 0 && (
          <button
            type="button"
            className="toggle-logs-btn"
            onClick={() => setShowLogs((prev) => !prev)}
          >
            {showLogs ? "Hide Logs ▲" : `View Logs (${logs.length}) ▼`}
          </button>
        )}
      </div>

      <div className="stats-grid">
        <StatCard value={stats.statStatus} label="Status" />
        <StatCard value={stats.statRepos} label="Repositories" />
        <StatCard value={stats.statJira} label="Jira Tickets" />
        <StatCard value={stats.statEmbeddings} label="Embeddings" />
        <StatCard value={stats.statAction} label="Action" />
      </div>

      <div className="progress-bars-container">
        <ProgressSection
          label="Repository scanning & parsing"
          pctValue={stats.repoPct}
          meta={stats.repoEtaText}
        />
        <ProgressSection
          label="Jira tickets synchronization"
          pctValue={stats.jiraPct}
          meta={stats.jiraEtaText}
        />
        {stats.showEmbeddingSection && (
          <ProgressSection
            label={stats.embeddingLabel || "Vector embeddings generation"}
            pctValue={stats.embeddingPct}
            meta={stats.embeddingEtaText}
            indeterminate={stats.embeddingIndeterminate}
          />
        )}
      </div>

      {showLogs && logs.length > 0 && (
        <div className="job-logs-panel">
          <div className="job-logs-head">Activity Logs</div>
          <div className="job-logs-content">
            {logs.map((logItem, idx) => (
              <div key={idx} className="log-row">
                <span className="log-idx">#{idx + 1}</span>
                <span className="log-text">
                  {typeof logItem === "string" ? logItem : JSON.stringify(logItem)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({ value, label }) {
  return (
    <div className="stat-card">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function ProgressSection({ label, pctValue, meta, indeterminate }) {
  const cleanPct = Math.max(0, Math.min(100, Math.round(Number(pctValue) || 0)));
  return (
    <div className="progress-section">
      <div className="progress-label-row">
        <span className="progress-label">{label}</span>
        <span className="progress-pct-text">{indeterminate ? "Processing..." : `${cleanPct}%`}</span>
      </div>
      <div className="progress-track">
        <div
          className={`progress-fill${indeterminate ? " indeterminate" : ""}`}
          style={indeterminate ? undefined : { width: `${cleanPct}%` }}
        />
      </div>
      {meta && <div className="progress-meta">{meta}</div>}
    </div>
  );
}
