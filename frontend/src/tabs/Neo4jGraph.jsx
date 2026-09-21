import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";

// Build modes map to the backend `wipe_mode`.
const BUILD_MODES = [
  { value: "managed", label: "Update — replace code/git graph, keep Jira & embeddings" },
  { value: "all", label: "Full rebuild — wipe the ENTIRE database first" },
  { value: "none", label: "Merge — no wipe, upsert in place" },
];

function activityTier(score) {
  if (score == null) return { cls: "idle", label: "Unknown" };
  if (score >= 60) return { cls: "ok", label: "Active" };
  if (score >= 30) return { cls: "warn", label: "Moderate" };
  if (score >= 1) return { cls: "err", label: "Low" };
  return { cls: "idle", label: "Stale" };
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
  const pollRef = useRef(null);

  const loadAnalytics = useCallback(async () => {
    const data = await apiFetch("/graph-admin/neo4j/analytics");
    setAnalytics(data);
  }, []);

  const loadRepos = useCallback(async () => {
    const data = await apiFetch("/graph-admin/neo4j/active-repositories");
    const list = data.repositories || [];
    setRepos(list);
    setSelected(new Set(list.map((r) => r.name)));
  }, []);

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
            setStatus({ msg: "Neo4j graph build completed", cls: "ok" });
            loadAnalytics().catch(() => {});
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
      setStatus({ msg: "Select at least one repository", cls: "error" });
      return;
    }
    if (mode === "all" && !window.confirm(
      "Full rebuild will DETACH DELETE the entire Neo4j database (including Jira nodes " +
      "and embeddings) before writing. Continue?")) {
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
        msg: `Build started (${String(data.job_id).slice(0, 8)}…) for ${data.repository_count} repos`,
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
  const allSelected = repos.length > 0 && selected.size === repos.length;

  if (loading) return <p className="meta">Loading Neo4j graph status…</p>;

  const connected = analytics?.connected;

  return (
    <div className="neo4j-tab">
      {/* ── Build / Update controls ─────────────────────────────── */}
      <section className="card">
        <h3>Build &amp; Update Graph DB</h3>
        <p className="meta">
          Builds a code knowledge graph in Neo4j ({analytics?.uri || "configured server"}) from the
          active (non-stale) repositories: git history (Repo/Commit/Author/File) plus a
          tree-sitter code layer (Class/Function/Interface, CALLS/IMPORTS/INHERITS).
        </p>
        <div className="repo-actions" style={{ alignItems: "center", flexWrap: "wrap", gap: 12 }}>
          <label>
            Mode:{" "}
            <select value={mode} onChange={(e) => setMode(e.target.value)} disabled={busy}>
              {BUILD_MODES.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={includeCode}
              onChange={(e) => setIncludeCode(e.target.checked)}
              disabled={busy}
            />{" "}
            Include code structure (tree-sitter)
          </label>
          <label>
            <input
              type="checkbox"
              checked={pullLatest}
              onChange={(e) => setPullLatest(e.target.checked)}
              disabled={busy}
            />{" "}
            Pull latest code first (git pull)
          </label>
          <button className="primary" onClick={build} disabled={busy}>
            {busy ? "Building…" : "Build / Update Graph DB"}
          </button>
        </div>

        {job && (
          <div className="job-progress" style={{ marginTop: 12 }}>
            <div className="status-bar running">
              Status: {job.status} · {job.progress?.repositories_done ?? 0}/
              {job.totals?.repositories ?? 0} repositories
            </div>
            <ul className="job-log">
              {(job.logs || []).slice(-6).map((l, i) => (
                <li key={i} className={`meta log-${l.level}`}>
                  [{l.step}] {l.message}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {/* ── Connection / totals ─────────────────────────────────── */}
      <section className="card">
        <h3>Graph Analytics</h3>
        {!connected ? (
          <div className="status-bar error">
            Not connected to Neo4j{analytics?.error ? `: ${analytics.error}` : ""}.
          </div>
        ) : (
          <>
            {analytics.trends?.available && analytics.trends.baseline_at && (
              <p className="meta" style={{ marginTop: 0 }}>
                Trend ▲/▼ shown vs the previous snapshot ({new Date(analytics.trends.baseline_at).toLocaleString()}).
              </p>
            )}

            <div className="stat-grid">
              <StatCard label="Total nodes" value={numberFmt(analytics.node_total)}
                delta={analytics.trends?.totals?.node_total} />
              <StatCard label="Total relationships" value={numberFmt(analytics.relationship_total)}
                delta={analytics.trends?.totals?.relationship_total} />
              <StatCard label="Repositories" value={numberFmt(analytics.repositories?.length)}
                delta={analytics.trends?.totals?.repository_count} />
              <StatCard
                label="Functions"
                value={numberFmt(
                  analytics.nodes_by_label?.find((l) => l.label === "Function")?.count,
                )}
                delta={analytics.trends?.totals?.functions}
              />
            </div>

            <div className="analytics-cols">
              <LabelTable title="Nodes by label" rows={analytics.nodes_by_label} keyName="label"
                deltas={analytics.trends?.by_label} />
              <LabelTable title="Relationships by type" rows={analytics.relationships_by_type} keyName="type"
                deltas={analytics.trends?.by_rel} />
              <LabelTable title="Top languages (by file)" rows={analytics.languages} keyName="ext" valueName="files"
                deltas={analytics.trends?.by_language} />
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

            <h4>Per-repository breakdown</h4>
            <table>
              <thead>
                <tr>
                  <th style={{ width: "4%" }}>Use</th>
                  <th>Repository</th>
                  <th>Activity</th>
                  <th>Files</th>
                  <th>Commits</th>
                  <th>Functions</th>
                  <th>Classes</th>
                </tr>
              </thead>
              <tbody>
                {(analytics.repositories || []).map((r) => {
                  const tier = activityTier(r.activity_score);
                  const inActive = repos.some((x) => x.name === r.name);
                  return (
                    <tr key={r.name}>
                      <td>
                        {inActive ? (
                          <input
                            type="checkbox"
                            checked={selected.has(r.name)}
                            onChange={(e) => toggleRepo(r.name, e.target.checked)}
                          />
                        ) : (
                          <span className="meta" title="Not currently active">—</span>
                        )}
                      </td>
                      <td>{r.name}</td>
                      <td>
                        <span className={`badge ${tier.cls}`}>
                          {r.activity_score == null ? "—" : r.activity_score}
                        </span>{" "}
                        <span className="activity-sub">{tier.label}</span>
                      </td>
                      <td>{numberFmt(r.files)}</td>
                      <td>{numberFmt(r.commits)}</td>
                      <td>{numberFmt(r.functions)}</td>
                      <td>{numberFmt(r.classes)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}

        <div className="repo-actions" style={{ marginTop: 12 }}>
          <label>
            <input type="checkbox" checked={allSelected}
              onChange={(e) => setSelected(e.target.checked ? new Set(repos.map((r) => r.name)) : new Set())} />
            Select all active repositories ({repos.length})
          </label>
          <button className="secondary" onClick={() => loadAnalytics().catch(() => {})} disabled={busy}>
            Refresh analytics
          </button>
        </div>
      </section>
    </div>
  );
}

// ▲N (up, green) / ▼N (down, red) / nothing for 0 or unknown.
function Trend({ delta }) {
  if (delta == null || delta === 0) return null;
  const up = delta > 0;
  return (
    <span className={`trend ${up ? "up" : "down"}`} title={`${up ? "+" : ""}${delta} vs previous snapshot`}>
      {up ? "▲" : "▼"} {numberFmt(Math.abs(delta))}
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
