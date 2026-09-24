// RCA tab — enter a Jira Ticket ID, run a read-only Root Cause Analysis, and
// view the evidence-first diagnosis (classification, High/Med/Low confidence,
// facts/inferences/unknowns, evidence, agent trace).
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api.js";
import { fmtDuration } from "../lib/format";
import {
  MagnifyingGlass,
  Play,
  SpinnerGap,
  WarningCircle,
  Database,
  CaretDown,
  CaretUp,
  GitBranch,
  CheckCircle,
  Question,
  Lightbulb,
  ListChecks,
  Code,
  Article,
  Info,
} from "@phosphor-icons/react";

const TERMINAL = new Set(["delivered", "low_confidence", "failed"]);

const STATUS_LABEL = {
  queued: "Queued",
  investigating: "Investigating codebase…",
  synthesizing: "Synthesizing diagnosis…",
  delivered: "Diagnosis Ready",
  low_confidence: "Low Confidence (Human Review Recommended)",
  failed: "Investigation Failed",
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
      setError("Please enter a valid Jira defect key, e.g. SCRUM-9 or OPS-428");
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

  const status = run?.status;
  const diagnosis = run?.diagnosis || run?.document;
  const confidenceLabel = diagnosis?.confidence_label;
  const classification = diagnosis?.issue_classification;

  return (
    <div className="rca-tab" style={{ maxWidth: "1200px", margin: "0 auto" }}>
      {/* ── Header ── */}
      <div style={{ marginBottom: "18px" }}>
        <h2 style={{ fontSize: "20px", fontWeight: "750", margin: "0 0 6px", color: "var(--ink, #0f172a)" }}>
          Root Cause Analysis (RCA)
        </h2>
        <p style={{ margin: 0, color: "var(--muted, #64748b)", fontSize: "13.5px", lineHeight: 1.5 }}>
          Investigate Jira defects autonomously across your indexed repositories. The AI agent inspects code read-only
          and produces structured, evidence-backed diagnoses.
        </p>
      </div>

      {/* ── Workflow Guide ── */}
      <div
        style={{
          background: "var(--card, #ffffff)",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "10px",
          padding: "16px 20px",
          marginBottom: "18px",
          boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
        }}
      >
        <div style={{ fontSize: "12px", fontWeight: "700", textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--muted, #64748b)", marginBottom: "12px" }}>
          RCA Investigation Workflow
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: "14px" }}>
          <div style={{ padding: "12px 14px", background: "rgba(37,99,235,0.03)", borderRadius: "8px", border: "1px solid rgba(37,99,235,0.1)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "6px", fontWeight: "700", fontSize: "13px", color: "var(--accent-strong, #2563eb)" }}>
              <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "20px", height: "20px", borderRadius: "50%", background: "#2563eb", color: "#fff", fontSize: "11px" }}>1</span>
              Build Code Index
            </div>
            <p style={{ margin: "6px 0 0", color: "var(--muted, #64748b)", fontSize: "12px", lineHeight: 1.45 }}>
              Click <strong>Build Index</strong> to index AST code chunks into vector embeddings for semantic discovery.
            </p>
          </div>
          <div style={{ padding: "12px 14px", background: "rgba(37,99,235,0.03)", borderRadius: "8px", border: "1px solid rgba(37,99,235,0.1)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "6px", fontWeight: "700", fontSize: "13px", color: "var(--accent-strong, #2563eb)" }}>
              <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "20px", height: "20px", borderRadius: "50%", background: "#2563eb", color: "#fff", fontSize: "11px" }}>2</span>
              Select Defect &amp; Target Repo
            </div>
            <p style={{ margin: "6px 0 0", color: "var(--muted, #64748b)", fontSize: "12px", lineHeight: 1.45 }}>
              Enter the Jira Ticket Key (e.g. <code>SCRUM-9</code>) and select the target repository (or keep auto-detect).
            </p>
          </div>
          <div style={{ padding: "12px 14px", background: "rgba(37,99,235,0.03)", borderRadius: "8px", border: "1px solid rgba(37,99,235,0.1)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "6px", fontWeight: "700", fontSize: "13px", color: "var(--accent-strong, #2563eb)" }}>
              <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "20px", height: "20px", borderRadius: "50%", background: "#2563eb", color: "#fff", fontSize: "11px" }}>3</span>
              Run RCA Diagnosis
            </div>
            <p style={{ margin: "6px 0 0", color: "var(--muted, #64748b)", fontSize: "12px", lineHeight: 1.45 }}>
              Click <strong>Run RCA</strong> to inspect agent reasoning steps in real time and inspect the structured root-cause findings.
            </p>
          </div>
        </div>
      </div>

      {/* ── Semantic Code Index Panel (Single Instance) ── */}
      <CodeIndexPanel />

      {/* ── RCA Investigation Controls Card ── */}
      <div
        style={{
          background: "var(--card, #ffffff)",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "10px",
          padding: "18px 20px",
          marginBottom: "20px",
          boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
        }}
      >
        <div style={{ fontSize: "14px", fontWeight: "700", color: "var(--ink, #0f172a)", marginBottom: "12px" }}>
          Launch RCA Investigation
        </div>

        <div style={{ display: "flex", gap: "14px", flexWrap: "wrap", alignItems: "flex-end" }}>
          <div style={{ flex: "1 1 240px", minWidth: "200px" }}>
            <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "6px" }}>
              Jira Defect Key
            </label>
            <div style={{ position: "relative" }}>
              <input
                value={ticketId}
                onChange={(e) => setTicketId(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !busy && start()}
                placeholder="e.g. SCRUM-9 or OPS-428"
                disabled={busy}
                style={{
                  width: "100%",
                  height: "40px",
                  padding: "0 12px 0 36px",
                  fontSize: "13.5px",
                  borderRadius: "6px",
                  border: "1px solid var(--line-strong, #cbd5e1)",
                  background: "var(--surface, #ffffff)",
                  boxSizing: "border-box",
                }}
              />
              <MagnifyingGlass
                size={16}
                style={{ position: "absolute", left: "12px", top: "12px", color: "var(--muted, #94a3b8)", pointerEvents: "none" }}
              />
            </div>
          </div>

          <div style={{ flex: "1 1 280px", minWidth: "220px" }}>
            <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "6px" }}>
              Target Repository
            </label>
            <div style={{ position: "relative" }}>
              <select
                value={selectedRepo}
                onChange={(e) => setSelectedRepo(e.target.value)}
                disabled={busy}
                style={{
                  width: "100%",
                  height: "40px",
                  padding: "0 12px 0 34px",
                  fontSize: "13.5px",
                  borderRadius: "6px",
                  border: "1px solid var(--line-strong, #cbd5e1)",
                  background: "var(--surface, #ffffff)",
                  boxSizing: "border-box",
                  cursor: "pointer",
                }}
              >
                <option value="">All active repositories (auto-detect)</option>
                {repoList.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.name}
                  </option>
                ))}
              </select>
              <GitBranch
                size={16}
                style={{ position: "absolute", left: "12px", top: "12px", color: "var(--muted, #94a3b8)", pointerEvents: "none" }}
              />
            </div>
          </div>

          <div style={{ display: "flex", gap: "10px", alignItems: "center", flex: "0 0 auto" }}>
            <button
              onClick={start}
              disabled={busy}
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                gap: "8px",
                height: "40px",
                padding: "0 24px",
                fontSize: "13.5px",
                fontWeight: "600",
                borderRadius: "6px",
                background: "var(--accent-grad-strong, #2563eb)",
                color: "#ffffff",
                border: "none",
                cursor: busy ? "not-allowed" : "pointer",
                boxShadow: "0 1px 3px rgba(37,99,235,0.25)",
                width: "auto",
                minWidth: "140px",
              }}
            >
              {busy ? (
                <>
                  <SpinnerGap size={17} className="animate-spin" />
                  <span>Investigating…</span>
                </>
              ) : (
                <>
                  <Play size={15} weight="fill" />
                  <span>Run RCA</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* ── Error Banner ── */}
      {error && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "10px",
            padding: "12px 16px",
            borderRadius: "8px",
            background: "rgba(239,68,68,0.08)",
            border: "1px solid rgba(239,68,68,0.25)",
            color: "#b91c1c",
            fontSize: "13.5px",
            marginBottom: "18px",
          }}
        >
          <WarningCircle size={18} weight="fill" style={{ flexShrink: 0 }} />
          <span>{error}</span>
        </div>
      )}

      {/* ── Investigation Status Header ── */}
      {run && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: "10px",
            padding: "12px 18px",
            background: "var(--card, #ffffff)",
            border: "1px solid var(--line, #e2e8f0)",
            borderRadius: "8px",
            marginBottom: "16px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
            <span
              className={`badge ${
                status === "delivered"
                  ? "ok"
                  : status === "low_confidence"
                  ? "warn"
                  : status === "failed"
                  ? "err"
                  : "run"
              }`}
              style={{ fontSize: "12px", padding: "4px 10px" }}
            >
              {STATUS_LABEL[status] || status}
            </span>
            <span style={{ fontSize: "13.5px", fontWeight: "600", color: "var(--ink, #0f172a)" }}>
              Ticket: {run.jira_key}
            </span>
            {classification && (
              <span style={{ fontSize: "13px", color: "var(--muted, #64748b)" }}>
                · Classification: <strong>{classification}</strong>
              </span>
            )}
            {confidenceLabel && (
              <span style={{ fontSize: "13px", color: "var(--muted, #64748b)" }}>
                · Confidence: <strong>{confidenceLabel}</strong>
              </span>
            )}
          </div>
        </div>
      )}

      {/* ── Live Investigation Trace ── */}
      {!TERMINAL.has(status) && run && <LiveTrace trace={run.agent_trace} />}

      {/* ── Structured Diagnosis Output ── */}
      {diagnosis && <StructuredDiagnosis run={run} />}
    </div>
  );
}

