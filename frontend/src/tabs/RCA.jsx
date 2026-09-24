// RCA tab — enter a Jira Ticket ID, run a read-only Root Cause Analysis, and
// view the evidence-first diagnosis (classification, High/Med/Low confidence,
// facts/inferences/unknowns, evidence, agent trace) plus a downloadable .docx.
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiDownload } from "../api.js";
import { fmtDuration } from "../lib/format";

const TERMINAL = new Set(["delivered", "low_confidence", "failed"]);

const STATUS_LABEL = {
  queued: "Queued",
  investigating: "Investigating codebase…",
  synthesizing: "Synthesizing diagnosis…",
  delivered: "Diagnosis ready",
  low_confidence: "Low confidence — review",
  failed: "Failed",
};

export default function RCA() {
  const [ticketId, setTicketId] = useState("");
  const [selectedRepo, setSelectedRepo] = useState("");
  const [repoList, setRepoList] = useState([]);
  const [run, setRun] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pollRef = useRef(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  useEffect(() => {
    apiFetch("/graph-admin/neo4j/active-repositories")
      .then((data) => setRepoList(data.repositories || []))
      .catch(() => {});
    return stopPolling;
  }, []);

  const poll = useCallback((runId) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const data = await apiFetch(`/rca/runs/${runId}`);
        setRun(data);
        if (TERMINAL.has(data.status)) {
          stopPolling();
          setBusy(false);
        }
      } catch (e) {
        setError(e.message);
        stopPolling();
        setBusy(false);
      }
    }, 2500);
  }, []);

  const start = async () => {
    const key = ticketId.trim().toUpperCase();
    if (!/^[A-Z][A-Z0-9]+-\d+$/.test(key)) {
      setError("Enter a valid Jira key, e.g. OPS-428");
      return;
    }
    setError("");
    setBusy(true);
    setRun(null);
    try {
      const query = selectedRepo ? `?repo=${encodeURIComponent(selectedRepo)}` : "";
      const res = await apiFetch(`/rca/${key}${query}`, { method: "POST" });
      setRun({ run_id: res.run_id, jira_key: key, status: res.status });
      poll(res.run_id);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  const downloadDocx = async () => {
    if (!run?.run_id) return;
    try {
      await apiDownload(`/rca/runs/${run.run_id}/document.docx`, {
        fallbackName: `RCA-${run.jira_key}.docx`,
      });
    } catch (e) {
      setError(e.message);
    }
  };

  const status = run?.status;
  const diagnosis = run?.diagnosis;
  const confidenceLabel = diagnosis?.confidence_label;
  const classification = diagnosis?.issue_classification;

  return (
    <div className="rca-tab">
      <h2>Root Cause Analysis</h2>
      <p className="muted">
        Enter a Jira defect ID. The system investigates the codebase read-only and
        produces a diagnosis pinpointing the most likely root cause with evidence.
        It does not generate or apply fixes.
      </p>

      {/* ── Step-by-Step RCA Workflow Guide ── */}
      <div
        style={{
          background: "var(--card, #ffffff)",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "8px",
          padding: "14px 16px",
          margin: "12px 0 16px",
          fontSize: "13px",
        }}
      >
        <div style={{ fontWeight: 600, color: "var(--text, #1e293b)", marginBottom: "8px" }}>
          How to run an RCA Investigation:
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "12px" }}>
          <div style={{ padding: "10px", background: "rgba(37,99,235,0.04)", borderRadius: "6px", border: "1px solid rgba(37,99,235,0.1)" }}>
            <strong style={{ color: "var(--primary, #2563eb)" }}>Step 1: Build Code Index</strong>
            <p style={{ margin: "4px 0 0", color: "var(--text-muted, #64748b)", fontSize: "12px", lineHeight: 1.4 }}>
              Click <strong>Build now</strong> below to index repository functions into vector chunks so the AI agent can discover relevant code.
            </p>
          </div>
          <div style={{ padding: "10px", background: "rgba(37,99,235,0.04)", borderRadius: "6px", border: "1px solid rgba(37,99,235,0.1)" }}>
            <strong style={{ color: "var(--primary, #2563eb)" }}>Step 2: Select Defect &amp; Target Repo</strong>
            <p style={{ margin: "4px 0 0", color: "var(--text-muted, #64748b)", fontSize: "12px", lineHeight: 1.4 }}>
              Enter the Jira Ticket ID (e.g. <code>SCRUM-9</code>) and select a repository (or keep auto-detect).
            </p>
          </div>
          <div style={{ padding: "10px", background: "rgba(37,99,235,0.04)", borderRadius: "6px", border: "1px solid rgba(37,99,235,0.1)" }}>
            <strong style={{ color: "var(--primary, #2563eb)" }}>Step 3: Run RCA &amp; Export</strong>
            <p style={{ margin: "4px 0 0", color: "var(--text-muted, #64748b)", fontSize: "12px", lineHeight: 1.4 }}>
              Click <strong>Run RCA</strong> to generate the root-cause diagnosis, inspect the agent trace, and download the official <strong>.docx</strong> report.
            </p>
          </div>
        </div>
      </div>

      <CodeIndexPanel />

      <div className="rca-controls" style={{ display: "flex", gap: 10, margin: "14px 0", flexWrap: "wrap", alignItems: "center" }}>
        <input
          value={ticketId}
          onChange={(e) => setTicketId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && start()}
          placeholder="Ticket ID (e.g. SCRUM-9)"
          disabled={busy}
          style={{ flex: "0 0 220px" }}
        />
        <select
          value={selectedRepo}
          onChange={(e) => setSelectedRepo(e.target.value)}
          disabled={busy}
          style={{ flex: "0 0 240px", padding: "8px 12px", fontSize: "13px" }}
        >
          <option value="">All active repositories (auto-detect)</option>
          {repoList.map((r) => (
            <option key={r.name} value={r.name}>
              {r.name}
            </option>
          ))}
        </select>
        <button onClick={start} disabled={busy}>
          {busy ? "Running…" : "Run RCA"}
        </button>
        {status === "delivered" || status === "low_confidence" ? (
          <button onClick={downloadDocx} className="secondary">
            Download .docx
          </button>
        ) : null}
      </div>

      <CodeIndexPanel />

      {error ? <div className="error-banner">{error}</div> : null}

      {run ? (
        <div className="rca-status" style={{ margin: "8px 0" }}>
          <strong>{STATUS_LABEL[status] || status}</strong>
          {classification ? <span> · {classification}</span> : null}
          {confidenceLabel ? <span> · confidence {confidenceLabel}</span> : null}
          {status === "low_confidence" ? (
            <span className="muted"> · routed to human review (low confidence)</span>
          ) : null}
          {run.error ? <div className="error-banner">{run.error}</div> : null}
        </div>
      ) : null}

      {!TERMINAL.has(status) && run ? (
        <LiveTrace trace={run.agent_trace} />
      ) : null}

      {diagnosis ? <Diagnosis markdown={run.markdown} run={run} /> : null}
    </div>
  );
}

