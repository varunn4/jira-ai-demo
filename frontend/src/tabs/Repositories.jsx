import { useState, useRef, useEffect, useMemo } from "react";
import { apiDownload, apiFetch } from "../api";
import JobProgress from "../components/JobProgress.jsx";
import EmbeddingsStatus from "../components/EmbeddingsStatus.jsx";

const EMBED_MODELS = ["codebase_bge_m3", "codebase_qwen3_0_6b", "codebase_mxbai_large"];

// Activity score → label + badge style. Mirrors the recency/frequency blend
// computed on the backend (see app/repository_discovery.py).
function activityTier(score) {
  if (score == null) return { cls: "idle", label: "Unknown", color: "#64748b", desc: "No Git history available", statusText: "Unscanned", recommendation: "Recalculate activity score to inspect Git log" };
  if (score >= 60) return { cls: "ok", label: "Active", color: "#10b981", desc: "Steady recent development; high commit frequency", statusText: "High Velocity", recommendation: "Auto-included in AI Graph & Test Generation" };
  if (score >= 30) return { cls: "warn", label: "Moderate", color: "#f59e0b", desc: "Commits within last 1–2 months; steady pace", statusText: "Steady Pace", recommendation: "Auto-included; steady development cadence" };
  if (score >= 1) return { cls: "run", label: "Low", color: "#38bdf8", desc: "Minimal recent changes; older development", statusText: "Low Activity", recommendation: "Optional; minimal recent changes" };
  return { cls: "idle", label: "Stale", color: "#94a3b8", desc: "Untouched for >90 days; auto-unselected to optimize builds", statusText: "Dormant / Stale", recommendation: "Auto-deselected to save tokens & build time" };
}

function computeScoreBreakdown(r) {
  const days = r.last_commit_days != null ? r.last_commit_days : null;
  const commits90 = r.commits_90d || 0;
  const commits30 = r.commits_30d || 0;
  const authors = r.authors_90d || 0;
  const lastMsg = r.last_commit_message || "";
  const lastAuthor = r.last_commit_author || "";
  const lastRel = r.last_commit_relative || (days != null ? `${Math.round(days)}d ago` : "Never");

  let recencyPts = 0;
  if (r.recency_score != null) {
    recencyPts = r.recency_score;
  } else if (days != null) {
    recencyPts = Math.round(60.0 * Math.pow(0.5, days / 30.0));
  }

  let frequencyPts = 0;
  if (r.frequency_score != null) {
    frequencyPts = r.frequency_score;
  } else {
    frequencyPts = Math.round(40.0 * Math.min(1.0, commits90 / 25.0));
  }

  const totalScore = r.activity_score != null ? r.activity_score : Math.min(100, Math.round(recencyPts + frequencyPts));

  return {
    days,
    commits90,
    commits30,
    authors,
    lastMsg,
    lastAuthor,
    lastRel,
    recencyPts,
    frequencyPts,
    totalScore,
  };
}

