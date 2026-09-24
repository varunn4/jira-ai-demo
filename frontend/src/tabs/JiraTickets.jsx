import { useEffect, useMemo, useState } from "react";
import { apiFetch, apiDownload } from "../api";
import { fmtDate } from "../lib/format";
import {
  Target,
  Flask,
  FileText,
  Files,
  ClipboardText,
  ArrowsClockwise,
  FileXls,
  SpinnerGap,
  Check,
  X,
  CaretUp,
  CaretDown,
  MagnifyingGlass,
} from "@phosphor-icons/react";

const MATCH_FILTERS = [
  { value: "all", label: "All Tickets", icon: null },
  { value: "matches_only", label: "Any Requirement", icon: Target },
  { value: "test_cases", label: "Test Cases", icon: Flask },
  { value: "prd", label: "PRD", icon: FileText },
  { value: "brd", label: "BRD", icon: Files },
  { value: "srs", label: "SRS", icon: ClipboardText },
];

const PIPELINE_LIMITS = [
  { value: "0", label: "No pipeline run" },
  { value: "3", label: "3 pipeline tickets" },
  { value: "5", label: "5 pipeline tickets" },
  { value: "10", label: "10 pipeline tickets" },
  { value: "20", label: "20 pipeline tickets" },
];

export default function JiraTickets() {
  const [tickets, setTickets] = useState([]);
  const [counts, setCounts] = useState({});
  const [totalScanned, setTotalScanned] = useState(0);
  const [excludedText, setExcludedText] = useState("");
  const [loading, setLoading] = useState(true);
  const [filterType, setFilterType] = useState("all");
  const [search, setSearch] = useState("");
  const [projectKey, setProjectKey] = useState("");
  const [pipelineLimit, setPipelineLimit] = useState("5");
  const [downloading, setDownloading] = useState(false);
  const [info, setInfo] = useState({ msg: "", error: false });

  async function loadData() {
    setLoading(true);
    setInfo({ msg: "", error: false });
    const params = new URLSearchParams({ limit: "500", match_type: "all" });
    const proj = projectKey.trim().toUpperCase();
    if (proj) params.set("project_key", proj);

    try {
      const data = await apiFetch(`/graph-admin/jira-ticket-insights?${params.toString()}`);
      if (data.error) {
        setInfo({ msg: data.error, error: true });
        setTickets([]);
        setCounts({});
        return;
      }
      const excluded = (data.excluded_projects || []).join(", ");
      setExcludedText(excluded ? ` · excluded ${excluded}` : "");
      setTotalScanned(data.total_tickets || 0);
      setCounts(data.counts || {});
      setTickets(data.tickets || []);
    } catch (err) {
      setInfo({ msg: err.message, error: true });
      setTickets([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Filter and Search tickets
  const filteredTickets = useMemo(() => {
    let result = tickets;

    // 1. Filter by match category
    if (filterType === "test_cases") {
      result = result.filter((t) => t.has_test_cases);
    } else if (filterType === "matches_only") {
      result = result.filter((t) => t.has_test_cases || (t.requirement_docs && t.requirement_docs.length > 0));
    } else if (["prd", "brd", "srs"].includes(filterType)) {
      result = result.filter((t) => (t.requirement_docs || []).map((d) => d.toLowerCase()).includes(filterType));
    }

    // 2. Omni-search filter
    const q = search.trim().toLowerCase();
    if (q) {
      result = result.filter((t) => {
        const key = (t.ticket_key || "").toLowerCase();
        const proj = (t.project_key || "").toLowerCase();
        const summary = (t.summary || "").toLowerCase();
        const status = (t.status || "").toLowerCase();
        const type = (t.issue_type || "").toLowerCase();
        const docs = (t.requirement_docs || []).join(" ").toLowerCase();
        const evidence = (t.matches || []).map((m) => `${m.label} ${m.snippet}`).join(" ").toLowerCase();
        return (
          key.includes(q) ||
          proj.includes(q) ||
          summary.includes(q) ||
          status.includes(q) ||
          type.includes(q) ||
          docs.includes(q) ||
          evidence.includes(q)
        );
      });
    }

    return result;
  }, [tickets, filterType, search]);

  async function downloadExcelReport() {
    setDownloading(true);
    setInfo({ msg: "Generating test case comparison report...", error: false });
    const params = new URLSearchParams({ limit: "500", format: "xlsx" });
    const proj = projectKey.trim().toUpperCase();
    if (proj) params.set("project_key", proj);
    params.set("pipeline_limit", pipelineLimit || "5");

    try {
      await apiDownload(`/graph-admin/test-case-comparison-report?${params.toString()}`, {
        fallbackName: "jira-test-case-comparison.xlsx",
      });
      setInfo({ msg: "Excel comparison report downloaded successfully", error: false });
    } catch (err) {
      setInfo({ msg: `Download failed: ${err.message}`, error: true });
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="jira-tickets-insights-content">
      {/* 1. Top KPI Summary Cards */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
          gap: 12,
          marginBottom: 16,
        }}
      >
        <KpiCard
          label="Total Cached Tickets"
          value={totalScanned}
          subtext="Indexed in database"
          color="var(--ink, #0f172a)"
        />
        <KpiCard
          label="Test Case Specs"
          value={counts.test_cases || 0}
          subtext="With explicit QA IDs"
          color="var(--ok, #059669)"
        />
        <KpiCard
          label="PRD Requirements"
          value={counts.prd || 0}
          subtext="Product specs"
          color="var(--accent-strong, #4338ca)"
        />
        <KpiCard
          label="BRD Requirements"
          value={counts.brd || 0}
          subtext="Business specs"
          color="#d97706"
        />
        <KpiCard
          label="SRS Requirements"
          value={counts.srs || 0}
          subtext="System specs"
          color="#0284c7"
        />
        <KpiCard
          label="Matching Tickets"
          value={counts.matching_tickets || 0}
          subtext="With detected artifacts"
          color="#7c3aed"
        />
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
        {/* Left: Filter Pills */}
        <div style={{ display: "inline-flex", alignItems: "center", background: "var(--surface-2, #eef2f9)", padding: 3, borderRadius: 20, gap: 3 }}>
          {MATCH_FILTERS.map((f) => {
            const active = filterType === f.value;
            let countLabel = "";
            if (f.value === "all") countLabel = ` (${totalScanned})`;
            else if (f.value === "matches_only") countLabel = ` (${counts.matching_tickets || 0})`;
            else if (f.value === "test_cases") countLabel = ` (${counts.test_cases || 0})`;
            else if (f.value === "prd") countLabel = ` (${counts.prd || 0})`;
            else if (f.value === "brd") countLabel = ` (${counts.brd || 0})`;
            else if (f.value === "srs") countLabel = ` (${counts.srs || 0})`;

            return (
              <button
                key={f.value}
                type="button"
                onClick={() => setFilterType(f.value)}
                style={{
                  width: "auto",
                  minHeight: "unset",
                  padding: "4px 10px",
                  borderRadius: 14,
                  fontSize: 12,
                  fontWeight: "600",
                  border: "none",
                  cursor: "pointer",
                  background: active ? "#ffffff" : "transparent",
                  color: active ? "var(--ink, #0f172a)" : "var(--muted, #64748b)",
                  boxShadow: active ? "0 1px 2px rgba(0,0,0,0.08)" : "none",
                  transform: "none",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 5,
                }}
              >
                {f.icon && <f.icon size={14} weight={active ? "fill" : "regular"} />}
                {f.label}{countLabel}
              </button>
            );
          })}
        </div>

        {/* Right: Search, Project Filter & Excel Export */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
          {/* Omni Search */}
          <div style={{ position: "relative", width: 260 }}>
            <input
              type="text"
              placeholder="Search key, summary, evidence…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{
                width: "100%",
                padding: "6px 24px 6px 10px",
                fontSize: 12.5,
                background: "var(--surface, #f7f9fd)",
                border: "1px solid var(--line-strong, #d3dae8)",
                borderRadius: 6,
                color: "var(--ink, #0f172a)",
                height: 34,
                margin: 0,
              }}
            />
            {search && (
              <button
                type="button"
                onClick={() => setSearch("")}
                style={{
                  position: "absolute",
                  right: 6,
                  top: 7,
                  width: "auto",
                  minHeight: "unset",
                  padding: 2,
                  background: "transparent",
                  border: "none",
                  color: "var(--muted, #64748b)",
                  cursor: "pointer",
                  fontSize: 11,
                  boxShadow: "none",
                }}
              >
                <X size={12} weight="bold" />
              </button>
            )}
          </div>

          {/* Project Key Input */}
          <input
            type="text"
            placeholder="Project (e.g. TRAIL)"
            value={projectKey}
            onChange={(e) => setProjectKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && loadData()}
            style={{
              width: 140,
              padding: "6px 10px",
              fontSize: 12.5,
              background: "var(--surface, #f7f9fd)",
              border: "1px solid var(--line-strong, #d3dae8)",
              borderRadius: 6,
              color: "var(--ink, #0f172a)",
              height: 34,
              margin: 0,
            }}
          />

          <button
            type="button"
            className="secondary"
            onClick={loadData}
            style={{
              width: "auto",
              minHeight: "unset",
              height: 34,
              padding: "0 12px",
              fontSize: 12.5,
              fontWeight: "600",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <ArrowsClockwise size={15} weight="bold" />
            Refresh
          </button>

          {/* Pipeline Limit Dropdown */}
          <select
            value={pipelineLimit}
            onChange={(e) => setPipelineLimit(e.target.value)}
            style={{
              background: "var(--surface, #f7f9fd)",
              border: "1px solid var(--line-strong, #d3dae8)",
              borderRadius: 6,
              color: "var(--ink, #0f172a)",
              height: 34,
              fontSize: 12,
              padding: "0 8px",
              fontWeight: "500",
              cursor: "pointer",
            }}
          >
            {PIPELINE_LIMITS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>

          {/* Download Excel */}
          <button
            type="button"
            className="secondary"
            disabled={downloading}
            onClick={downloadExcelReport}
            style={{
              width: "auto",
              minHeight: "unset",
              height: 34,
              padding: "0 12px",
              fontSize: 12.5,
              fontWeight: "600",
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              background: downloading ? "var(--accent-soft)" : "#ffffff",
              color: "var(--accent-strong, #4338ca)",
              cursor: downloading ? "not-allowed" : "pointer",
            }}
            title="Download test case and requirement comparative analysis as Excel (.xlsx)"
          >
            {downloading ? (
              <>
                <SpinnerGap size={15} className="animate-spin" />
                Exporting...
              </>
            ) : (
              <>
                <FileXls size={15} weight="bold" />
                Excel Report
              </>
            )}
          </button>
        </div>
      </div>

      {/* Info / Error Banner */}
      {info.msg && (
        <div
          style={{
            padding: "8px 14px",
            borderRadius: 6,
            fontSize: 13,
            marginBottom: 12,
            background: info.error ? "var(--danger-soft, #fef2f2)" : "var(--surface-2, #eef2f9)",
            color: info.error ? "var(--danger, #dc2626)" : "var(--ink, #0f172a)",
            border: `1px solid ${info.error ? "rgba(220, 38, 38, 0.2)" : "var(--line, #e2e8f0)"}`,
          }}
        >
          {info.msg}
        </div>
      )}

      {/* 3. Unified Tickets & Insights Table */}
      <div
        style={{
          background: "#ffffff",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "var(--r, 10px)",
          boxShadow: "var(--shadow-xs)",
          overflow: "hidden",
        }}
      >
        <table style={{ margin: 0, width: "100%", borderCollapse: "separate", borderSpacing: 0, tableLayout: "fixed" }}>
          <thead>
            <tr>
              <th style={{ width: "11%", padding: "12px 14px" }}>Key</th>
              <th style={{ width: "8%", padding: "12px 10px" }}>Project</th>
              <th style={{ width: "35%", padding: "12px 14px" }}>Summary & Status</th>
              <th style={{ width: "9%", padding: "12px 10px", textAlign: "center" }}>Test Cases</th>
              <th style={{ width: "11%", padding: "12px 10px" }}>Docs</th>
              <th style={{ width: "16%", padding: "12px 12px" }}>Evidence Snippet</th>
              <th style={{ width: "10%", padding: "12px 14px", textAlign: "right" }}>Updated</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7} style={{ textAlign: "center", padding: "36px", color: "var(--muted)" }}>
                  Loading Jira tickets & requirement intelligence…
                </td>
              </tr>
            ) : filteredTickets.length === 0 ? (
              <tr>
                <td colSpan={7} style={{ textAlign: "center", padding: "36px", color: "var(--muted)" }}>
                  No tickets match the selected filter criteria.
                </td>
              </tr>
            ) : (
              filteredTickets.map((t) => <CombinedTicketRow key={t.ticket_key} t={t} />)
            )}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 12, fontSize: 12, color: "var(--muted)" }}>
        Showing {filteredTickets.length} of {totalScanned} tickets in cache{excludedText}
      </div>
    </div>
  );
}

function KpiCard({ label, value, subtext, color }) {
  return (
    <div
      style={{
        background: "#ffffff",
        border: "1px solid var(--line, #e2e8f0)",
        borderRadius: "var(--r, 10px)",
        padding: "12px 16px",
        boxShadow: "var(--shadow-xs)",
      }}
    >
      <div style={{ fontSize: 11, fontWeight: "700", color: "var(--muted, #64748b)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
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

function CombinedTicketRow({ t }) {
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
        {t.project_key}
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
      <td style={{ padding: "12px 14px", fontSize: 12, color: "var(--muted)", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
        {fmtDate(t.updated_at)}
      </td>
    </tr>
  );
}
