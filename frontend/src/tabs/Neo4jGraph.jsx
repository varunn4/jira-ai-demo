import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../api";
import { CaretUp, CaretDown } from "@phosphor-icons/react";

// Build modes map to the backend `wipe_mode`.
const BUILD_MODES = [
  { value: "managed", label: "Update — replace code/git graph, keep Jira & embeddings" },
  { value: "all", label: "Full rebuild — wipe the ENTIRE database first" },
  { value: "none", label: "Merge — no wipe, upsert in place (Single repo update)" },
];

const CATCHY_BUILD_PHRASES = [
  "Extracting abstract syntax trees (AST) with Tree-Sitter...",
  "Mapping class hierarchies, functions, and cross-module dependencies...",
  "Mining Git commit timelines, code churn, and author ownership...",
  "Synthesizing high-dimensional code knowledge graph in Neo4j...",
  "Linking callers, imports, interfaces, and architecture layers...",
  "Finalizing graph indexes for instant semantic traversals...",
];

function activityTier(score) {
  if (score == null) return { cls: "idle", label: "Unknown" };
  if (score >= 60) return { cls: "ok", label: "Active" };
  if (score >= 30) return { cls: "warn", label: "Moderate" };
  if (score >= 1) return { cls: "err", label: "Low" };
  return { cls: "idle", label: "Ready" };
}

function numberFmt(n) {
  return (n ?? 0).toLocaleString();
}

