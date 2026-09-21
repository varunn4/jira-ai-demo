import { useEffect, useRef, useState } from "react";
import { apiFetch, apiDownload, triggerBlobDownload } from "../api";
import { renderMarkdown } from "../lib/markdown";
import Header from "../components/Header.jsx";

const fmtTokens = (n) => Number(n || 0).toLocaleString();

// Costs come from the backend in USD; we display them in INR. The rate is
// supplied by the backend config (USD_TO_INR) and refreshed on page load.
let usdToInr = 95.75;
const fmtCost = (n) =>
  `₹${(Number(n || 0) * usdToInr).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

// Standalone Documentation portal. Reached only via its own URL (/docs-portal).
export default function Documentation() {
  const [repos, setRepos] = useState([]);
  const [docTypes, setDocTypes] = useState([]);
  const [repo, setRepo] = useState("");
  const [docType, setDocType] = useState("");
  const [format, setFormat] = useState("docx");
  const [status, setStatus] = useState({ msg: "", cls: "" });
  const [busy, setBusy] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [job, setJob] = useState(null); // live job record
  const [jobId, setJobId] = useState("");
  const [emailTo, setEmailTo] = useState("");
  const [emailBusy, setEmailBusy] = useState(false);
  const [estimating, setEstimating] = useState(false);
  const [confirm, setConfirm] = useState(null); // pre-flight estimate awaiting confirmation
  const [delivery, setDelivery] = useState("download"); // "download" | "email"
  const cancelRef = useRef(false);

  useEffect(() => {
    cancelRef.current = false;
    (async () => {
      try {
        const data = await apiFetch("/graph-admin/repo-docs/repositories");
        if (data.usd_to_inr) usdToInr = data.usd_to_inr;
        const list = data.repositories || [];
        setRepos(list);
        const firstReady = list.find((r) => r.ready) || list[0];
        if (firstReady) setRepo(firstReady.name);
        const types = data.doc_types || [];
        setDocTypes(types);
        if (types[0]) setDocType(types[0].id);
      } catch (err) {
        setStatus({ msg: err.message, cls: "error" });
      }
    })();
    return () => {
      cancelRef.current = true;
    };
  }, []);

  const isAll = docType === "all";
  const done = job && job.status === "done";
  const docs = (job && job.docs) || [];
  const usage = (job && job.usage) || {};
  const estimate = (job && job.estimate) || {};
  const progress = (job && job.progress) || {};

  // Step 1: ask the backend for a cost/cache estimate, then show the confirm panel.
  async function requestGenerate() {
    if (!repo) {
      setStatus({ msg: "Pick a repository first", cls: "error" });
      return;
    }
    setEstimating(true);
    setConfirm(null);
    setStatus({ msg: "Refreshing repository data & checking cost…", cls: "running" });
    try {
      const est = await apiFetch("/graph-admin/repo-docs/estimate", {
        method: "POST",
        body: { repo, doc_type: docType },
      });
      setConfirm(est);
      setStatus({ msg: "", cls: "" });
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
    } finally {
      setEstimating(false);
    }
  }

  // Step 2: user confirmed the estimate + picked a delivery method — start the job.
  function confirmGenerate() {
    if (delivery === "email" && !emailTo.trim()) {
      setStatus({ msg: "Enter a recipient email before generating", cls: "error" });
      return;
    }
    const chosenDelivery = delivery;
    setConfirm(null);
    generate(chosenDelivery);
  }

  function cancelConfirm() {
    setConfirm(null);
    setStatus({ msg: "Generation cancelled", cls: "" });
  }

  async function generate(deliveryChoice) {
    setBusy(true);
    setJob(null);
    setJobId("");
    setStatus({ msg: "Starting generation…", cls: "running" });
    const startedAt = Date.now();
    const MAX_MS = 1_200_000; // 20 min ceiling for the "all" bundle
    try {
      const start = await apiFetch("/graph-admin/repo-docs/generate", {
        method: "POST",
        body: { repo, doc_type: docType },
      });
      setJobId(start.job_id);

      // Poll, refreshing the live job each tick so progress/tokens/cost update.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        if (cancelRef.current) return;
        await new Promise((r) => setTimeout(r, 2500));
        const cur = await apiFetch(`/graph-admin/repo-docs/jobs/${start.job_id}`);
        setJob(cur);
        if (cur.status === "done") {
          setStatus({ msg: "Done", cls: "ok" });
          // Honour the delivery choice made before generation started.
          await deliver(deliveryChoice, cur);
          break;
        }
        if (cur.status === "error") {
          throw new Error(cur.error || "Document generation failed");
        }
        if (Date.now() - startedAt > MAX_MS) throw new Error("Generation timed out");
        const p = cur.progress || {};
        const secs = Math.round((Date.now() - startedAt) / 1000);
        setStatus({
          msg: `${p.current_label || "Generating…"} (${p.completed || 0}/${p.total || 1}) · ${secs}s`,
          cls: "running",
        });
      }
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
    } finally {
      setBusy(false);
    }
  }

  // Run the chosen post-generation action against a fresh job record (state may
  // not have re-rendered yet, so we read straight from the polled job).
  async function deliver(choice, jobRec) {
    if (choice === "email") {
      await sendEmailTo(jobRec.job_id, emailTo.trim());
    } else if (jobRec.doc_type === "all") {
      await downloadZipFor(jobRec.job_id, jobRec.repo || repo);
    } else {
      await downloadDoc((jobRec.docs || [])[0]);
    }
  }

  async function downloadDoc(d) {
    if (!d) return;
    const baseName = d.filename.replace(/\.md$/i, "");
    if (format === "md") {
      triggerBlobDownload(new Blob([d.markdown], { type: "text/markdown" }), `${baseName}.md`);
      return;
    }
    setDownloadBusy(true);
    setStatus({ msg: "Building Word document…", cls: "running" });
    try {
      await apiDownload("/graph-admin/repo-docs/export", {
        method: "POST",
        body: { markdown: d.markdown, filename: baseName },
        fallbackName: `${baseName}.docx`,
      });
      setStatus({ msg: "Document ready", cls: "ok" });
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
    } finally {
      setDownloadBusy(false);
    }
  }

  async function downloadZipFor(id, repoName) {
    setDownloadBusy(true);
    setStatus({ msg: "Building ZIP…", cls: "running" });
    try {
      await apiDownload(`/graph-admin/repo-docs/jobs/${id}/zip`, {
        fallbackName: `${repoName}-docs.zip`,
      });
      setStatus({ msg: "ZIP ready", cls: "ok" });
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
    } finally {
      setDownloadBusy(false);
    }
  }

  async function sendEmailTo(id, to) {
    if (!to) {
      setStatus({ msg: "Enter a recipient email", cls: "error" });
      return;
    }
    setEmailBusy(true);
    setStatus({ msg: "Sending email…", cls: "running" });
    try {
      const res = await apiFetch("/graph-admin/repo-docs/email", {
        method: "POST",
        body: { job_id: id, to_email: to },
      });
      setStatus({ msg: `Emailed to ${res.recipients.join(", ")}`, cls: "ok" });
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
    } finally {
      setEmailBusy(false);
    }
  }

  // Thin wrappers so the manual post-generation buttons reuse the same helpers.
  const downloadSingle = () => downloadDoc(docs[0]);
  const downloadZip = () => downloadZipFor(jobId, repo);
  const sendEmail = () => sendEmailTo(jobId, emailTo.trim());

  return (
    <>
      <Header title="Documentation Portal" />
      <div className="docs-portal-body">
        <div className="tc-layout">
          <div className="tc-form-col">
            <p className="tc-section-label">Generate repository document</p>
            <div className="tc-field">
              <label className="tc-label">Repository <span className="tc-required">*</span></label>
              <select className="tc-select" value={repo} onChange={(e) => setRepo(e.target.value)}>
                {repos.length === 0 && <option value="">Loading repositories…</option>}
                {repos.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.name}{r.has_packed_source ? " ✓ (Indexed)" : ""}
                  </option>
                ))}
              </select>
            </div>
            <div className="tc-field">
              <label className="tc-label">Document type</label>
              <select className="tc-select" value={docType} onChange={(e) => setDocType(e.target.value)}>
                {docTypes.map((t) => (
                  <option key={t.id} value={t.id}>{t.label}</option>
                ))}
              </select>
            </div>
            <div className="tc-field">
              <label className="tc-label">Download format</label>
              <select className="tc-select" value={format} onChange={(e) => setFormat(e.target.value)}>
                <option value="docx">Word document (.docx)</option>
                <option value="md">Markdown (.md)</option>
              </select>
            </div>
            <button
              style={{ marginTop: 18 }}
              disabled={busy || estimating || !!confirm}
              onClick={requestGenerate}
            >
              {estimating ? "Refreshing data…" : isAll ? "Generate All Documents" : "Generate Document"}
            </button>
            <div className={`status-bar ${status.cls}`} style={{ marginTop: 10 }}>{status.msg}</div>

            {/* Confirmation: show estimated cost (skipped when reused/free) and pick delivery */}
            {confirm && (
              <div className="sim-card" style={{ marginTop: 12, padding: 14 }}>
                <p className="tc-section-label" style={{ marginTop: 0 }}>Confirm generation</p>

                {/* Repomix refresh: ran before estimating, so the figures below reflect the latest code. */}
                {confirm.reindex && (
                  <p className="tc-optional" style={{ marginTop: 0, marginBottom: 6, lineHeight: 1.5 }}>
                    {confirm.reindex.error
                      ? `⚠ Could not refresh repository data (using last indexed copy): ${confirm.reindex.error}`
                      : (confirm.reindex.packed || []).length
                      ? "Repository code changed — Repomix data refreshed (free) before estimating."
                      : "Repository unchanged since last index — using current data (refresh is free)."}
                  </p>
                )}

                {confirm.all_cached ? (
                  <p className="tc-optional" style={{ marginTop: 0, lineHeight: 1.5 }}>
                    {confirm.mode === "all" ? "These documents were" : "This document was"} already generated
                    for the current code and will be <strong>reused at no cost</strong> (0 tokens).
                  </p>
                ) : (
                  <p className="tc-optional" style={{ marginTop: 0, lineHeight: 1.5 }}>
                    Estimated cost ≈ <strong>{fmtCost(confirm.total_cost_usd ?? confirm.estimated_cost_usd)}</strong>{" "}
                    ({fmtTokens(confirm.estimated_input_tokens)} input tokens; reindex {fmtCost(confirm.reindex_cost_usd || 0)}).
                    {confirm.mode === "all" &&
                      ` ${confirm.uncached_count} of ${confirm.docs.length} documents will be generated` +
                        (confirm.cached_count ? `; ${confirm.cached_count} reused free.` : ".")}
                  </p>
                )}

                <div className="tc-field" style={{ marginBottom: 8 }}>
                  <label className="tc-label">After generation</label>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 400, cursor: "pointer" }}>
                    <input
                      type="radio"
                      name="delivery"
                      checked={delivery === "download"}
                      onChange={() => setDelivery("download")}
                    />
                    Auto-download the {isAll ? "ZIP" : format === "md" ? "Markdown" : "Word document"}
                  </label>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 400, cursor: "pointer", marginTop: 4 }}>
                    <input
                      type="radio"
                      name="delivery"
                      checked={delivery === "email"}
                      onChange={() => setDelivery("email")}
                    />
                    Email it
                  </label>
                </div>

                {delivery === "email" && (
                  <div className="tc-field">
                    <input
                      className="tc-input"
                      type="text"
                      placeholder="name@example.com, other@example.com"
                      value={emailTo}
                      onChange={(e) => setEmailTo(e.target.value)}
                    />
                  </div>
                )}

                <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
                  <button style={{ width: "auto" }} onClick={confirmGenerate}>
                    {confirm.all_cached ? "Confirm (free)" : "Confirm & Generate"}
                  </button>
                  <button className="secondary" style={{ width: "auto" }} onClick={cancelConfirm}>
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {/* Live progress + token/cost meter */}
            {job && (job.status === "running" || done) && (
              <div style={{ marginTop: 12 }}>
                <div className="progress-track">
                  <div className="progress-fill" style={{ width: `${progress.percent || 0}%` }} />
                </div>
                <div className="progress-meta">
                  {progress.completed || 0}/{progress.total || 1} documents
                  {progress.current_label ? ` · ${progress.current_label}` : ""}
                </div>
                <div className="tc-stats" style={{ marginTop: 10 }}>
                  <span className="tc-stat"><span>{fmtTokens(usage.input_tokens)}</span> in</span>
                  <span className="tc-stat"><span>{fmtTokens(usage.output_tokens)}</span> out</span>
                  <span className="tc-stat"><span>{fmtCost(usage.cost_usd)}</span> cost</span>
                  {!!usage.reused_count && (
                    <span className="tc-stat"><span>{usage.reused_count}</span> reused</span>
                  )}
                </div>
                {!done && estimate.cost_usd != null && (
                  <p className="tc-optional" style={{ marginTop: 4 }}>
                    Estimated total ≈ {fmtCost(estimate.cost_usd)} ({fmtTokens(estimate.input_tokens)} input tokens)
                  </p>
                )}
              </div>
            )}

            {/* Actions once finished */}
            {done && (
              <div style={{ marginTop: 14 }}>
                {isAll ? (
                  <button className="secondary" disabled={downloadBusy} onClick={downloadZip}>
                    Download ZIP ({docs.length} docs)
                  </button>
                ) : (
                  <button className="secondary" disabled={downloadBusy} onClick={downloadSingle}>
                    Download {format === "md" ? "Markdown" : "Word"}
                  </button>
                )}

                <p className="tc-section-label" style={{ marginTop: 18 }}>Send by email</p>
                <div className="tc-field">
                  <input
                    className="tc-input"
                    type="text"
                    placeholder="name@example.com, other@example.com"
                    value={emailTo}
                    onChange={(e) => setEmailTo(e.target.value)}
                  />
                </div>
                <button className="secondary" disabled={emailBusy} onClick={sendEmail}>
                  {emailBusy ? "Sending…" : "Send via email (Word)"}
                </button>
              </div>
            )}

            <p className="tc-optional" style={{ marginTop: 14, lineHeight: 1.5 }}>
              Documents are grounded in the repository's RepoTree architecture map and Repomix packed
              source. If the code is unchanged since a previous run, the saved document is reused — no
              tokens are spent. "All documents" generates all four types and bundles them as a ZIP.
            </p>
          </div>

          <div className="tc-result-col">
            {!done ? (
              <div className="tc-placeholder">
                Pick a repository and click <strong>Generate</strong>.
              </div>
            ) : isAll ? (
              <DocList docs={docs} />
            ) : (
              <>
                <div className="tc-stats">
                  <span className="tc-stat">{docs[0]?.filename}</span>
                  {docs[0]?.reused && <span className="badge ok">reused · 0 tokens</span>}
                  {!docs[0]?.reused && (
                    <span className="tc-stat">
                      <span>{fmtTokens(docs[0]?.output_tokens)}</span> out · {fmtCost(docs[0]?.cost_usd)}
                    </span>
                  )}
                </div>
                <div className="tc-output" dangerouslySetInnerHTML={{ __html: renderMarkdown(docs[0]?.markdown || "") }} />
              </>
            )}
          </div>
        </div>

        <UsagePanel />
      </div>
    </>
  );
}

function DocList({ docs }) {
  return (
    <div>
      {docs.map((d, i) => (
        <details key={d.doc_type} open={i === 0} className="sim-card" style={{ marginBottom: 12 }}>
          <summary style={{ cursor: "pointer", fontWeight: 600 }}>
            {d.label}{" "}
            {d.reused ? (
              <span className="badge ok">reused · 0 tokens</span>
            ) : (
              <span className="badge run">{fmtTokens(d.output_tokens)} out · {fmtCost(d.cost_usd)}</span>
            )}
          </summary>
          <div className="tc-output" style={{ marginTop: 12 }} dangerouslySetInnerHTML={{ __html: renderMarkdown(d.markdown || "") }} />
        </details>
      ))}
    </div>
  );
}

function UsagePanel() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  async function load() {
    setError("");
    try {
      setData(await apiFetch("/graph-admin/repo-docs/usage?limit=25"));
    } catch (err) {
      setError(err.message);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !data) load();
  }

  const totals = data?.totals || {};
  const byUser = data?.by_user || [];

  return (
    <div style={{ marginTop: 28, borderTop: "1px solid var(--line)", paddingTop: 18 }}>
      <button
        className="secondary"
        style={{ width: "auto", minHeight: 34, padding: "6px 14px", fontSize: 13 }}
        onClick={toggle}
      >
        {open ? "Hide" : "Show"} usage &amp; cost analytics
      </button>
      {open && (
        <div style={{ marginTop: 16 }}>
          {error && <div style={{ color: "var(--danger)", fontSize: 13 }}>{error}</div>}
          {data && (
            <>
              <div className="tc-stats" style={{ marginBottom: 12 }}>
                <span className="tc-stat"><span>{fmtTokens(totals.generations)}</span> generations</span>
                <span className="tc-stat"><span>{fmtTokens(totals.reused_count)}</span> reused</span>
                <span className="tc-stat"><span>{fmtTokens(totals.input_tokens)}</span> in</span>
                <span className="tc-stat"><span>{fmtTokens(totals.output_tokens)}</span> out</span>
                <span className="tc-stat"><span>{fmtCost(totals.cost_usd)}</span> total</span>
                <span className="tc-stat" style={{ marginLeft: "auto" }}>
                  scope: {data.scope}
                  <button
                    className="secondary"
                    style={{ width: "auto", minHeight: 26, padding: "2px 10px", fontSize: 12, marginLeft: 8 }}
                    onClick={load}
                  >
                    Refresh
                  </button>
                </span>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>User</th>
                    <th style={{ width: "12%" }}>Docs</th>
                    <th style={{ width: "12%" }}>Reused</th>
                    <th style={{ width: "16%" }}>Input tokens</th>
                    <th style={{ width: "16%" }}>Output tokens</th>
                    <th style={{ width: "14%" }}>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {byUser.length === 0 ? (
                    <tr>
                      <td colSpan={6} style={{ color: "var(--muted)" }}>No usage recorded yet</td>
                    </tr>
                  ) : (
                    byUser.map((u) => (
                      <tr key={u.user_email || "unknown"}>
                        <td><b>{u.user_email || "—"}</b></td>
                        <td>{fmtTokens(u.generations)}</td>
                        <td>{fmtTokens(u.reused_count)}</td>
                        <td>{fmtTokens(u.input_tokens)}</td>
                        <td>{fmtTokens(u.output_tokens)}</td>
                        <td>{fmtCost(u.cost_usd)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
    </div>
  );
}