// Status + one-click build for the code_chunks semantic index.
function CodeIndexPanel() {
  const [status, setStatus] = useState(null);
  const [live, setLive] = useState(null);
  const [building, setBuilding] = useState(false);
  const [msg, setMsg] = useState("");
  const [scope, setScope] = useState("all"); // Default to "all" repos
  const [exclude, setExclude] = useState("");
  const excludeTouched = useRef(false);
  const timerRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      const st = await apiFetch("/rca/code-index/status");
      setStatus(st);
      if (!excludeTouched.current && Array.isArray(st.excluded_default)) {
        setExclude(st.excluded_default.join(", "));
      }
    } catch {
      /* ignore */
    }
    try {
      const emb = await apiFetch("/graph-admin/embeddings/status");
      const row = (emb.collections || []).find((c) => c.kind === "rca");
      setLive(row || null);
      return row;
    } catch {
      return null;
    }
  }, []);

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
          "background. Unchanged files are skipped automatically.",
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
  const ready = status?.exists && (points > 0 || status?.points > 0);

  const badge = updating
    ? { cls: "run", text: "Building" }
    : ready
      ? { cls: "ok", text: "Ready" }
      : { cls: "warn", text: "Not built" };

  return (
    <div className="ci-card">
      <div className="ci-head">
        <div className="ci-title-wrap">
          <Database size={17} style={{ color: "#2563eb" }} />
          <span className="ci-title">Semantic Code Index</span>
          <span className={`badge ${badge.cls}`}>{badge.text}</span>
          {!updating && points != null ? (
            <span className="ci-sub">{Number(points).toLocaleString()} indexed chunks</span>
          ) : null}
        </div>
        <div className="ci-controls">
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            disabled={building || updating}
            title="Which repositories to index"
          >
            <option value="all">All repos</option>
            <option value="active">Active repos</option>
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
            title="Comma-separated repo names to skip"
          />
          <button className="ci-btn" onClick={build} disabled={building || updating}>
            {building ? "Starting…" : updating ? "Building…" : ready ? "Rebuild Index" : "Build Index"}
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
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: "8px",
            marginTop: "12px",
            padding: "10px 14px",
            background: "rgba(37,99,235,0.04)",
            border: "1px solid rgba(37,99,235,0.12)",
            borderRadius: "6px",
            fontSize: "12.5px",
            lineHeight: "1.5",
            color: "var(--muted, #64748b)",
          }}
        >
          <Info size={16} style={{ color: "#2563eb", flexShrink: 0, marginTop: "2px" }} />
          <div>
            <strong style={{ color: "var(--ink-soft, #334155)" }}>Note:</strong> The <em>Not built (0 chunks)</em> status indicates vector embeddings have not been populated in Qdrant. The RCA agent operates autonomously without requiring vector embeddings — it performs deep AST analysis, file pattern matching, git history/blame traversal, and Jira context retrieval regardless.
          </div>
        </div>
      ) : null}
      {msg ? <div className="ci-note">{msg}</div> : null}
    </div>
  );
}

