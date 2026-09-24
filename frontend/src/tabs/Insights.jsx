import { useEffect, useState } from "react";
import { apiFetch, apiDownload } from "../api";
import { fmtDate } from "../lib/format";
import {
  ArrowsClockwise,
  FileXls,
  SpinnerGap,
  Check,
  CaretUp,
  CaretDown,
  MagnifyingGlass,
} from "@phosphor-icons/react";

const MATCH_TYPES = [
  { value: "all", label: "All matches" },
  { value: "test_cases", label: "Test Cases" },
  { value: "requirements", label: "PRD / BRD / SRS" },
  { value: "prd", label: "PRD" },
  { value: "brd", label: "BRD" },
  { value: "srs", label: "SRS" },
];
const PIPELINE_LIMITS = [
  { value: "0", label: "No pipeline run" },
  { value: "3", label: "3 pipeline tickets" },
  { value: "5", label: "5 pipeline tickets" },
  { value: "10", label: "10 pipeline tickets" },
  { value: "20", label: "20 pipeline tickets" },
];

export default function Insights() {
  const [project, setProject] = useState("");
  const [matchType, setMatchType] = useState("all");
  const [pipelineLimit, setPipelineLimit] = useState("5");
  const [rows, setRows] = useState([]);
  const [counts, setCounts] = useState({});
  const [countText, setCountText] = useState("Loading…");
  const [info, setInfo] = useState({ msg: "", error: false });
  const [downloading, setDownloading] = useState(false);

  async function load(nextMatchType = matchType) {
    setCountText("Loading…");
    setInfo({ msg: "", error: false });
    const params = new URLSearchParams({ limit: "500", match_type: nextMatchType });
    const proj = project.trim().toUpperCase();
    if (proj) params.set("project_key", proj);
    try {
      const data = await apiFetch(`/graph-admin/jira-ticket-insights?${params.toString()}`);
      if (data.error) {
        setInfo({ msg: data.error, error: true });
        setCountText("—");
        setRows([]);
        setCounts({});
        return;
      }
      const total = data.total_tickets || 0;
      const matching = data.matching_tickets || 0;
      const returned = data.returned_tickets || 0;
      const excluded = (data.excluded_projects || []).join(", ");
      setCountText(
        `${matching} matching ticket${matching !== 1 ? "s" : ""} from ${total} scanned${excluded ? ` · excluded ${excluded}` : ""}`,
      );
      setCounts(data.counts || {});
      setRows(data.tickets || []);
      if (returned < matching) {
        setInfo({ msg: `Showing latest ${returned} of ${matching} matching tickets`, error: false });
      }
    } catch (err) {
      setInfo({ msg: err.message, error: true });
      setCountText("—");
      setCounts({});
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function downloadReport() {
    setDownloading(true);
    setInfo({ msg: "Generating pipeline comparison...", error: false });
    const params = new URLSearchParams({ limit: "500", format: "xlsx" });
    const proj = project.trim().toUpperCase();
    if (proj) params.set("project_key", proj);
    params.set("pipeline_limit", pipelineLimit || "5");
    try {
      await apiDownload(`/graph-admin/test-case-comparison-report?${params.toString()}`, {
        fallbackName: "jira-test-case-comparison.xlsx",
      });
      setInfo({ msg: "Pipeline comparison downloaded", error: false });
    } catch (err) {
      setInfo({ msg: err.message, error: true });
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div>
      {/* 1. Stat Cards Grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
          gap: 12,
          marginBottom: 16,
        }}
      >
        <StatCard title="Test Cases" value={counts.test_cases || 0} subtext="Detected test suites" color="#0284c7" />
        <StatCard title="PRD Documents" value={counts.prd || 0} subtext="Product requirements" color="#4f46e5" />
        <StatCard title="BRD Documents" value={counts.brd || 0} subtext="Business requirements" color="#059669" />
        <StatCard title="SRS Documents" value={counts.srs || 0} subtext="System requirements" color="#d97706" />
        <StatCard title="Matching Tickets" value={counts.matching_tickets || 0} subtext="Total with artifacts" color="#7c3aed" />
      </div>

      {/* 2. Controls Toolbar */}
      <div
        style={{
          background: "#ffffff",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: 8,
          padding: "12px 14px",
          marginBottom: 16,
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "space-between",
          alignItems: "center",
          gap: 12,
          boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
        }}
      >
        {/* Left: Scan Summary */}
        <div style={{ fontSize: 13, color: "var(--muted, #64748b)", fontWeight: 500 }}>
          {countText}
        </div>

        {/* Right: Filters, Limits & Actions */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
          <input
            type="text"
            placeholder="Project (e.g. TRAIL)"
            value={project}
            onChange={(e) => setProject(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load()}
            style={{
              width: 140,
              height: 34,
              padding: "0 10px",
              fontSize: 12.5,
              background: "var(--surface, #f7f9fd)",
              border: "1px solid var(--line-strong, #d3dae8)",
              borderRadius: 6,
              color: "var(--ink, #0f172a)",
              margin: 0,
            }}
          />

          <select
            value={matchType}
            onChange={(e) => {
              setMatchType(e.target.value);
              load(e.target.value);
            }}
            style={{
              height: 34,
              minWidth: 150,
              padding: "0 28px 0 10px",
              fontSize: 12.5,
              background: "var(--surface, #f7f9fd)",
              border: "1px solid var(--line-strong, #d3dae8)",
              borderRadius: 6,
              color: "var(--ink, #0f172a)",
              margin: 0,
            }}
          >
            {MATCH_TYPES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>

          <button
            type="button"
            className="secondary"
            onClick={() => load()}
            style={{
              width: "auto",
              minHeight: "unset",
              height: 34,
              padding: "0 12px",
              fontSize: 12.5,
              fontWeight: 600,
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              margin: 0,
            }}
          >
            <ArrowsClockwise size={15} weight="bold" />
            Refresh
          </button>

          <select
            value={pipelineLimit}
            onChange={(e) => setPipelineLimit(e.target.value)}
            style={{
              height: 34,
              minWidth: 140,
              padding: "0 28px 0 10px",
              fontSize: 12.5,
              background: "var(--surface, #f7f9fd)",
              border: "1px solid var(--line-strong, #d3dae8)",
              borderRadius: 6,
              color: "var(--ink, #0f172a)",
              margin: 0,
            }}
          >
            {PIPELINE_LIMITS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>

          <button
            type="button"
            className="secondary"
            disabled={downloading}
            onClick={downloadReport}
            style={{
              width: "auto",
              minHeight: "unset",
              height: 34,
              padding: "0 14px",
              fontSize: 12.5,
              fontWeight: 650,
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              margin: 0,
              background: "#ffffff",
              border: "1px solid var(--line-strong, #cbd5e1)",
            }}
          >
            {downloading ? <SpinnerGap size={15} className="animate-spin" /> : <FileXls size={15} weight="bold" />}
            <span>{downloading ? "Exporting..." : "Excel Report"}</span>
          </button>
        </div>
      </div>

      {/* 3. Main Tickets Table */}
      <table style={{ width: "100%", tableLayout: "fixed" }}>
        <thead>
          <tr>
            <th style={{ width: "11%" }}>Key</th>
            <th style={{ width: "8%" }}>Project</th>
            <th style={{ width: "35%" }}>Summary & Status</th>
            <th style={{ width: "9%", textAlign: "center" }}>Test Cases</th>
            <th style={{ width: "11%" }}>Docs</th>
            <th style={{ width: "16%" }}>Evidence Snippet</th>
            <th style={{ width: "10%" }}>Updated</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={7} style={{ textAlign: "center", padding: "32px 14px", color: "var(--muted)" }}>
                No matching tickets found
              </td>
            </tr>
          ) : (
            rows.map((t) => <InsightRow key={t.ticket_key} t={t} />)
          )}
        </tbody>
      </table>

      {info.msg && (
        <div style={{ color: info.error ? "var(--danger)" : "var(--muted)", fontSize: 13, marginTop: 8 }}>
          {info.msg}
        </div>
      )}
    </div>
  );
}

function StatCard({ title, value, subtext, color = "var(--ink, #0f172a)" }) {
  return (
    <div
      style={{
        background: "#ffffff",
        border: "1px solid var(--line, #e2e8f0)",
        borderRadius: 8,
        padding: "12px 16px",
        boxShadow: "0 1px 2px rgba(0,0,0,0.04)",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
      }}
    >
      <div style={{ fontSize: 11, fontWeight: "700", textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--muted, #64748b)" }}>
        {title}
      </div>
      <div style={{ fontSize: 22, fontWeight: "800", color, marginTop: 4 }}>
        {value}
      </div>
      <div style={{ fontSize: 11.5, color: "var(--muted, #64748b)", marginTop: 2 }}>
        {subtext}
      </div>
    </div>
  );
}

function InsightRow({ t }) {
  const docs = t.requirement_docs || [];
  const matches = t.matches || [];
  const [open, setOpen] = useState(false);

  const statusLower = (t.status || "").toLowerCase();
  let statusBadgeClass = "badge";
  if (statusLower.includes("done") || statusLower.includes("closed") || statusLower.includes("resolved")) {
    statusBadgeClass = "badge ok";
  } else if (statusLower.includes("progress") || statusLower.includes("review") || statusLower.includes("active")) {
    statusBadgeClass = "badge run";
  } else if (statusLower.includes("block") || statusLower.includes("cancel")) {
    statusBadgeClass = "badge err";
  }

  return (
    <tr style={{ verticalAlign: "middle" }}>
      {/* Key */}
      <td style={{ padding: "12px 14px" }}>
        {t.url ? (
          <a
            href={t.url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: "var(--accent-strong, #4338ca)", textDecoration: "none", fontFamily: "ui-monospace, monospace", fontSize: 12.5, fontWeight: 700 }}
          >
            {t.ticket_key}
          </a>
        ) : (
          <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12.5, fontWeight: 700, color: "var(--accent-strong, #4338ca)" }}>
            {t.ticket_key}
          </span>
        )}
      </td>

      {/* Project */}
      <td style={{ padding: "12px 10px", color: "var(--muted)", fontWeight: 600, fontSize: 12.5 }}>
        {t.project_key || "—"}
      </td>

      {/* Summary + Status Badge + Type */}
      <td style={{ padding: "12px 14px" }}>
        <div style={{ fontWeight: 600, fontSize: 13.5, color: "var(--ink, #0f172a)", lineHeight: 1.4, wordBreak: "break-word" }}>
          {t.summary || "—"}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 5 }}>
          <span className={statusBadgeClass} style={{ fontSize: 10.5, padding: "1px 7px", fontWeight: 700 }}>
            {t.status || "—"}
          </span>
          <span style={{ fontSize: 11.5, color: "var(--muted)", fontWeight: 500 }}>
            • {t.issue_type || "Task"}
          </span>
        </div>
      </td>

      {/* Test Cases Found */}
      <td style={{ padding: "12px 10px", textAlign: "center" }}>
        {t.has_test_cases ? (
          <span className="badge ok" style={{ fontSize: 11, padding: "2px 8px", display: "inline-flex", alignItems: "center", gap: 4 }}>
            <Check size={12} weight="bold" />
            Found
          </span>
        ) : (
          <span style={{ color: "var(--muted)", fontSize: 13 }}>—</span>
        )}
      </td>

      {/* Docs (PRD / BRD / SRS) */}
      <td style={{ padding: "12px 10px" }}>
        {docs.length > 0 ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {docs.map((d, i) => (
              <span
                key={i}
                className="badge"
                style={{
                  fontSize: 10.5,
                  padding: "1px 7px",
                  background: "var(--accent-soft, #eef2ff)",
                  color: "var(--accent-strong, #4338ca)",
                  border: "1px solid rgba(67, 56, 202, 0.2)",
                  fontWeight: 700,
                }}
              >
                {d}
              </span>
            ))}
          </div>
        ) : (
          <span style={{ color: "var(--muted)", fontSize: 13 }}>—</span>
        )}
      </td>

      {/* Evidence Snippet */}
      <td style={{ padding: "12px 12px" }}>
        {matches.length === 0 ? (
          <span style={{ color: "var(--muted)", fontSize: 12 }}>—</span>
        ) : (
          <div>
            <button
              type="button"
              onClick={() => setOpen(!open)}
              style={{
                width: "auto",
                minHeight: "unset",
                height: 26,
                padding: "0 10px",
                fontSize: 11.5,
                fontWeight: 650,
                background: open ? "var(--accent-strong, #4338ca)" : "var(--surface-2, #f1f5f9)",
                color: open ? "#ffffff" : "var(--accent-strong, #4338ca)",
                border: "1px solid var(--line, #cbd5e1)",
                borderRadius: 5,
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                boxShadow: "none",
                transform: "none",
              }}
            >
              {open ? <CaretUp size={12} weight="bold" /> : <CaretDown size={12} weight="bold" />}
              <MagnifyingGlass size={13} weight="bold" />
              <span>{matches.length} hit{matches.length !== 1 ? "s" : ""}</span>
            </button>

            {open && (
              <div
                style={{
                  marginTop: 6,
                  display: "flex",
                  flexDirection: "column",
                  gap: 5,
                  maxWidth: 280,
                }}
              >
                {matches.slice(0, 3).map((m, i) => (
                  <div
                    key={i}
                    style={{
                      background: "var(--surface, #f8fafc)",
                      border: "1px solid var(--line, #e2e8f0)",
                      borderRadius: 6,
                      padding: "6px 8px",
                      fontSize: 11,
                      lineHeight: 1.4,
                      color: "var(--ink-soft, #334155)",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 2 }}>
                      <span className="badge" style={{ fontSize: 9.5, padding: "0 5px", background: "var(--surface-2)", color: "var(--accent-strong)" }}>
                        {m.label}
                      </span>
                      <span style={{ fontSize: 10, color: "var(--muted)", textTransform: "uppercase", fontWeight: 700 }}>
                        {m.source}
                      </span>
                    </div>
                    <div style={{ color: "var(--ink-soft)", fontStyle: "italic", wordBreak: "break-word" }}>
                      "{m.snippet}"
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </td>

      {/* Updated */}
      <td style={{ padding: "12px 14px", color: "var(--muted)", fontSize: 12 }}>
        {fmtDate(t.updated_at)}
      </td>
    </tr>
  );
}
