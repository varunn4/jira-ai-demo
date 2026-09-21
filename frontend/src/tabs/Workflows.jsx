import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { fmtDate } from "../lib/format";

// Maps an n8n execution status to the shared badge class.
function statusClass(status) {
  if (!status) return "";
  if (status === "success") return "ok";
  if (status === "error" || status === "crashed") return "err";
  return "run";
}

export default function Workflows() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load() {
    setError("");
    setLoading(true);
    try {
      const res = await apiFetch("/graph-admin/n8n/workflows");
      setData(res);
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

  const totals = data?.totals || {};
  const workflows = data?.workflows || [];

  return (
    <div>
      <SprintFilter />
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "var(--muted)" }}>
          Live n8n workflow status &amp; execution health
          {data?.execution_window
            ? ` (counts from last ${data.execution_window} executions)`
            : ""}
        </span>
        <button
          className="secondary"
          style={{ width: "auto", minHeight: "unset", padding: "6px 14px", fontSize: 13 }}
          onClick={load}
        >
          Refresh
        </button>
      </div>

      {data && !data.configured && (
        <div style={{ color: "var(--muted)", fontSize: 14, padding: "24px 0" }}>
          n8n monitoring is not configured. Set <code>N8N_BASE_URL</code> and{" "}
          <code>N8N_API_KEY</code> in the server environment to enable it.
        </div>
      )}

      {data?.configured && (
        <>
          <div className="stats-grid">
            <Stat value={totals.workflows ?? 0} label="Workflows" />
            <Stat value={totals.active ?? 0} label="Active / Live" />
            <Stat value={totals.inactive ?? 0} label="Inactive" />
            <Stat value={totals.executions ?? 0} label="Executions" />
            <Stat value={totals.errors ?? 0} label="Errors" />
          </div>

          <table>
            <thead>
              <tr>
                <th style={{ width: "26%" }}>Workflow</th>
                <th style={{ width: "9%" }}>Status</th>
                <th style={{ width: "9%" }}>Executions</th>
                <th style={{ width: "8%" }}>Success</th>
                <th style={{ width: "8%" }}>Errors</th>
                <th style={{ width: "12%" }}>Last Run</th>
                <th style={{ width: "10%" }}>Last Result</th>
                <th>Tags</th>
              </tr>
            </thead>
            <tbody>
              {workflows.length === 0 ? (
                <tr>
                  <td colSpan={8} style={{ color: "var(--muted)" }}>
                    No workflows found
                  </td>
                </tr>
              ) : (
                workflows.map((w) => (
                  <tr key={w.id}>
                    <td>{w.name}</td>
                    <td>
                      <span className={`badge ${w.active ? "ok" : ""}`}>
                        {w.active ? "Active" : "Inactive"}
                      </span>
                    </td>
                    <td>{w.executions}</td>
                    <td>{w.success}</td>
                    <td style={{ color: w.errors > 0 ? "var(--danger)" : undefined, fontWeight: w.errors > 0 ? 700 : undefined }}>
                      {w.errors}
                    </td>
                    <td>{fmtDate(w.last_run_at)}</td>
                    <td>
                      {w.last_status ? (
                        <span className={`badge ${statusClass(w.last_status)}`}>{w.last_status}</span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td style={{ fontSize: 12, color: "var(--muted)" }}>
                      {(w.tags || []).join(", ") || "—"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </>
      )}

      {loading && !data && <div style={{ color: "var(--muted)", fontSize: 13, marginTop: 8 }}>Loading…</div>}
      {error && <div style={{ color: "var(--danger)", fontSize: 13, marginTop: 8 }}>{error}</div>}
    </div>
  );
}

function Stat({ value, label }) {
  return (
    <div className="stat-card">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

// Runtime control for the WF7 RFT-estimate report: which sprint to scope to.
// Persisted server-side (app_settings) so changes take effect without redeploy.
const CALIBRATION_INFO =
  "WF7 grounds each estimate in how this team actually performs. It measures " +
  "the median actual-vs-estimate ratio of recently closed tickets (time logged " +
  "vs Original Estimate) and blends that factor into the predicted 'should-have' " +
  "time. Factor >1 means the team typically takes longer than it estimates " +
  "(work runs over budget); <1 means estimates are usually padded. Actuals for " +
  "the ticket being estimated aren't used — only history from completed tickets.";

// Small circular ⓘ with a hover tooltip (native title for reliability).
function InfoIcon({ text }) {
  return (
    <span
      title={text}
      style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        width: 15, height: 15, marginLeft: 5, borderRadius: "50%",
        border: "1px solid var(--muted)", color: "var(--muted)",
        fontSize: 10, fontWeight: 700, cursor: "help", fontStyle: "normal",
        lineHeight: 1, verticalAlign: "middle",
      }}
    >
      i
    </span>
  );
}

function SprintFilter() {
  const [options, setOptions] = useState([]);
  const [value, setValue] = useState("open");
  const [scope, setScope] = useState("");
  const [project, setProject] = useState("RFT");
  const [loadErr, setLoadErr] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [calibration, setCalibration] = useState(null);

  async function load() {
    setLoadErr("");
    try {
      const [cur, list] = await Promise.all([
        apiFetch("/graph-admin/rft-estimate/settings"),
        apiFetch("/graph-admin/rft-estimate/sprints"),
      ]);
      setValue(cur.value || "open");
      setScope(cur.scope_label || "");
      setProject(cur.project_key || list.project_key || "RFT");
      setOptions(list.options || []);
      if (list.error) setLoadErr(`Could not list sprints from Jira: ${list.error}`);
    } catch (err) {
      setLoadErr(err.message);
    }
    // Calibration involves a Jira history scan — fetch separately so the card
    // renders immediately and the factor fills in when ready.
    setCalibration(null);
    apiFetch("/graph-admin/rft-estimate/calibration")
      .then((c) => setCalibration(c))
      .catch(() => setCalibration({ available: false, error: true }));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save() {
    setSaving(true);
    setSaved("");
    try {
      const res = await apiFetch("/graph-admin/rft-estimate/settings", {
        method: "PUT",
        body: { value },
      });
      setScope(res.scope_label || "");
      setSaved("Saved");
      setTimeout(() => setSaved(""), 2500);
    } catch (err) {
      setSaved(`Error: ${err.message}`);
    } finally {
      setSaving(false);
    }
  }

  // If the stored value is a sprint id no longer in the active/future list
  // (e.g. completed), keep it selectable so the admin can see what's set.
  const hasValue = options.some((o) => String(o.value) === String(value));
  const selectOptions = hasValue
    ? options
    : [...options, { value, label: `Sprint ${value} (not in active list)` }];

  return (
    <div
      style={{
        background: "var(--card)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        padding: "14px 16px",
        marginBottom: 18,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div style={{ fontWeight: 700, fontSize: 13 }}>
          {project} Estimate Report (WF7) — Sprint
        </div>
        <select
          value={value}
          onChange={(e) => setValue(e.target.value)}
          style={{ width: "auto", minWidth: 280, padding: "6px 10px", fontSize: 13 }}
        >
          {selectOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <button
          style={{ width: "auto", minHeight: "unset", padding: "6px 16px", fontSize: 13 }}
          onClick={save}
          disabled={saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          className="secondary"
          style={{ width: "auto", minHeight: "unset", padding: "6px 12px", fontSize: 13 }}
          onClick={load}
        >
          Reload
        </button>
        {saved && (
          <span style={{ fontSize: 12, color: saved.startsWith("Error") ? "var(--danger)" : "var(--ok)" }}>
            {saved}
          </span>
        )}
      </div>
      <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 8 }}>
        Scopes the daily estimate digest. Current scope: <strong>{scope || "—"}</strong>.
        “Current sprint” auto-tracks the active sprint; pick a specific sprint to pin it.
      </div>
      <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>
        Estimate calibration
        <InfoIcon text={CALIBRATION_INFO} />:{" "}
        {calibration == null ? (
          <em>loading…</em>
        ) : calibration.available ? (
          <>
            <strong>×{calibration.factor}</strong> — team runs{" "}
            <strong>
              {calibration.median_pct >= 0 ? "+" : ""}
              {calibration.median_pct}%
            </strong>{" "}
            {calibration.median_pct >= 0 ? "over" : "under"} estimate (n={calibration.samples},
            last {calibration.lookback_days}d), blended at{" "}
            {Math.round((calibration.history_weight ?? 0) * 100)}%.
          </>
        ) : (
          <em>
            not enough closed-ticket history yet — using the model estimate as-is
            {calibration.error ? " (lookup failed)" : ""}.
          </em>
        )}
      </div>
      {loadErr && <div style={{ fontSize: 12, color: "var(--danger)", marginTop: 6 }}>{loadErr}</div>}
    </div>
  );
}