export default function Repositories({
  repos,
  excluded,
  selected,
  setSelected,
  reloadRepos,
  embeddingModel,
  setStatus,
  downloading: parentDownloading,
  setDownloading: parentSetDownloading,
  // Vector DB & Job execution controls
  options = {},
  setOption = () => {},
  trigger = () => {},
  busy = false,
  job = null,
  stats = null,
  embedRefreshKey = 0,
}) {
  const [isEngineOpen, setIsEngineOpen] = useState(false);
  const [showAdvancedSync, setShowAdvancedSync] = useState(false);
  const [localDownloading, setLocalDownloading] = useState(false);
  const [repomixing, setRepomixing] = useState(false);
  const [recalculating, setRecalculating] = useState(false);
  const [reportFormat, setReportFormat] = useState("pdf"); // 'pdf' | 'markdown'
  const [showInfoBanner, setShowInfoBanner] = useState(true);
  const [inspectRepo, setInspectRepo] = useState(null); // Repo object for Activity Score modal
  const [filterTier, setFilterTier] = useState("all"); // 'all' | 'active' | 'moderate' | 'low' | 'stale'
  const [searchQuery, setSearchQuery] = useState("");

  const isDownloading = parentDownloading !== undefined ? parentDownloading : localDownloading;
  const setDownloading = parentSetDownloading || setLocalDownloading;

  const downloadTimerRef = useRef(null);

  useEffect(() => {
    return () => {
      if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    };
  }, []);

  const allSelected = repos.length > 0 && selected.size === repos.length;

  // Global KPIs across discovered repositories
  const statsSummary = useMemo(() => {
    const total = repos.length;
    const active = repos.filter((r) => (r.activity_score ?? 0) >= 60).length;
    const moderate = repos.filter((r) => (r.activity_score ?? 0) >= 30 && (r.activity_score ?? 0) < 60).length;
    const low = repos.filter((r) => (r.activity_score ?? 0) >= 1 && (r.activity_score ?? 0) < 30).length;
    const stale = repos.filter((r) => (r.activity_score ?? 0) === 0).length;
    const total90dCommits = repos.reduce((acc, r) => acc + (r.commits_90d || 0), 0);
    const total30dCommits = repos.reduce((acc, r) => acc + (r.commits_30d || 0), 0);
    const totalAuthors = repos.reduce((acc, r) => acc + (r.authors_90d || 0), 0);
    const avgScore = total > 0 ? Math.round(repos.reduce((acc, r) => acc + (r.activity_score || 0), 0) / total) : 0;

    return { total, active, moderate, low, stale, total90dCommits, total30dCommits, totalAuthors, avgScore };
  }, [repos]);

  // Filter & rank repositories
  const filteredRepos = useMemo(() => {
    return repos
      .filter((r) => {
        const score = r.activity_score ?? 0;
        if (filterTier === "active" && score < 60) return false;
        if (filterTier === "moderate" && (score < 30 || score >= 60)) return false;
        if (filterTier === "low" && (score < 1 || score >= 30)) return false;
        if (filterTier === "stale" && score > 0) return false;

        if (searchQuery.trim()) {
          const q = searchQuery.toLowerCase();
          const matchName = r.name.toLowerCase().includes(q);
          const matchBranch = (r.branch || "").toLowerCase().includes(q);
          const matchMsg = (r.last_commit_message || "").toLowerCase().includes(q);
          const matchAuthor = (r.last_commit_author || "").toLowerCase().includes(q);
          if (!matchName && !matchBranch && !matchMsg && !matchAuthor) return false;
        }
        return true;
      })
      .sort((a, b) => {
        const diff = (b.activity_score ?? -1) - (a.activity_score ?? -1);
        return diff !== 0 ? diff : a.name.localeCompare(b.name);
      });
  }, [repos, filterTier, searchQuery]);

  function toggleRepo(name, checked) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(name);
      else next.delete(name);
      return next;
    });
  }

  function toggleAll(checked) {
    setSelected(checked ? new Set(repos.map((r) => r.name)) : new Set());
  }

  async function downloadCodeAnalysis() {
    if (selected.size === 0) {
      setStatus({ msg: "Select at least one repository for analysis", cls: "error" });
      return;
    }
    setDownloading(true);
    const formatLabel = reportFormat.toUpperCase();
    setStatus({ msg: `Generating ${formatLabel} code analysis report (takes ~2s)...`, cls: "running" });

    if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    downloadTimerRef.current = setTimeout(() => {
      setDownloading(false);
      setStatus({ msg: "Report request timed out. Please try again.", cls: "error" });
    }, 30000);

    try {
      const ext = reportFormat === "pdf" ? "pdf" : "md";
      const primaryName = [...selected][0] || "Code_Analysis";
      const fallbackName = `${primaryName}_Code_Analysis_Report.${ext}`;

      await apiDownload("/graph-admin/code-analysis-report", {
        method: "POST",
        body: {
          repositories: [...selected],
          include_graph_context: false,
          embedding_model: embeddingModel,
          format: reportFormat,
        },
        fallbackName,
      });
      if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
      setStatus({ msg: `Code analysis report (${formatLabel}) downloaded successfully`, cls: "ok" });
    } catch (err) {
      if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
      setStatus({ msg: `Download failed: ${err.message}`, cls: "error" });
    } finally {
      if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
      setDownloading(false);
    }
  }

  async function recalcActivity() {
    setRecalculating(true);
    setStatus({ msg: "Recalculating activity scores from Git logs...", cls: "running" });
    try {
      const count = await reloadRepos({ keepSelection: true });
      setStatus({ msg: `Activity scores recalculated for ${count} repositories`, cls: "ok" });
    } catch (err) {
      setStatus({ msg: `Recalculation failed: ${err.message}`, cls: "error" });
    } finally {
      setRecalculating(false);
    }
  }

  async function updateRepomix() {
    if (selected.size === 0) {
      setStatus({ msg: "Select at least one repository to update RepoMix data", cls: "error" });
      return;
    }
    setRepomixing(true);
    setStatus({ msg: "Updating RepoMix XML context for selected repositories...", cls: "running" });
    try {
      const data = await apiFetch("/graph-admin/repomix/reindex", {
        method: "POST",
        body: { repositories: [...selected], pull_latest_code: true, force: false },
      });
      const packed = data.packed?.length || 0;
      const skipped = data.skipped?.length || 0;
      const failed = data.failed?.length || 0;
      const unknown = data.unknown?.length || 0;
      const parts = [`${packed} repacked`, `${skipped} unchanged`];
      if (failed) parts.push(`${failed} failed`);
      if (unknown) parts.push(`${unknown} not found`);
      setStatus({
        msg: `RepoMix update complete: ${parts.join(", ")}`,
        cls: failed ? "error" : "ok",
      });
    } catch (err) {
      setStatus({ msg: `RepoMix update error: ${err.message}`, cls: "error" });
    } finally {
      setRepomixing(false);
    }
  }

  return (
    <div className="repositories-tab-content">
      {/* Vector Database & Knowledge Sync Engine Card (Collapsible) */}
      <div
        className="card vector-hub-card"
        style={{
          marginBottom: "20px",
          border: "1px solid var(--accent-subtle, #c7d2fe)",
          background: "linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)",
          padding: isEngineOpen || busy || job ? "18px 22px" : "12px 20px",
        }}
      >
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "14px",
            marginBottom: isEngineOpen || busy || job ? "14px" : "0",
          }}
        >
          <div
            style={{ display: "flex", alignItems: "center", gap: "10px", cursor: "pointer", userSelect: "none" }}
            onClick={() => setIsEngineOpen(!isEngineOpen)}
          >
            <span style={{ fontSize: "16px" }}>⚡</span>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                <h2
                  style={{
                    fontSize: "15px",
                    fontWeight: "700",
                    margin: 0,
                    color: "var(--ink, #0f172a)",
                  }}
                >
                  Vector Database & Knowledge Sync Engine
                </h2>
                <span className="badge badge-indigo">Qdrant Vector DB</span>
                {!(isEngineOpen || busy || job) && (
                  <span style={{ fontSize: "12px", color: "var(--muted, #64748b)" }}>
                    ({options.embeddingModel || "codebase_bge_m3"})
                  </span>
                )}
              </div>
              {(isEngineOpen || busy || job) && (
                <p
                  style={{
                    margin: "3px 0 0 0",
                    fontSize: "12.5px",
                    color: "var(--muted, #64748b)",
                  }}
                >
                  Sync repository codebases, parse ASTs, embed Jira ticket insights, and regenerate the Qdrant semantic index.
                </p>
              )}
            </div>
          </div>

          {/* Right side controls / Toggle Button */}
          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
            {!(isEngineOpen || busy || job) && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => trigger("update")}
                disabled={busy}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  fontWeight: "600",
                  fontSize: "12.5px",
                  padding: "6px 14px",
                  minHeight: "32px",
                }}
              >
                <span>⚡</span> Sync & Update
              </button>
            )}

            {(isEngineOpen || busy || job) && (
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <label
                  style={{
                    fontSize: "12px",
                    fontWeight: "600",
                    color: "var(--muted, #64748b)",
                    margin: 0,
                  }}
                >
                  Embedding Model:
                </label>
                <select
                  value={options.embeddingModel || "codebase_bge_m3"}
                  onChange={(e) => setOption("embeddingModel", e.target.value)}
                  disabled={busy}
                  style={{
                    padding: "5px 10px",
                    fontSize: "12px",
                    borderRadius: "6px",
                    border: "1px solid var(--line, #cbd5e1)",
                    background: "#ffffff",
                    fontWeight: "600",
                    cursor: "pointer",
                  }}
                >
                  {EMBED_MODELS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <button
              type="button"
              onClick={() => setIsEngineOpen(!isEngineOpen)}
              style={{
                background: "#ffffff",
                border: "1px solid var(--line, #cbd5e1)",
                color: "var(--accent-strong, #4338ca)",
                padding: "5px 12px",
                borderRadius: "6px",
                fontSize: "12px",
                fontWeight: "600",
                cursor: "pointer",
                minHeight: "32px",
                display: "inline-flex",
                alignItems: "center",
                gap: "4px",
              }}
            >
              {isEngineOpen || busy || job ? "▲ Collapse Controls" : "▼ Expand Controls & Embeddings"}
            </button>
          </div>
        </div>

        {/* Collapsible Body */}
        {(isEngineOpen || busy || job) && (
          <div style={{ marginTop: "14px", borderTop: "1px solid var(--line, #e2e8f0)", paddingTop: "14px" }}>
            {/* Active Job Progress View */}
            {(busy || job) && (
              <div style={{ marginBottom: "16px" }}>
                <JobProgress stats={stats} job={job} />
              </div>
            )}

            {/* Action Buttons Hub (5 Distinct Actions) */}
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: "10px",
                alignItems: "center",
              }}
            >
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => trigger("update")}
                disabled={busy}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  fontWeight: "600",
                }}
              >
                <span>⚡</span> Incremental Sync & Update
              </button>

              <button
                type="button"
                className="btn"
                onClick={() => trigger("jira_tickets_only")}
                disabled={busy}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  fontWeight: "600",
                  background: "#ffffff",
                  border: "1px solid var(--line, #cbd5e1)",
                  color: "var(--ink, #0f172a)",
                }}
              >
                <span>🎫</span> Sync Jira Tickets Only
              </button>

              <button
                type="button"
                className="btn"
                onClick={() => trigger("codebase_only")}
                disabled={busy}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  fontWeight: "600",
                  background: "#ffffff",
                  border: "1px solid var(--line, #cbd5e1)",
                  color: "var(--accent-strong, #4338ca)",
                }}
              >
                <span>🧠</span> Codebase Embeddings Only
              </button>

              <button
                type="button"
                className="btn"
                onClick={() => trigger("jira_embeddings_only")}
                disabled={busy}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  fontWeight: "600",
                  background: "#ffffff",
                  border: "1px solid var(--line, #cbd5e1)",
                  color: "#059669",
                }}
              >
                <span>🏷️</span> Jira Embeddings Only
              </button>

              <button
                type="button"
                className="btn"
                onClick={() => {
                  if (
                    window.confirm(
                      "Are you sure you want to perform a FULL Vector DB Rebuild? This will re-index all selected repositories from scratch."
                    )
                  ) {
                    trigger("create_new");
                  }
                }}
                disabled={busy}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  fontWeight: "600",
                  background: "#fef2f2",
                  border: "1px solid #fecaca",
                  color: "#dc2626",
                }}
              >
                <span>🔄</span> Full Rebuild Vector DB
              </button>

              <button
                type="button"
                onClick={() => setShowAdvancedSync(!showAdvancedSync)}
                style={{
                  background: "none",
                  border: "none",
                  color: "var(--accent-strong, #4338ca)",
                  fontSize: "12px",
                  fontWeight: "600",
                  cursor: "pointer",
                  marginLeft: "auto",
                  textDecoration: "underline",
                }}
              >
                {showAdvancedSync ? "Hide Advanced Options ▲" : "Show Advanced Options ▼"}
              </button>
            </div>

            {/* Collapsible Advanced Options Panel */}
            {showAdvancedSync && (
              <div
                style={{
                  marginTop: "14px",
                  padding: "12px 16px",
                  background: "#f1f5f9",
                  borderRadius: "6px",
                  border: "1px solid #e2e8f0",
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                  gap: "10px",
                }}
              >
                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    fontSize: "12px",
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={options.pullLatestCode !== false}
                    onChange={(e) => setOption("pullLatestCode", e.target.checked)}
                    disabled={busy}
                  />
                  <span>Git Pull Latest Code</span>
                </label>

                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    fontSize: "12px",
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={options.fetchLatestJira !== false}
                    onChange={(e) => setOption("fetchLatestJira", e.target.checked)}
                    disabled={busy}
                  />
                  <span>Fetch Latest Jira Tickets</span>
                </label>

                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    fontSize: "12px",
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={options.includeJira !== false}
                    onChange={(e) => setOption("includeJira", e.target.checked)}
                    disabled={busy}
                  />
                  <span>Include Jira Tickets in Graph</span>
                </label>

                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    fontSize: "12px",
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={options.buildEmbeddings !== false}
                    onChange={(e) => setOption("buildEmbeddings", e.target.checked)}
                    disabled={busy}
                  />
                  <span>Build Codebase Embeddings</span>
                </label>
              </div>
            )}

            {/* Realtime Collection Stats status panel */}
            <div
              style={{
                marginTop: "14px",
                borderTop: "1px dashed var(--line, #e2e8f0)",
                paddingTop: "10px",
              }}
            >
              <EmbeddingsStatus refreshKey={embedRefreshKey} />
            </div>
          </div>
        )}
      </div>

      {/* 4 Sleek Fleet KPI Metric Cards */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
        gap: "12px",
        marginBottom: "16px",
      }}>
        <div style={{
          background: "#ffffff",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "var(--r-sm, 8px)",
          padding: "12px 18px",
          boxShadow: "var(--shadow-xs, 0 1px 2px rgba(0,0,0,0.05))",
        }}>
          <div style={{ fontSize: "11px", fontWeight: "700", color: "var(--muted, #64748b)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
            Active Repositories
          </div>
          <div style={{ fontSize: "20px", fontWeight: "800", color: "var(--ok, #059669)", marginTop: "4px" }}>
            {statsSummary.active} <span style={{ fontSize: "13px", color: "var(--muted, #64748b)", fontWeight: "500" }}>/ {statsSummary.total} total</span>
          </div>
          <div style={{ fontSize: "11.5px", color: "var(--muted, #64748b)", marginTop: "2px" }}>Auto-selected for build graph</div>
        </div>

        <div style={{
          background: "#ffffff",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "var(--r-sm, 8px)",
          padding: "12px 18px",
          boxShadow: "var(--shadow-xs, 0 1px 2px rgba(0,0,0,0.05))",
        }}>
          <div style={{ fontSize: "11px", fontWeight: "700", color: "var(--muted, #64748b)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
            90-Day Git Commits
          </div>
          <div style={{ fontSize: "20px", fontWeight: "800", color: "var(--accent-strong, #4338ca)", marginTop: "4px" }}>
            {statsSummary.total90dCommits} <span style={{ fontSize: "13px", color: "var(--muted, #64748b)", fontWeight: "500" }}>commits</span>
          </div>
          <div style={{ fontSize: "11.5px", color: "var(--muted, #64748b)", marginTop: "2px" }}>{statsSummary.total30dCommits} in last 30 days</div>
        </div>

        <div style={{
          background: "#ffffff",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "var(--r-sm, 8px)",
          padding: "12px 18px",
          boxShadow: "var(--shadow-xs, 0 1px 2px rgba(0,0,0,0.05))",
        }}>
          <div style={{ fontSize: "11px", fontWeight: "700", color: "var(--muted, #64748b)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
            Active Contributors
          </div>
          <div style={{ fontSize: "20px", fontWeight: "800", color: "#7c3aed", marginTop: "4px" }}>
            {statsSummary.totalAuthors} <span style={{ fontSize: "13px", color: "var(--muted, #64748b)", fontWeight: "500" }}>authors</span>
          </div>
          <div style={{ fontSize: "11.5px", color: "var(--muted, #64748b)", marginTop: "2px" }}>Across workspace codebases</div>
        </div>

        <div style={{
          background: "#ffffff",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "var(--r-sm, 8px)",
          padding: "12px 18px",
          boxShadow: "var(--shadow-xs, 0 1px 2px rgba(0,0,0,0.05))",
        }}>
          <div style={{ fontSize: "11px", fontWeight: "700", color: "var(--muted, #64748b)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
            Avg Vitality Score
          </div>
          <div style={{ fontSize: "20px", fontWeight: "800", color: "#d97706", marginTop: "4px" }}>
            {statsSummary.avgScore} <span style={{ fontSize: "13px", color: "var(--muted, #64748b)", fontWeight: "500" }}>/ 100</span>
          </div>
          <div style={{ fontSize: "11.5px", color: "var(--muted, #64748b)", marginTop: "2px" }}>Blended recency + velocity</div>
        </div>
      </div>

      {/* Unified Compact Filter & Action Control Bar */}
      <div style={{
        background: "#ffffff",
        border: "1px solid var(--line, #e2e8f0)",
        borderRadius: "var(--r-sm, 8px)",
        padding: "10px 14px",
        marginBottom: "16px",
        display: "flex",
        flexWrap: "wrap",
        justifyContent: "space-between",
        alignItems: "center",
        gap: "12px",
        boxShadow: "var(--shadow-xs, 0 1px 2px rgba(0,0,0,0.05))",
      }}>
        {/* Left Side: Select All + Filter Pills */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "10px" }}>
          <label style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "8px",
            margin: 0,
            cursor: "pointer",
            fontSize: "13px",
            fontWeight: "600",
            color: "var(--ink, #0f172a)",
            userSelect: "none",
          }}>
            <input
              type="checkbox"
              checked={allSelected}
              onChange={(e) => toggleAll(e.target.checked)}
              style={{ width: "16px", height: "16px", margin: 0, cursor: "pointer" }}
            />
            <span>Select all ({selected.size}/{repos.length})</span>
          </label>

          <div style={{ width: "1px", height: "20px", background: "var(--line, #e2e8f0)" }} />

          {/* Pill Group */}
          <div style={{
            display: "inline-flex",
            alignItems: "center",
            background: "var(--surface-2, #eef2f9)",
            padding: "3px",
            borderRadius: "20px",
            gap: "3px",
          }}>
            <button
              type="button"
              onClick={() => setFilterTier("all")}
              style={{
                width: "auto",
                minHeight: "unset",
                padding: "4px 10px",
                borderRadius: "14px",
                fontSize: "12px",
                fontWeight: "600",
                border: "none",
                cursor: "pointer",
                background: filterTier === "all" ? "#ffffff" : "transparent",
                color: filterTier === "all" ? "var(--ink, #0f172a)" : "var(--muted, #64748b)",
                boxShadow: filterTier === "all" ? "0 1px 2px rgba(0,0,0,0.08)" : "none",
                transform: "none",
              }}
            >
              All ({statsSummary.total})
            </button>

            <button
              type="button"
              onClick={() => setFilterTier("active")}
              style={{
                width: "auto",
                minHeight: "unset",
                padding: "4px 10px",
                borderRadius: "14px",
                fontSize: "12px",
                fontWeight: "600",
                border: "none",
                cursor: "pointer",
                background: filterTier === "active" ? "#ffffff" : "transparent",
                color: filterTier === "active" ? "var(--ok, #059669)" : "var(--muted, #64748b)",
                boxShadow: filterTier === "active" ? "0 1px 2px rgba(0,0,0,0.08)" : "none",
                transform: "none",
              }}
            >
              🟢 Active ({statsSummary.active})
            </button>

            <button
              type="button"
              onClick={() => setFilterTier("moderate")}
              style={{
                width: "auto",
                minHeight: "unset",
                padding: "4px 10px",
                borderRadius: "14px",
                fontSize: "12px",
                fontWeight: "600",
                border: "none",
                cursor: "pointer",
                background: filterTier === "moderate" ? "#ffffff" : "transparent",
                color: filterTier === "moderate" ? "var(--warn, #d97706)" : "var(--muted, #64748b)",
                boxShadow: filterTier === "moderate" ? "0 1px 2px rgba(0,0,0,0.08)" : "none",
                transform: "none",
              }}
            >
              🟡 Moderate ({statsSummary.moderate})
            </button>

            <button
              type="button"
              onClick={() => setFilterTier("low")}
              style={{
                width: "auto",
                minHeight: "unset",
                padding: "4px 10px",
                borderRadius: "14px",
                fontSize: "12px",
                fontWeight: "600",
                border: "none",
                cursor: "pointer",
                background: filterTier === "low" ? "#ffffff" : "transparent",
                color: filterTier === "low" ? "#0284c7" : "var(--muted, #64748b)",
                boxShadow: filterTier === "low" ? "0 1px 2px rgba(0,0,0,0.08)" : "none",
                transform: "none",
              }}
            >
              🔵 Low ({statsSummary.low})
            </button>

            <button
              type="button"
              onClick={() => setFilterTier("stale")}
              style={{
                width: "auto",
                minHeight: "unset",
                padding: "4px 10px",
                borderRadius: "14px",
                fontSize: "12px",
                fontWeight: "600",
                border: "none",
                cursor: "pointer",
                background: filterTier === "stale" ? "#ffffff" : "transparent",
                color: filterTier === "stale" ? "#475569" : "var(--muted, #64748b)",
                boxShadow: filterTier === "stale" ? "0 1px 2px rgba(0,0,0,0.08)" : "none",
                transform: "none",
              }}
            >
              ⚪ Stale ({statsSummary.stale})
            </button>
          </div>
        </div>

        {/* Right Side: Search + Action Buttons */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px" }}>
          {/* Compact Search Bar */}
          <div style={{ position: "relative", width: "190px" }}>
            <input
              type="text"
              placeholder="Search repositories..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              style={{
                width: "100%",
                padding: "6px 24px 6px 26px",
                fontSize: "12px",
                background: "var(--surface, #f7f9fd)",
                border: "1px solid var(--line-strong, #d3dae8)",
                borderRadius: "6px",
                color: "var(--ink, #0f172a)",
                height: "34px",
                margin: 0,
              }}
            />
            <span style={{ position: "absolute", left: "8px", top: "9px", fontSize: "11px", opacity: 0.5 }}>🔍</span>
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery("")}
                style={{
                  position: "absolute",
                  right: "6px",
                  top: "6px",
                  width: "auto",
                  minHeight: "unset",
                  padding: "2px 4px",
                  background: "transparent",
                  border: "none",
                  color: "var(--muted, #64748b)",
                  cursor: "pointer",
                  fontSize: "11px",
                  boxShadow: "none",
                  transform: "none",
                }}
              >
                ✕
              </button>
            )}
          </div>

          {/* Recalculate Button */}
          <button
            type="button"
            className="secondary"
            disabled={recalculating}
            onClick={recalcActivity}
            style={{
              width: "auto",
              minHeight: "unset",
              height: "34px",
              padding: "0 12px",
              fontSize: "12.5px",
              fontWeight: "600",
              display: "inline-flex",
              alignItems: "center",
              gap: "5px",
              cursor: recalculating ? "not-allowed" : "pointer",
            }}
            title="Scan Git history across local clones to update commit statistics and vitality ranking"
          >
            {recalculating ? "⏳ Recalculating…" : "🔄 Recalculate"}
          </button>

          {/* Download Report Button + Format Dropdown */}
          <div style={{
            display: "inline-flex",
            borderRadius: "var(--r-sm, 6px)",
            overflow: "hidden",
            border: "1px solid var(--line-strong, #d3dae8)",
            boxShadow: "var(--shadow-xs)",
            height: "34px",
          }}>
            <button
              type="button"
              className="secondary"
              disabled={isDownloading}
              onClick={downloadCodeAnalysis}
              style={{
                width: "auto",
                minHeight: "unset",
                height: "100%",
                border: "none",
                borderRadius: "0",
                margin: 0,
                padding: "0 12px",
                fontSize: "12.5px",
                fontWeight: "600",
                display: "inline-flex",
                alignItems: "center",
                gap: "5px",
                background: isDownloading ? "var(--accent-soft)" : "#ffffff",
                color: "var(--accent-strong, #4338ca)",
                cursor: isDownloading ? "not-allowed" : "pointer",
                boxShadow: "none",
                transform: "none",
              }}
              title={`Download code analysis report as ${reportFormat.toUpperCase()}`}
            >
              {isDownloading ? `⏳ Exporting ${reportFormat.toUpperCase()}...` : `📄 Report (${reportFormat.toUpperCase()})`}
            </button>
            <select
              value={reportFormat}
              onChange={(e) => setReportFormat(e.target.value)}
              disabled={isDownloading}
              style={{
                background: "var(--surface, #f7f9fd)",
                color: "var(--ink, #0f172a)",
                border: "none",
                borderLeft: "1px solid var(--line-strong, #d3dae8)",
                padding: "0 6px",
                fontSize: "11.5px",
                fontWeight: "600",
                cursor: "pointer",
                height: "100%",
              }}
              title="Choose report export format"
            >
              <option value="pdf">PDF</option>
              <option value="markdown">MD</option>
            </select>
          </div>

          {/* Update RepoMix Button */}
          <button
            type="button"
            className="secondary"
            disabled={repomixing}
            onClick={updateRepomix}
            style={{
              width: "auto",
              minHeight: "unset",
              height: "34px",
              padding: "0 12px",
              fontSize: "12.5px",
              fontWeight: "600",
              display: "inline-flex",
              alignItems: "center",
              gap: "5px",
              cursor: repomixing ? "not-allowed" : "pointer",
            }}
            title="Pack selected repositories into token-efficient XML files for LLM test generation"
          >
            {repomixing ? "⏳ Packing..." : "📦 RepoMix"}
          </button>
        </div>
      </div>

      {/* Clean Table Card */}
      <div style={{
        background: "#ffffff",
        border: "1px solid var(--line, #e2e8f0)",
        borderRadius: "var(--r-sm, 8px)",
        boxShadow: "var(--shadow-xs, 0 1px 2px rgba(0,0,0,0.05))",
        overflow: "hidden",
      }}>
        <table style={{ margin: 0, width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
          <thead>
            <tr style={{ background: "var(--surface, #f7f9fd)", borderBottom: "1px solid var(--line, #e2e8f0)" }}>
              <th style={{ position: "static", width: "5%", padding: "12px 10px", textAlign: "center" }}>Use</th>
              <th style={{ position: "static", width: "5%", padding: "12px 8px", textAlign: "center" }}>Rank</th>
              <th style={{ position: "static", width: "22%", padding: "12px 14px" }}>Repository</th>
              <th style={{ position: "static", width: "38%", padding: "12px 14px" }}>Activity & Vitality Telemetry</th>
              <th style={{ position: "static", width: "16%", padding: "12px 14px" }}>Local Clone Path</th>
              <th style={{ position: "static", width: "14%", padding: "12px 14px" }}>Branch / Commit</th>
            </tr>
          </thead>
          <tbody>
            {filteredRepos.length === 0 ? (
              <tr>
                <td colSpan={6} style={{ textAlign: "center", padding: "32px", color: "var(--muted, #64748b)" }}>
                  No repositories match the current filter or search criteria.
                </td>
              </tr>
            ) : (
              filteredRepos.map((r, i) => {
                const tier = activityTier(r.activity_score);
                const b = computeScoreBreakdown(r);

                return (
                  <tr key={r.name} style={{
                    borderBottom: "1px solid var(--line, #e2e8f0)",
                    verticalAlign: "middle",
                    transition: "background 0.15s ease",
                  }}>
                    <td style={{ padding: "12px 12px", textAlign: "center" }}>
                      <input
                        type="checkbox"
                        checked={selected.has(r.name)}
                        onChange={(e) => toggleRepo(r.name, e.target.checked)}
                        style={{ width: "16px", height: "16px", cursor: "pointer", margin: 0 }}
                      />
                    </td>
                    <td className="meta" style={{ fontWeight: "700", textAlign: "center", color: "var(--muted, #64748b)" }}>
                      {i + 1}
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <div style={{ fontWeight: "700", fontSize: "13.5px", color: "var(--ink, #0f172a)" }}>
                        {r.name}
                      </div>
                      <div style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "4px",
                        marginTop: "4px",
                        fontSize: "11px",
                        padding: "1px 6px",
                        borderRadius: "4px",
                        fontWeight: "600",
                        background: selected.has(r.name) ? "var(--ok-soft, #ecfdf5)" : "var(--surface-2, #eef2f9)",
                        color: selected.has(r.name) ? "var(--ok, #059669)" : "var(--muted, #64748b)",
                        border: "1px solid " + (selected.has(r.name) ? "rgba(5, 150, 105, 0.2)" : "rgba(100, 116, 139, 0.15)"),
                      }}>
                        {selected.has(r.name) ? "✓ Auto-Indexed" : "○ Skipped"}
                      </div>
                    </td>

                    {/* Activity & Vitality Telemetry */}
                    <td style={{ padding: "12px 14px" }}>
                      <div style={{ display: "flex", flexDirection: "column", gap: "5px" }}>
                        {/* Top Line: Badge + Label + Insights button */}
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" }}>
                          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                            <span
                              className={`badge ${tier.cls}`}
                              style={{
                                fontSize: "11.5px",
                                padding: "2px 8px",
                                fontWeight: "750",
                                cursor: "pointer",
                                whiteSpace: "nowrap",
                              }}
                              onClick={() => setInspectRepo(r)}
                              title="Click to view detailed mathematical breakdown"
                            >
                              {b.totalScore} / 100
                            </span>

                            <span style={{ fontSize: "12px", fontWeight: "600", color: tier.color }}>
                              {tier.statusText}
                            </span>
                          </div>

                          <button
                            type="button"
                            onClick={() => setInspectRepo(r)}
                            style={{
                              width: "auto",
                              minHeight: "unset",
                              padding: "2px 8px",
                              fontSize: "11px",
                              fontWeight: "600",
                              background: "transparent",
                              border: "1px solid var(--line-strong, #d3dae8)",
                              borderRadius: "4px",
                              color: "var(--accent-strong, #4338ca)",
                              cursor: "pointer",
                              boxShadow: "none",
                              transform: "none",
                            }}
                          >
                            Insights 📊
                          </button>
                        </div>

                        {/* Split Progress Meter (Recency + Velocity) */}
                        <div style={{
                          height: "5px",
                          background: "var(--surface-2, #eef2f9)",
                          borderRadius: "3px",
                          overflow: "hidden",
                          display: "flex",
                        }} title={`Recency: ${b.recencyPts}/60 pts · Velocity: ${b.frequencyPts}/40 pts`}>
                          <div style={{ height: "100%", width: `${Math.min(60, b.recencyPts)}%`, background: "#0284c7" }} />
                          <div style={{ height: "100%", width: `${Math.min(40, b.frequencyPts)}%`, background: "var(--ok, #059669)" }} />
                        </div>

                        {/* Metric summary line */}
                        <div style={{ fontSize: "11.5px", color: "var(--muted, #64748b)", display: "flex", flexWrap: "wrap", gap: "6px" }}>
                          <span>⏱️ Last commit: <strong style={{ color: "var(--ink, #0f172a)" }}>{b.lastRel}</strong></span>
                          <span>·</span>
                          <span>📈 <strong style={{ color: "var(--ink, #0f172a)" }}>{b.commits90}</strong> commits (90d)</span>
                          <span>·</span>
                          <span>👥 <strong style={{ color: "var(--ink, #0f172a)" }}>{b.authors}</strong> {b.authors === 1 ? "author" : "authors"}</span>
                        </div>

                        {/* Last Commit message */}
                        {b.lastMsg && (
                          <div style={{
                            fontSize: "11.5px",
                            color: "var(--ink-soft, #334155)",
                            whiteSpace: "nowrap",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            maxWidth: "440px",
                          }} title={b.lastMsg}>
                            <span style={{ color: "var(--muted, #64748b)" }}>💬 &ldquo;{b.lastMsg}&rdquo;</span>
                            {b.lastAuthor && <span style={{ color: "var(--muted, #94a3b8)" }}> — {b.lastAuthor}</span>}
                          </div>
                        )}
                      </div>
                    </td>

                    <td className="meta" style={{ padding: "12px 14px", fontFamily: "monospace", fontSize: "12px", wordBreak: "break-all", color: "var(--muted, #64748b)" }}>
                      {r.path}
                    </td>

                    <td style={{ padding: "12px 14px" }}>
                      <div style={{ fontSize: "12.5px", fontWeight: "600", color: "var(--ink, #0f172a)" }}>
                        {r.branch || "main"}
                      </div>
                      <div style={{ fontFamily: "monospace", fontSize: "11.5px", color: "var(--muted, #64748b)", marginTop: "2px" }}>
                        {(r.current_commit || "-").slice(0, 10)}
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Activity Insights Breakdown Modal */}
      {inspectRepo && (
        <div className="modal-backdrop" onClick={() => setInspectRepo(null)} style={{
          position: "fixed",
          inset: 0,
          background: "rgba(15, 23, 42, 0.45)",
          backdropFilter: "blur(4px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 100,
          padding: "20px",
        }}>
          <div
            style={{
              background: "#ffffff",
              border: "1px solid var(--line, #e2e8f0)",
              borderRadius: "var(--r, 12px)",
              maxWidth: "520px",
              width: "100%",
              padding: "24px",
              boxShadow: "var(--shadow-lg, 0 20px 25px -5px rgba(0,0,0,0.1))",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "16px" }}>
              <div>
                <span className={`badge ${activityTier(inspectRepo.activity_score).cls}`} style={{ marginBottom: "6px" }}>
                  {activityTier(inspectRepo.activity_score).label} ({inspectRepo.activity_score ?? 0}/100)
                </span>
                <h3 style={{ margin: 0, fontSize: "17px", fontWeight: "800", color: "var(--ink, #0f172a)" }}>
                  Activity Score: <span style={{ color: "var(--accent, #4f46e5)" }}>{inspectRepo.name}</span>
                </h3>
              </div>
              <button
                type="button"
                onClick={() => setInspectRepo(null)}
                style={{
                  width: "auto",
                  minHeight: "unset",
                  background: "transparent",
                  border: "none",
                  color: "var(--muted, #64748b)",
                  fontSize: "18px",
                  cursor: "pointer",
                  padding: "4px 8px",
                  boxShadow: "none",
                  transform: "none",
                }}
              >
                ✕
              </button>
            </div>

            {/* Score Breakdown Content */}
            {(() => {
              const b = computeScoreBreakdown(inspectRepo);
              const tier = activityTier(inspectRepo.activity_score);
              return (
                <div>
                  <div style={{
                    background: "var(--surface, #f7f9fd)",
                    border: "1px solid var(--line, #e2e8f0)",
                    borderRadius: "var(--r-sm, 8px)",
                    padding: "14px",
                    marginBottom: "14px",
                  }}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "6px" }}>
                      <span style={{ fontSize: "13px", fontWeight: "700", color: "var(--ink, #0f172a)" }}>Total Score</span>
                      <span style={{ fontSize: "14px", fontWeight: "800", color: tier.color }}>{b.totalScore} / 100 pts</span>
                    </div>
                    <div style={{ height: "6px", background: "var(--surface-2, #eef2f9)", borderRadius: "3px", overflow: "hidden" }}>
                      <div style={{ height: "100%", width: `${b.totalScore}%`, background: tier.color, borderRadius: "3px" }} />
                    </div>
                    <div style={{ fontSize: "12px", color: "var(--muted, #64748b)", marginTop: "6px" }}>
                      {tier.desc}
                    </div>
                  </div>

                  <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                    {/* Recency Signal */}
                    <div style={{
                      background: "#ffffff",
                      border: "1px solid var(--line, #e2e8f0)",
                      borderRadius: "var(--r-sm, 8px)",
                      padding: "12px 14px",
                    }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                        <span style={{ fontWeight: "700", color: "#0284c7", fontSize: "12.5px" }}>
                          ⏱️ Commit Recency (60% weight)
                        </span>
                        <span style={{ fontWeight: "800", color: "#0284c7", fontSize: "13px" }}>
                          {b.recencyPts} / 60 pts
                        </span>
                      </div>
                      <div style={{ fontSize: "12px", color: "var(--ink-soft, #334155)", marginTop: "4px" }}>
                        Last commit was <strong>{b.lastRel}</strong>.
                      </div>
                      <div style={{ fontSize: "11px", color: "var(--muted, #64748b)", marginTop: "2px", fontFamily: "monospace" }}>
                        Decay: 60 × (0.5 ^ ({b.days != null ? Math.round(b.days) : 90}d / 30d))
                      </div>
                    </div>

                    {/* Velocity Signal */}
                    <div style={{
                      background: "#ffffff",
                      border: "1px solid var(--line, #e2e8f0)",
                      borderRadius: "var(--r-sm, 8px)",
                      padding: "12px 14px",
                    }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                        <span style={{ fontWeight: "700", color: "var(--ok, #059669)", fontSize: "12.5px" }}>
                          📈 Commit Velocity (40% weight)
                        </span>
                        <span style={{ fontWeight: "800", color: "var(--ok, #059669)", fontSize: "13px" }}>
                          {b.frequencyPts} / 40 pts
                        </span>
                      </div>
                      <div style={{ fontSize: "12px", color: "var(--ink-soft, #334155)", marginTop: "4px" }}>
                        <strong>{b.commits90} commits</strong> in last 90 days ({b.commits30} in last 30 days).
                      </div>
                      <div style={{ fontSize: "11px", color: "var(--muted, #64748b)", marginTop: "2px", fontFamily: "monospace" }}>
                        Saturation: 40 × min(1.0, {b.commits90} / 25 commits)
                      </div>
                    </div>

                    {/* Team Authors */}
                    <div style={{
                      background: "#ffffff",
                      border: "1px solid var(--line, #e2e8f0)",
                      borderRadius: "var(--r-sm, 8px)",
                      padding: "12px 14px",
                    }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                        <span style={{ fontWeight: "700", color: "#7c3aed", fontSize: "12.5px" }}>
                          👥 Team Contributors
                        </span>
                        <span style={{ fontWeight: "800", color: "#7c3aed", fontSize: "13px" }}>
                          {b.authors} {b.authors === 1 ? "author" : "authors"}
                        </span>
                      </div>
                      <div style={{ fontSize: "12px", color: "var(--ink-soft, #334155)", marginTop: "4px" }}>
                        {b.authors} active developer{b.authors === 1 ? "" : "s"} committed changes in the last 90 days.
                      </div>
                    </div>
                  </div>

                  <div style={{ marginTop: "16px", display: "flex", justifyContent: "flex-end" }}>
                    <button
                      type="button"
                      className="primary"
                      onClick={() => setInspectRepo(null)}
                      style={{ width: "auto", minHeight: "unset", padding: "8px 18px", fontSize: "13px" }}
                    >
                      Close Insights
                    </button>
                  </div>
                </div>
              );
            })()}
          </div>
        </div>
      )}
    </div>
  );
}