function LiveTrace({ trace }) {
  if (!trace || trace.length === 0) {
    return (
      <div style={{ padding: "16px", background: "var(--card, #fff)", border: "1px solid var(--line, #e2e8f0)", borderRadius: "8px", marginBottom: "16px" }}>
        <p style={{ margin: 0, color: "var(--muted, #64748b)", fontSize: "13px" }}>Gathering codebase evidence and constructing AST graph…</p>
      </div>
    );
  }
  return (
    <div
      style={{
        background: "var(--card, #ffffff)",
        border: "1px solid var(--line, #e2e8f0)",
        borderRadius: "8px",
        padding: "16px 20px",
        marginBottom: "18px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "14px", fontWeight: "700", color: "var(--ink, #0f172a)", marginBottom: "12px" }}>
        <SpinnerGap size={16} className="animate-spin" style={{ color: "#2563eb" }} />
        <span>Live Investigation Trace ({trace.length} steps executed)</span>
      </div>
      <ul style={{ margin: 0, paddingLeft: "18px", fontSize: "13px", lineHeight: "1.6" }}>
        {trace.slice(-8).map((t, i) => (
          <li key={i} style={{ marginBottom: "4px" }}>
            <code style={{ background: "rgba(37,99,235,0.08)", color: "#2563eb", padding: "2px 6px", borderRadius: "4px", fontWeight: "600" }}>{t.tool}</code>{" "}
            <span style={{ color: "var(--muted, #64748b)", fontSize: "12px" }}>{summarizeInput(t.input)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function summarizeInput(input) {
  if (!input) return "";
  return Object.entries(input)
    .map(([k, v]) => `${k}=${String(v).slice(0, 50)}`)
    .join(", ");
}

// ── Beautiful Structured Diagnosis Component ──
function StructuredDiagnosis({ run }) {
  const [viewRaw, setViewRaw] = useState(false);
  const [showTrace, setShowTrace] = useState(false);

  const doc = run?.document || run?.diagnosis || {};
  const rootCause = doc.root_cause || "Undetermined";
  const status = doc.root_cause_status || "undetermined";
  const isMostLikely = status === "most_likely" || status === "undetermined";
  const facts = doc.facts || [];
  const inferences = doc.inferences || [];
  const unknowns = doc.unknowns || [];
  const nextActions = doc.verification_steps || [];
  const extraEvidence = doc.additional_evidence_required || [];
  const evidenceList = doc.evidence || [];
  const location = doc.root_cause_location;
  const classification = doc.issue_classification || "Cannot Determine";
  const confidence = doc.confidence_label || "Low";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
      {/* ── Top Summary Card ── */}
      <div
        style={{
          background: "var(--card, #ffffff)",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "10px",
          padding: "22px 24px",
          boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "12px", marginBottom: "16px" }}>
          <div>
            <div style={{ fontSize: "11px", fontWeight: "700", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--muted, #64748b)", marginBottom: "4px" }}>
              {isMostLikely ? "Most Likely Root Cause" : "Confirmed Root Cause"}
            </div>
            <h3 style={{ fontSize: "18px", fontWeight: "750", margin: 0, color: "var(--ink, #0f172a)" }}>
              {doc.title || `Diagnosis for ${run.jira_key}`}
            </h3>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span
              style={{
                fontSize: "12px",
                fontWeight: "700",
                padding: "4px 10px",
                borderRadius: "6px",
                background: "rgba(37,99,235,0.08)",
                color: "#2563eb",
                border: "1px solid rgba(37,99,235,0.2)",
              }}
            >
              {classification}
            </span>
            <span
              style={{
                fontSize: "12px",
                fontWeight: "700",
                padding: "4px 10px",
                borderRadius: "6px",
                background:
                  confidence === "High"
                    ? "rgba(16,185,129,0.1)"
                    : confidence === "Medium"
                    ? "rgba(245,158,11,0.1)"
                    : "rgba(100,116,139,0.1)",
                color:
                  confidence === "High"
                    ? "#059669"
                    : confidence === "Medium"
                    ? "#d97706"
                    : "#64748b",
                border: "1px solid currentColor",
              }}
            >
              Confidence: {confidence}
            </span>
          </div>
        </div>

        {/* Highlighted Cause Box */}
        <div
          style={{
            background: isMostLikely ? "rgba(245,158,11,0.04)" : "rgba(16,185,129,0.04)",
            border: `1px solid ${isMostLikely ? "rgba(245,158,11,0.2)" : "rgba(16,185,129,0.2)"}`,
            borderRadius: "8px",
            padding: "16px 20px",
            fontSize: "14px",
            lineHeight: "1.6",
            color: "var(--ink, #0f172a)",
          }}
        >
          <div style={{ fontWeight: "700", fontSize: "14.5px", marginBottom: "4px", color: isMostLikely ? "#b45309" : "#047857" }}>
            {rootCause}
          </div>
          {location && location !== "Not localized" && (
            <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "12.5px", color: "var(--muted, #64748b)", marginTop: "8px" }}>
              <Code size={15} />
              <span>Location: <code>{location}</code></span>
            </div>
          )}
        </div>

        {/* Additional Evidence Required if not fully confirmed */}
        {extraEvidence.length > 0 && (
          <div style={{ marginTop: "14px", padding: "12px 16px", background: "rgba(100,116,139,0.05)", borderRadius: "6px", border: "1px solid var(--line, #e2e8f0)" }}>
            <div style={{ fontSize: "12px", fontWeight: "700", color: "var(--ink-soft, #334155)", marginBottom: "6px" }}>
              Additional Evidence Required to Confirm:
            </div>
            <ul style={{ margin: 0, paddingLeft: "18px", fontSize: "12.5px", color: "var(--muted, #64748b)", lineHeight: "1.5" }}>
              {extraEvidence.map((ev, i) => (
                <li key={i}>{ev}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* ── Next Actions & Verification Checklist ── */}
      {nextActions.length > 0 && (
        <div
          style={{
            background: "var(--card, #ffffff)",
            border: "1px solid var(--line, #e2e8f0)",
            borderRadius: "10px",
            padding: "18px 22px",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "14px", fontWeight: "700", color: "var(--ink, #0f172a)", marginBottom: "12px" }}>
            <ListChecks size={18} style={{ color: "#2563eb" }} />
            <span>Recommended Next Actions &amp; Verification Steps</span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            {nextActions.map((step, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: "10px",
                  padding: "10px 14px",
                  background: "var(--surface-2, #f8fafc)",
                  borderRadius: "6px",
                  border: "1px solid var(--line, #e2e8f0)",
                  fontSize: "13px",
                  lineHeight: "1.5",
                  color: "var(--ink, #0f172a)",
                }}
              >
                <CheckCircle size={17} weight="fill" style={{ color: "#2563eb", flexShrink: 0, marginTop: "2px" }} />
                <span>{step}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Facts, Inferences & Unknowns Grid ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "16px" }}>
        {/* Facts */}
        <div
          style={{
            background: "var(--card, #ffffff)",
            border: "1px solid var(--line, #e2e8f0)",
            borderRadius: "10px",
            padding: "18px 20px",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13.5px", fontWeight: "700", color: "#047857", marginBottom: "12px" }}>
            <CheckCircle size={17} weight="bold" />
            <span>Verified Facts ({facts.length})</span>
          </div>
          {facts.length === 0 ? (
            <div style={{ fontSize: "12.5px", color: "var(--muted, #64748b)" }}>No direct facts recorded.</div>
          ) : (
            <ul style={{ margin: 0, paddingLeft: "16px", fontSize: "12.5px", lineHeight: "1.6", color: "var(--ink-soft, #334155)" }}>
              {facts.map((f, i) => (
                <li key={i} style={{ marginBottom: "4px" }}>{f}</li>
              ))}
            </ul>
          )}
        </div>

        {/* Inferences */}
        <div
          style={{
            background: "var(--card, #ffffff)",
            border: "1px solid var(--line, #e2e8f0)",
            borderRadius: "10px",
            padding: "18px 20px",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13.5px", fontWeight: "700", color: "#2563eb", marginBottom: "12px" }}>
            <Lightbulb size={17} weight="bold" />
            <span>AI Inferences ({inferences.length})</span>
          </div>
          {inferences.length === 0 ? (
            <div style={{ fontSize: "12.5px", color: "var(--muted, #64748b)" }}>No inferences generated.</div>
          ) : (
            <ul style={{ margin: 0, paddingLeft: "16px", fontSize: "12.5px", lineHeight: "1.6", color: "var(--ink-soft, #334155)" }}>
              {inferences.map((inf, i) => (
                <li key={i} style={{ marginBottom: "4px" }}>{inf}</li>
              ))}
            </ul>
          )}
        </div>

        {/* Unknowns */}
        <div
          style={{
            background: "var(--card, #ffffff)",
            border: "1px solid var(--line, #e2e8f0)",
            borderRadius: "10px",
            padding: "18px 20px",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13.5px", fontWeight: "700", color: "#d97706", marginBottom: "12px" }}>
            <Question size={17} weight="bold" />
            <span>Unknowns &amp; Edge Cases ({unknowns.length})</span>
          </div>
          {unknowns.length === 0 ? (
            <div style={{ fontSize: "12.5px", color: "var(--muted, #64748b)" }}>No pending unknowns.</div>
          ) : (
            <ul style={{ margin: 0, paddingLeft: "16px", fontSize: "12.5px", lineHeight: "1.6", color: "var(--ink-soft, #334155)" }}>
              {unknowns.map((u, i) => (
                <li key={i} style={{ marginBottom: "4px" }}>{u}</li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* ── Evidence Sources ── */}
      {evidenceList.length > 0 && (
        <div
          style={{
            background: "var(--card, #ffffff)",
            border: "1px solid var(--line, #e2e8f0)",
            borderRadius: "10px",
            padding: "18px 22px",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div style={{ fontSize: "14px", fontWeight: "700", color: "var(--ink, #0f172a)", marginBottom: "12px" }}>
            Evidence Artifacts ({evidenceList.length})
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "10px" }}>
            {evidenceList.map((ev, i) => (
              <div
                key={i}
                style={{
                  padding: "10px 12px",
                  background: "var(--surface-2, #f8fafc)",
                  border: "1px solid var(--line, #e2e8f0)",
                  borderRadius: "6px",
                  fontSize: "12.5px",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "6px", fontWeight: "700", color: "var(--ink, #0f172a)", marginBottom: "4px" }}>
                  <span style={{ textTransform: "uppercase", fontSize: "10.5px", padding: "2px 6px", background: "rgba(37,99,235,0.08)", color: "#2563eb", borderRadius: "4px" }}>
                    {ev.category || "Evidence"}
                  </span>
                  <span>{ev.source || "Source"}</span>
                </div>
                <div style={{ color: "var(--muted, #64748b)", lineHeight: "1.4" }}>
                  {ev.summary || String(ev)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Collapsible Raw Markdown & Full Agent Trace ── */}
      <div style={{ display: "flex", gap: "16px", alignItems: "center", flexWrap: "wrap" }}>
        <button
          onClick={() => setViewRaw((v) => !v)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            background: "none",
            border: "none",
            color: "var(--accent-strong, #2563eb)",
            fontWeight: "600",
            fontSize: "13px",
            cursor: "pointer",
            padding: 0,
            width: "auto",
          }}
        >
          <Article size={15} />
          <span>{viewRaw ? "Hide" : "View"} Raw Markdown Report</span>
        </button>

        <button
          onClick={() => setShowTrace((s) => !s)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            background: "none",
            border: "none",
            color: "var(--accent-strong, #2563eb)",
            fontWeight: "600",
            fontSize: "13px",
            cursor: "pointer",
            padding: 0,
            width: "auto",
          }}
        >
          {showTrace ? <CaretUp size={14} /> : <CaretDown size={14} />}
          <span>{showTrace ? "Hide" : "View"} Full Agent Trace JSON ({run.agent_trace?.length || 0} steps)</span>
        </button>
      </div>

      {viewRaw && (
        <div
          style={{
            background: "var(--surface-2, #f8fafc)",
            border: "1px solid var(--line, #e2e8f0)",
            borderRadius: "8px",
            padding: "16px 20px",
            fontSize: "13px",
            lineHeight: "1.6",
            color: "var(--ink, #0f172a)",
          }}
        >
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontFamily: "monospace" }}>
            {run.markdown || JSON.stringify(doc, null, 2)}
          </pre>
        </div>
      )}

      {showTrace && (
        <pre
          style={{
            marginTop: "6px",
            padding: "14px",
            background: "#0f172a",
            color: "#f8fafc",
            borderRadius: "8px",
            fontSize: "12px",
            maxHeight: "350px",
            overflow: "auto",
          }}
        >
          {JSON.stringify(run.agent_trace, null, 2)}
        </pre>
      )}
    </div>
  );
}