// Status + one-click build for the code_chunks semantic index. Building it once
// enables the semantic retriever (the pipeline still works without it, just with
// fewer signals). Indexing is incremental thereafter.
function CodeIndexPanel() {
  const [status, setStatus] = useState(null);
  const [live, setLive] = useState(null); // { points, updating, progress } from embeddings status
  const [building, setBuilding] = useState(false);
  const [msg, setMsg] = useState("");
  const [scope, setScope] = useState("active"); // "active" | "all"
  const [exclude, setExclude] = useState(""); // comma-separated repo names to skip
  const excludeTouched = useRef(false);
  const timerRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      const st = await apiFetch("/rca/code-index/status");
      setStatus(st);
      // Prefill the skip list from the env default (RCA_INDEX_EXCLUDED_REPOS)
      // until the user edits the field.
      if (!excludeTouched.current && Array.isArray(st.excluded_default)) {
        setExclude(st.excluded_default.join(", "));
      }
    } catch {
      /* ignore */
    }
    // Pull the RCA code-chunks entry from the shared embeddings status so we can
    // surface live build progress + ETA (and keep polling while it's building).
    try {
      const emb = await apiFetch("/graph-admin/embeddings/status");
      const row = (emb.collections || []).find((c) => c.kind === "rca");
      setLive(row || null);
      return row;
    } catch {
      return null;
    }
  }, []);

  // Poll every 3s while the index is building, else just once.
  useEffect(() => {
    let active = true;
    async function tick() {
      const row = await refresh();
      if (!active) return;
      timerRef.current = setTimeout(tick, row && row.updating ? 3000 : 30000);
    }
    tick();
    return () => {
      active = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [refresh]);

  const build = async () => {
    setBuilding(true);
    setMsg("");
    try {
      const exc = exclude.split(",").map((s) => s.trim()).filter(Boolean);
      const res = await apiFetch("/rca/code-index/build", {
        method: "POST",
        body: { scope, exclude: exc },
      });
      const n = res.repository_count;
      const skipped = res.excluded?.length ? ` (skipping ${res.excluded.join(", ")})` : "";
      setMsg(
        `Indexing ${n != null ? `${n} ` : ""}${scope} repo${n === 1 ? "" : "s"}${skipped} in the ` +
          "background — unchanged repos are skipped, so this is fast after the first run.",
      );
      setTimeout(refresh, 2000);
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBuilding(false);
    }
  };

  const updating = live?.updating;
  const p = live?.progress;
  const points = live?.points ?? status?.points;
  const ready = status?.exists;

  const badge = updating
    ? { cls: "run", text: "Building" }
    : ready
      ? { cls: "ok", text: "Ready" }
      : { cls: "warn", text: "Not built" };

  return (
    <div className="ci-card">
      <div className="ci-head">
        <div className="ci-title-wrap">
          <span className="ci-title">Semantic code index</span>
          <span className={`badge ${badge.cls}`}>{badge.text}</span>
          {!updating && points != null ? (
            <span className="ci-sub">{Number(points).toLocaleString()} chunks</span>
          ) : null}
        </div>
        <div className="ci-controls">
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            disabled={building || updating}
            title="Which repositories to index"
          >
            <option value="active">Active repos</option>
            <option value="all">All repos</option>
          </select>
          <input
            type="text"
            value={exclude}
            onChange={(e) => {
              excludeTouched.current = true;
              setExclude(e.target.value);
            }}
            placeholder="skip repos (e.g. AWS)"
            disabled={building || updating}
            title="Comma-separated repo names to skip (large repos like AWS)"
          />
          <button className="ci-btn" onClick={build} disabled={building || updating}>
            {building ? "Starting…" : updating ? "Building…" : ready ? "Rebuild" : "Build now"}
          </button>
        </div>
      </div>

      {updating && p ? (
        <div className="ci-progress">
          <div className="ci-bar">
            <div className="ci-bar-fill" style={{ width: `${p.percent ?? 0}%` }} />
          </div>
          <div className="ci-progress-meta">
            <span className="ci-progress-repo">
              {p.detail || (p.total ? `${p.done}/${p.total} ${p.unit}` : "indexing…")}
            </span>
            <span>
              {p.eta_seconds
                ? `~${fmtDuration(p.eta_seconds)} left${p.eta_scope === "repo" ? " (repo)" : ""} · `
                : ""}
              {points != null ? `${Number(points).toLocaleString()} chunks total` : ""}
            </span>
          </div>
        </div>
      ) : null}

      {!updating && !ready ? (
        <div className="ci-note">Semantic retriever disabled until the index is built.</div>
      ) : null}
      {msg ? <div className="ci-note">{msg}</div> : null}
    </div>
  );
}

function LiveTrace({ trace }) {
  if (!trace || trace.length === 0) {
    return <p className="muted">Gathering evidence…</p>;
  }
  return (
    <div className="rca-trace">
      <h4>Investigation ({trace.length} steps)</h4>
      <ul>
        {trace.slice(-8).map((t, i) => (
          <li key={i}>
            <code>{t.tool}</code>{" "}
            <span className="muted">{summarizeInput(t.input)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function summarizeInput(input) {
  if (!input) return "";
  return Object.entries(input)
    .map(([k, v]) => `${k}=${String(v).slice(0, 40)}`)
    .join(", ");
}

function Diagnosis({ markdown, run }) {
  const [showTrace, setShowTrace] = useState(false);
  return (
    <div className="rca-result">
      <pre className="rca-markdown" style={{ whiteSpace: "pre-wrap" }}>
        {markdown || "Diagnosis produced; open the .docx for the full report."}
      </pre>
      <button className="link" onClick={() => setShowTrace((s) => !s)}>
        {showTrace ? "Hide" : "Show"} agent trace ({run.agent_trace?.length || 0} steps)
      </button>
      {showTrace ? (
        <pre className="rca-trace-json" style={{ maxHeight: 300, overflow: "auto" }}>
          {JSON.stringify(run.agent_trace, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}