export default function Neo4jGraph({ setStatus }) {
  const [analytics, setAnalytics] = useState(null);
  const [repos, setRepos] = useState([]);
  const [selected, setSelected] = useState(() => new Set());
  const [mode, setMode] = useState("managed");
  const [includeCode, setIncludeCode] = useState(true);
  const [pullLatest, setPullLatest] = useState(true);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [showRawLogs, setShowRawLogs] = useState(false);
  const [phraseIdx, setPhraseIdx] = useState(0);

  const pollRef = useRef(null);
  const phraseIntervalRef = useRef(null);

  const loadAnalytics = useCallback(async () => {
    const data = await apiFetch("/graph-admin/neo4j/analytics");
    setAnalytics(data);
    return data;
  }, []);

  const loadRepos = useCallback(async () => {
    const data = await apiFetch("/graph-admin/neo4j/active-repositories");
    const list = data.repositories || [];
    setRepos(list);
    // Select all available repositories by default if none selected yet
    setSelected((prev) => (prev.size === 0 ? new Set(list.map((r) => r.name)) : prev));
    return list;
  }, []);

  async function handleRefreshAnalytics() {
    setRefreshing(true);
    try {
      await Promise.all([loadAnalytics(), loadRepos()]);
      setStatus({ msg: "Neo4j graph analytics refreshed successfully.", cls: "ok" });
    } catch (err) {
      setStatus({ msg: `Failed to refresh analytics: ${err.message}`, cls: "error" });
    } finally {
      setRefreshing(false);
    }
  }

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        await Promise.all([loadAnalytics(), loadRepos()]);
      } catch (err) {
        if (active) setStatus({ msg: `Neo4j load failed: ${err.message}`, cls: "error" });
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [loadAnalytics, loadRepos, setStatus]);

  useEffect(() => {
    if (busy) {
      phraseIntervalRef.current = setInterval(() => {
        setPhraseIdx((prev) => (prev + 1) % CATCHY_BUILD_PHRASES.length);
      }, 3500);
    } else {
      if (phraseIntervalRef.current) clearInterval(phraseIntervalRef.current);
    }
    return () => {
      if (phraseIntervalRef.current) clearInterval(phraseIntervalRef.current);
    };
  }, [busy]);

  useEffect(() => () => pollRef.current && clearInterval(pollRef.current), []);

  function startPolling(jobId) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const data = await apiFetch(`/graph-admin/neo4j/jobs/${jobId}`);
        setJob(data);
        if (data.status === "completed" || data.status === "failed") {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setBusy(false);
          if (data.status === "completed") {
            setStatus({ msg: "Neo4j graph build completed successfully.", cls: "ok" });
            loadAnalytics().catch(() => {});
            loadRepos().catch(() => {});
          } else {
            setStatus({ msg: `Neo4j build failed: ${data.error || "unknown"}`, cls: "error" });
          }
        } else {
          const done = data.progress?.repositories_done ?? 0;
          const total = data.totals?.repositories ?? 0;
          setStatus({ msg: `Building graph… ${done}/${total} repositories`, cls: "running" });
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, 2000);
  }

  async function build() {
    if (selected.size === 0) {
      setStatus({ msg: "Please select at least one repository to build the graph.", cls: "error" });
      return;
    }
    if (
      mode === "all" &&
      !window.confirm(
        "Full rebuild will DETACH DELETE the entire Neo4j database (including Jira nodes " +
          "and embeddings) before writing. Continue?"
      )
    ) {
      return;
    }
    setBusy(true);
    setJob(null);
    setStatus({ msg: "Starting Neo4j graph build…", cls: "running" });
    try {
      const data = await apiFetch("/graph-admin/neo4j/build", {
        method: "POST",
        body: {
          repositories: [...selected],
          wipe_mode: mode,
          include_code: includeCode,
          pull_latest: pullLatest,
        },
      });
      setStatus({
        msg: `Build started for ${data.repository_count} repository(ies)...`,
        cls: "running",
      });
      startPolling(data.job_id);
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
      setBusy(false);
    }
  }

  function toggleRepo(name, checked) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(name);
      else next.delete(name);
      return next;
    });
  }

  // Combined repository list: merges workspace repos with any Neo4j stats so they are ALWAYS visible
  const combinedRepoList = useMemo(() => {
    const map = new Map();

    // 1. Add all workspace discovered repos
    for (const r of repos) {
      map.set(r.name, {
        name: r.name,
        activity_score: r.activity_score ?? null,
        files: r.files ?? 0,
        commits: r.commits_90d ?? r.commits ?? 0,
        functions: 0,
        classes: 0,
        branch: r.branch || "main",
        inWorkspace: true,
        inGraph: false,
      });
    }

    // 2. Overlay Neo4j stats if graph is already built
    if (analytics?.repositories) {
      for (const gr of analytics.repositories) {
        const existing = map.get(gr.name) || {
          name: gr.name,
          activity_score: gr.activity_score,
          inWorkspace: false,
          branch: "main",
        };
        map.set(gr.name, {
          ...existing,
          activity_score: gr.activity_score ?? existing.activity_score,
          files: gr.files ?? existing.files,
          commits: gr.commits ?? existing.commits,
          functions: gr.functions ?? 0,
          classes: gr.classes ?? 0,
          inGraph: true,
        });
      }
    }

    return Array.from(map.values());
  }, [repos, analytics]);

  const allSelected = combinedRepoList.length > 0 && selected.size === combinedRepoList.length;

  if (loading) return <p className="meta">Loading Neo4j graph status…</p>;

  const connected = analytics?.connected;

  return (
    <div className="neo4j-tab">
      {/* ── Build / Update controls ─────────────────────────────── */}
      <section className="card" style={{ position: "relative", overflow: "hidden" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
          <div>
            <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
              <span>Build &amp; Update Graph DB</span>
              {connected && <span className="github-badge-status" style={{ fontSize: "11px" }}>Connected</span>}
            </h3>
            <p className="meta" style={{ marginTop: 4, marginBottom: 12 }}>
              Constructs an AST code knowledge graph in Neo4j ({analytics?.uri || "AuraDB"}) from your active
              workspace repositories (Repo, Commits, AST Classes, Functions, and Call Graphs).
            </p>
          </div>
        </div>

        {/* ── Top Target Repositories Selector ─────────────────────── */}
        <div
          style={{
            marginTop: 8,
            marginBottom: 14,
            padding: "12px 14px",
            background: "rgba(0,0,0,0.02)",
            border: "1px solid var(--border-subtle, rgba(0,0,0,0.08))",
            borderRadius: "8px",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, flexWrap: "wrap", gap: 8 }}>
            <span style={{ fontSize: "12.5px", fontWeight: 650 }}>
              Select Repositories to Build ({selected.size} of {combinedRepoList.length} selected):
            </span>
            <label style={{ fontSize: "12px", cursor: "pointer", fontWeight: 600, display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="checkbox"
                checked={allSelected}
                onChange={(e) =>
                  setSelected(e.target.checked ? new Set(combinedRepoList.map((r) => r.name)) : new Set())
                }
                disabled={busy}
              />
              Select All ({combinedRepoList.length})
            </label>
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {combinedRepoList.length === 0 ? (
              <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                No repositories synced. Connect a repository from Settings → Repositories.
              </span>
            ) : (
              combinedRepoList.map((r) => {
                const isChecked = selected.has(r.name);
                return (
                  <div
                    key={r.name}
                    onClick={() => !busy && toggleRepo(r.name, !isChecked)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "6px 12px",
                      borderRadius: "6px",
                      border: isChecked ? "1px solid var(--accent, #2563eb)" : "1px solid var(--border, #cbd5e1)",
                      background: isChecked ? "rgba(37,99,235,0.06)" : "#fff",
                      cursor: busy ? "default" : "pointer",
                      userSelect: "none",
                      fontSize: "12.5px",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={isChecked}
                      disabled={busy}
                      onChange={(e) => {
                        e.stopPropagation();
                        toggleRepo(r.name, e.target.checked);
                      }}
                    />
                    <span style={{ fontWeight: 600 }}>{r.name}</span>
                    <span className="github-badge-branch" style={{ fontSize: "10.5px" }}>{r.branch || "main"}</span>
                    {r.inGraph ? (
                      <span className="github-badge-status" style={{ fontSize: "10px", padding: "1px 5px" }}>In Graph</span>
                    ) : (
                      <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>Pending</span>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>

        <div className="repo-actions" style={{ alignItems: "center", flexWrap: "wrap", gap: 14, paddingTop: 2 }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 550, fontSize: "13px" }}>
            Mode:
            <select value={mode} onChange={(e) => setMode(e.target.value)} disabled={busy} className="field-input" style={{ width: "auto", padding: "4px 10px" }}>
              {BUILD_MODES.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </label>

          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "13px", cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={includeCode}
              onChange={(e) => setIncludeCode(e.target.checked)}
              disabled={busy}
            />
            <span>Include code structure (tree-sitter)</span>
          </label>

          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "13px", cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={pullLatest}
              onChange={(e) => setPullLatest(e.target.checked)}
              disabled={busy}
            />
            <span>Pull latest code first (git pull)</span>
          </label>

          {/* Button is strictly blocked if 0 repos are selected */}
          <button
            className="primary"
            onClick={build}
            disabled={busy || selected.size === 0}
            style={{
              padding: "8px 22px",
              fontWeight: 650,
              opacity: selected.size === 0 && !busy ? 0.6 : 1,
              cursor: selected.size === 0 && !busy ? "not-allowed" : "pointer",
            }}
            title={selected.size === 0 ? "Select at least one repository above to build" : ""}
          >
            {busy ? "Building Knowledge Graph..." : "Build / Update Graph DB"}
          </button>
        </div>

        {selected.size === 0 && !busy && (
          <div style={{ marginTop: 10, fontSize: "12px", color: "var(--err, #ef4444)", fontWeight: 550 }}>
            Please select at least one repository from the list above to enable building.
          </div>
        )}

        {/* ── Modern Dynamic Build State (Clean phrases & Animation without emojis) ── */}
        {busy && (
          <div
            style={{
              marginTop: 18,
              padding: "16px 20px",
              background: "linear-gradient(135deg, rgba(37,99,235,0.08) 0%, rgba(147,51,234,0.08) 100%)",
              border: "1px solid rgba(59,130,246,0.3)",
              borderRadius: "10px",
              position: "relative",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
              <div>
                <div style={{ fontWeight: 650, fontSize: "14px", color: "var(--accent-strong, #2563eb)" }}>
                  {CATCHY_BUILD_PHRASES[phraseIdx]}
                </div>
                <div style={{ fontSize: "12px", color: "var(--text-muted, #64748b)", marginTop: 2 }}>
                  Status: <strong style={{ textTransform: "capitalize" }}>{job?.status || "Processing"}</strong> · {job?.progress?.repositories_done ?? 0} of {job?.totals?.repositories ?? selected.size} repos finished
                </div>
              </div>

              <button
                type="button"
                className="btn-text-action"
                style={{ fontSize: "11.5px" }}
                onClick={() => setShowRawLogs((p) => !p)}
              >
                {showRawLogs ? "Hide Logs" : "Show Raw Logs"}
              </button>
            </div>

            {/* Visual Progress Bar */}
            <div style={{ width: "100%", height: 6, background: "rgba(0,0,0,0.08)", borderRadius: 3, overflow: "hidden" }}>
              <div
                style={{
                  height: "100%",
                  width: `${Math.max(10, ((job?.progress?.repositories_done ?? 0) / Math.max(1, job?.totals?.repositories ?? selected.size)) * 100)}%`,
                  background: "linear-gradient(90deg, #3b82f6 0%, #8b5cf6 100%)",
                  borderRadius: 3,
                  transition: "width 0.4s ease",
                }}
              />
            </div>

            {showRawLogs && (
              <ul className="job-log" style={{ marginTop: 12, maxHeight: 120, overflowY: "auto", fontSize: "11.5px" }}>
                {(job?.logs || []).slice(-8).map((l, i) => (
                  <li key={i} className={`meta log-${l.level}`} style={{ margin: "2px 0" }}>
                    [{l.step}] {l.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>

      {/* ── Connection / Analytics ─────────────────────────────────── */}
      <section className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Graph Analytics</h3>
          <button
            className="secondary"
            onClick={handleRefreshAnalytics}
            disabled={busy || refreshing}
          >
            {refreshing ? "Refreshing..." : "Refresh analytics"}
          </button>
        </div>

        {!connected ? (
          <div className="status-bar error">
            Not connected to Neo4j{analytics?.error ? `: ${analytics.error}` : ""}.
          </div>
        ) : (
          <>
            {analytics.trends?.available && analytics.trends.baseline_at && (
              <p className="meta" style={{ marginTop: 0 }}>
                Trend shown vs the previous snapshot ({new Date(analytics.trends.baseline_at).toLocaleString()}).
              </p>
            )}

            <div className="stat-grid">
              <StatCard label="Total nodes" value={numberFmt(analytics.node_total)} delta={analytics.trends?.totals?.node_total} />
              <StatCard label="Total relationships" value={numberFmt(analytics.relationship_total)} delta={analytics.trends?.totals?.relationship_total} />
              <StatCard label="Repositories" value={numberFmt(analytics.repositories?.length || combinedRepoList.length)} delta={analytics.trends?.totals?.repository_count} />
              <StatCard
                label="Functions"
                value={numberFmt(analytics.nodes_by_label?.find((l) => l.label === "Function")?.count)}
                delta={analytics.trends?.totals?.functions}
              />
            </div>

            <div className="analytics-cols">
              <LabelTable title="Nodes by label" rows={analytics.nodes_by_label} keyName="label" deltas={analytics.trends?.by_label} />
              <LabelTable title="Relationships by type" rows={analytics.relationships_by_type} keyName="type" deltas={analytics.trends?.by_rel} />
              <LabelTable title="Top languages (by file)" rows={analytics.languages} keyName="ext" valueName="files" deltas={analytics.trends?.by_language} />
            </div>

            <div className="analytics-cols">
              <ListTable
                title="Most-imported modules"
                rows={analytics.top_modules}
                cols={[["name", "Module"], ["imported_by", "Files"]]}
              />
              <ListTable
                title="Most-called functions"
                rows={analytics.top_called_functions}
                cols={[["name", "Function"], ["repo", "Repo"], ["calls", "Calls"]]}
              />
            </div>

            {/* ── Per-repository breakdown table (Always lists workspace repos) ── */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 24, marginBottom: 8 }}>
              <h4 style={{ margin: 0 }}>Workspace Repositories &amp; Graph Coverage</h4>
            </div>

            <table>
              <thead>
                <tr>
                  <th style={{ width: "5%" }}>Select</th>
                  <th>Repository</th>
                  <th>Branch</th>
                  <th>Activity</th>
                  <th>Files</th>
                  <th>Commits</th>
                  <th>Functions</th>
                  <th>Classes</th>
                  <th>Graph Status</th>
                </tr>
              </thead>
              <tbody>
                {combinedRepoList.length === 0 ? (
                  <tr>
                    <td colSpan={9} style={{ textAlign: "center", padding: "20px", color: "var(--text-muted)" }}>
                      No repositories connected yet. Go to <strong>Settings → Repositories</strong> to connect from GitHub.
                    </td>
                  </tr>
                ) : (
                  combinedRepoList.map((r) => {
                    const tier = activityTier(r.activity_score);
                    const isChecked = selected.has(r.name);
                    return (
                      <tr key={r.name} style={{ background: isChecked ? "rgba(37,99,235,0.03)" : "transparent" }}>
                        <td>
                          <input
                            type="checkbox"
                            checked={isChecked}
                            disabled={busy}
                            onChange={(e) => toggleRepo(r.name, e.target.checked)}
                          />
                        </td>
                        <td style={{ fontWeight: 600 }}>{r.name}</td>
                        <td><span className="github-badge-branch">{r.branch || "main"}</span></td>
                        <td>
                          <span className={`badge ${tier.cls}`}>
                            {r.activity_score == null ? "Ready" : r.activity_score}
                          </span>{" "}
                          <span className="activity-sub">{tier.label}</span>
                        </td>
                        <td>{numberFmt(r.files)}</td>
                        <td>{numberFmt(r.commits)}</td>
                        <td>{numberFmt(r.functions)}</td>
                        <td>{numberFmt(r.classes)}</td>
                        <td>
                          {r.inGraph ? (
                            <span className="github-badge-status" style={{ fontSize: "11px" }}>In Graph</span>
                          ) : (
                            <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>Pending Build</span>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </>
        )}
      </section>
    </div>
  );
}

// Up caret + N (green) / down caret + N (red) / nothing for 0 or unknown.
function Trend({ delta }) {
  if (delta == null || delta === 0) return null;
  const up = delta > 0;
  return (
    <span className={`trend ${up ? "up" : "down"}`} title={`${up ? "+" : ""}${delta} vs previous snapshot`}>
      {up ? <CaretUp size={11} weight="fill" /> : <CaretDown size={11} weight="fill" />}
      {numberFmt(Math.abs(delta))}
    </span>
  );
}

function StatCard({ label, value, delta }) {
  return (
    <div className="stat-card">
      <div className="stat-value">
        {value} <Trend delta={delta} />
      </div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function LabelTable({ title, rows, keyName, valueName = "count", deltas }) {
  return (
    <div className="analytics-block">
      <h4>{title}</h4>
      <table>
        <tbody>
          {(rows || []).map((r) => (
            <tr key={r[keyName]}>
              <td>{r[keyName]}</td>
              <td style={{ textAlign: "right" }}>{numberFmt(r[valueName])}</td>
              <td style={{ textAlign: "right", width: 64 }}>
                <Trend delta={deltas?.[r[keyName]]} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ListTable({ title, rows, cols }) {
  return (
    <div className="analytics-block">
      <h4>{title}</h4>
      <table>
        <thead>
          <tr>{cols.map(([k, label]) => <th key={k}>{label}</th>)}</tr>
        </thead>
        <tbody>
          {(rows || []).map((r, i) => (
            <tr key={i}>
              {cols.map(([k]) => (
                <td key={k} style={{ textAlign: typeof r[k] === "number" ? "right" : "left" }}>
                  {typeof r[k] === "number" ? numberFmt(r[k]) : r[k]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
