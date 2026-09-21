// Ring Studio — diamond/gold/solitaire ring image-prompt generator.
//
// Assembles CELESTE-style luxury jewelry spec-sheet prompts from value banks.
// Pin any field via the dropdowns (blank = randomize), set a seed for
// reproducible designs, preview the master prompt + spec, optionally render an
// image, and batch-export prompts as JSONL for a 1000-image run.
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch, apiDownload, apiUpload } from "../api.js";

const MAX_UPLOADS = 4;

// The dropdown fields we expose, in display order. Each maps to a bank key.
const FORM_FIELDS = [
  ["ring_name", "Ring Name"],
  ["ring_subtitle", "Subtitle"],
  ["diamond_shape", "Diamond Shape"],
  ["carat", "Carat"],
  ["color", "Color"],
  ["clarity", "Clarity"],
  ["cut", "Cut"],
  ["metal", "Metal"],
  ["band_width", "Band Width"],
  ["setting", "Setting"],
  ["background", "Background"],
  ["accent_color", "Accent Color"],
  ["ring_size", "Ring Size"],
  ["motif", "Botanical Motif"],
];

// Meta keys shown in the spec summary, in catalog order.
const SPEC_ROWS = [
  ["diamond_shape", "Center Diamond"],
  ["carat", "Carat"],
  ["color", "Color"],
  ["clarity", "Clarity"],
  ["cut", "Cut"],
  ["metal", "Metal"],
  ["band_width", "Band Width"],
  ["setting", "Setting"],
  ["total_diamonds", "Total Diamonds"],
  ["accent_carat", "Accent Carat"],
  ["detail_label", "Detail View"],
  ["ring_size", "Ring Size"],
  ["motif", "Motif"],
];

export default function RingStudio() {
  const [banks, setBanks] = useState(null);
  const [overrides, setOverrides] = useState({});
  const [seed, setSeed] = useState("");
  const [result, setResult] = useState(null); // { prompt, meta, seed }
  const [views, setViews] = useState(null); // RingViewsResult { status, views[], cost_usd, message }
  const [renderJobId, setRenderJobId] = useState(null); // for ZIP download
  const [quality, setQuality] = useState("medium"); // low | medium | high
  // Uploaded reference photos (up to 4 angles of ONE ring) that seed the render.
  const [uploads, setUploads] = useState([]); // [{ file, url }]
  // The reference ring's real measurements — prompt context + weight estimate.
  const [details, setDetails] = useState({
    ring_size: "",
    metal_weight: "",
    gross_weight: "",
  });
  // Gallery of everything generated so far (persisted server-side).
  const [galleryOpen, setGalleryOpen] = useState(false);
  const [gallery, setGallery] = useState(null);
  const [galleryLoading, setGalleryLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  // Batch controls.
  const [batchCount, setBatchCount] = useState(10);
  const [batchStart, setBatchStart] = useState(0);
  const [batching, setBatching] = useState(false);

  useEffect(() => {
    apiFetch("/ring-studio/banks")
      .then(setBanks)
      .catch((e) => setError(e.message));
  }, []);

  // Revoke object URLs for uploaded previews on unmount.
  useEffect(() => () => uploads.forEach((u) => URL.revokeObjectURL(u.url)), [uploads]);

  const addUploads = (fileList) => {
    const picked = Array.from(fileList || []).filter((f) => f.type.startsWith("image/"));
    if (!picked.length) return;
    setUploads((prev) => {
      const next = [...prev];
      for (const f of picked) {
        if (next.length >= MAX_UPLOADS) break;
        next.push({ file: f, url: URL.createObjectURL(f) });
      }
      return next;
    });
  };

  const removeUpload = (i) =>
    setUploads((prev) => {
      const u = prev[i];
      if (u) URL.revokeObjectURL(u.url);
      return prev.filter((_, idx) => idx !== i);
    });

  const setField = (key, value) =>
    setOverrides((o) => {
      const next = { ...o };
      if (value === "") delete next[key];
      else next[key] = value;
      return next;
    });

  const body = useCallback(() => {
    const s = seed.trim();
    return {
      seed: s === "" ? null : Number(s),
      overrides,
    };
  }, [seed, overrides]);

  const generate = async () => {
    setBusy(true);
    setError("");
    setViews(null);
    try {
      const data = await apiFetch("/ring-studio/prompt", { method: "POST", body: body() });
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const surprise = () => {
    setOverrides({});
    setSeed(String(Math.floor(Math.random() * 1_000_000)));
    // Generate on the next tick with the fresh seed.
    setTimeout(generate, 0);
  };

  const renderImages = async () => {
    if (!uploads.length) {
      setError("Upload 1–4 reference photos of your ring first.");
      return;
    }
    setRendering(true);
    setError("");
    setViews(null);
    setRenderJobId(null);
    try {
      // Feed the uploaded photos (multipart) as the multi-angle reference. The
      // render takes ~40–60s, so the API returns a job id we poll for — keeping
      // each request short of proxy timeouts. If a design was generated, pass its
      // meta so the global style/spec applies; otherwise the server picks one.
      const form = new FormData();
      uploads.forEach((u) => form.append("files", u.file, u.file.name));
      form.append("quality", quality);
      if (result?.meta) form.append("meta_json", JSON.stringify(result.meta));
      Object.entries(details).forEach(([k, v]) => {
        if (v.trim()) form.append(k, v.trim());
      });

      const { job_id } = await apiUpload("/ring-studio/upload-render", form);
      setRenderJobId(job_id);
      const job = await pollRenderJob(job_id);
      if (job.status === "failed") {
        setError(job.error || "Rendering failed.");
      } else {
        setViews(job.result);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setRendering(false);
    }
  };

  const downloadZip = async () => {
    if (!renderJobId) return;
    try {
      await apiDownload(`/ring-studio/image/${renderJobId}/zip`, {
        fallbackName: "ring-views.zip",
      });
    } catch (e) {
      setError(e.message);
    }
  };

  const copyPrompt = async () => {
    if (!result?.prompt) return;
    try {
      await navigator.clipboard.writeText(result.prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked */
    }
  };

  const downloadBatch = async () => {
    setBatching(true);
    setError("");
    try {
      await apiDownload("/ring-studio/batch.jsonl", {
        method: "POST",
        body: { count: Number(batchCount), start_seed: Number(batchStart) },
        fallbackName: "ring_prompts.jsonl",
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setBatching(false);
    }
  };

  const openGallery = async () => {
    setGalleryOpen(true);
    setGalleryLoading(true);
    try {
      setGallery(await apiFetch("/ring-studio/gallery"));
    } catch (e) {
      setGallery({ entries: [], message: e.message });
    } finally {
      setGalleryLoading(false);
    }
  };

  const setDetail = (key, value) => setDetails((d) => ({ ...d, [key]: value }));

  const pinnedCount = useMemo(() => Object.keys(overrides).length, [overrides]);
  const meta = result?.meta;

  return (
    <div className="ring-studio">
      <header className="ring-hero">
        <button
          className="ring-gallery-btn"
          onClick={openGallery}
          title="View all generated rings"
          aria-label="View all generated rings"
        >
          🖼️
        </button>
        <h2>💎 Ring Studio</h2>
        <p className="muted">
          <b>Upload up to 4 photos of your ring</b>, add its details, and render it
          re-designed across all 4 views — only the prompt and your photos drive
          it. The Design Spec form is a separate, optional tool for previewing the
          master prompt and exporting prompt batches; it does not affect a
          reference render.
        </p>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}

      <div className="ring-grid">
        {/* ── Design form ─────────────────────────────────────────── */}
        <section className="ring-form card">
          <h3>Design Spec</h3>
          <div className="ring-fields">
            {FORM_FIELDS.map(([key, label]) => (
              <label key={key} className="ring-field">
                <span>{label}</span>
                <select
                  className="tc-select"
                  value={overrides[key] || ""}
                  onChange={(e) => setField(key, e.target.value)}
                >
                  <option value="">Randomize</option>
                  {(banks?.banks?.[key] || []).map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>

          <div className="ring-seed-row">
            <label className="ring-field">
              <span>Seed (optional — reproducible)</span>
              <input
                value={seed}
                onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))}
                placeholder="e.g. 42"
                inputMode="numeric"
              />
            </label>
            <div className="ring-pins muted">
              {pinnedCount ? `${pinnedCount} field${pinnedCount > 1 ? "s" : ""} pinned` : "All randomized"}
            </div>
          </div>

          <div className="ring-actions">
            <button onClick={generate} disabled={busy || !banks}>
              {busy ? "Assembling…" : "Generate Prompt"}
            </button>
            <button className="secondary" onClick={surprise} disabled={busy || !banks}>
              🎲 Surprise Me
            </button>
            <button
              className="secondary"
              onClick={() => {
                setOverrides({});
                setSeed("");
              }}
              disabled={busy}
            >
              Reset
            </button>
          </div>

          <div className="ring-batch">
            <h4>Batch export</h4>
            <p className="muted">
              Generate a reproducible batch (seed = start + index) and download
              as <code>ring_prompts.jsonl</code>.
            </p>
            <div className="ring-batch-row">
              <label className="ring-field">
                <span>Count (1–1000)</span>
                <input
                  type="number"
                  min={1}
                  max={1000}
                  value={batchCount}
                  onChange={(e) => setBatchCount(e.target.value)}
                />
              </label>
              <label className="ring-field">
                <span>Start seed</span>
                <input
                  type="number"
                  value={batchStart}
                  onChange={(e) => setBatchStart(e.target.value)}
                />
              </label>
              <button className="secondary" onClick={downloadBatch} disabled={batching}>
                {batching ? "Building…" : "Download JSONL"}
              </button>
            </div>
          </div>

        </section>

        {/* ── Preview ─────────────────────────────────────────────── */}
        <section className="ring-preview card">
          {/* The master prompt is an OPTIONAL preview — the reference flow below
              ignores the design fields, so it is not a prerequisite for rendering. */}
          {result ? (
            <>
              <div className="ring-titlebar">
                <div>
                  <div className="ring-name">{meta.ring_name}</div>
                  <div className="ring-subtitle">{meta.ring_subtitle}</div>
                </div>
                {result.seed != null && (
                  <span className="ring-seed-tag">seed {result.seed}</span>
                )}
              </div>

              <p className="ring-desc">{meta.description}</p>

              <table className="ring-spec">
                <tbody>
                  {SPEC_ROWS.map(([key, label]) => (
                    <tr key={key}>
                      <th>{label}</th>
                      <td>{String(meta[key])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div className="ring-prompt-head">
                <h4>Master prompt</h4>
                <button className="link" onClick={copyPrompt}>
                  {copied ? "Copied ✓" : "Copy"}
                </button>
              </div>
              <pre className="ring-prompt">{result.prompt}</pre>
            </>
          ) : (
            <p className="ring-empty muted">
              Upload your ring photos below and hit <b>Render 4 Views</b>. (Hitting{" "}
              <b>Generate Prompt</b> first is optional — it only previews the
              master prompt; the design fields don't affect a reference render.)
            </p>
          )}

              <div className="ring-upload">
                <div className="ring-upload-head">
                  <h4>Reference photos (up to {MAX_UPLOADS} views of your ring)</h4>
                  <span className="muted">{uploads.length}/{MAX_UPLOADS}</span>
                </div>
                <p className="muted">
                  Upload photos of one ring from different angles. They're used
                  together as the reference — the design fields above are ignored
                  and the ring is re-designed from the raw prompt across all 4
                  views.
                </p>
                <div className="ring-upload-grid">
                  {uploads.map((u, i) => (
                    <div key={u.url} className="ring-upload-thumb">
                      <img src={u.url} alt={`Reference ${i + 1}`} />
                      <button
                        type="button"
                        className="ring-upload-remove"
                        onClick={() => removeUpload(i)}
                        disabled={rendering}
                        aria-label="Remove"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  {uploads.length < MAX_UPLOADS ? (
                    <label className="ring-upload-add">
                      <input
                        type="file"
                        accept="image/*"
                        multiple
                        disabled={rendering}
                        onChange={(e) => {
                          addUploads(e.target.files);
                          e.target.value = "";
                        }}
                      />
                      <span>+ Add</span>
                    </label>
                  ) : null}
                </div>

                <h4 className="ring-details-head">Reference ring details</h4>
                <p className="muted">
                  From the product page of the ring you uploaded. Used as scale
                  context in the prompt, and to estimate the new ring's weights.
                </p>
                <div className="ring-details-row">
                  {[
                    ["ring_size", "Ring Size", "e.g. 24 (20.3 mm)"],
                    ["metal_weight", "Metal Weight (g)", "e.g. 5.29"],
                    ["gross_weight", "Gross Weight (g)", "e.g. 5.36"],
                  ].map(([key, label, placeholder]) => (
                    <label key={key} className="ring-field">
                      <span>{label}</span>
                      <input
                        value={details[key]}
                        onChange={(e) => setDetail(key, e.target.value)}
                        placeholder={placeholder}
                        disabled={rendering}
                      />
                    </label>
                  ))}
                </div>
              </div>

              <div className="ring-render-controls">
                <label className="ring-field ring-quality">
                  <span>Quality</span>
                  <select
                    className="tc-select"
                    value={quality}
                    onChange={(e) => setQuality(e.target.value)}
                    disabled={rendering}
                  >
                    <option value="high">High (sharpest · costliest)</option>
                    <option value="medium">Medium (balanced)</option>
                    <option value="low">Low (fastest · cheapest)</option>
                  </select>
                </label>
                <button onClick={renderImages} disabled={rendering || !uploads.length}>
                  {rendering ? "Rendering 4 views…" : "🖼️ Render 4 Views"}
                </button>
              </div>
              <span className="ring-render-hint muted">
                {uploads.length
                  ? "Hero re-designs your uploaded ring from the prompt; Top · Side · Laydown then carry that same new ring forward."
                  : "Upload at least one reference photo above to render."}
              </span>

              {views ? (
                <>
                  <ProductSummaryPanel summary={views.summary} />
                  <ViewsGallery result={views} onDownloadZip={downloadZip} />
                </>
              ) : null}
        </section>
      </div>

      {galleryOpen ? (
        <GalleryModal
          data={gallery}
          loading={galleryLoading}
          onClose={() => setGalleryOpen(false)}
        />
      ) : null}
    </div>
  );
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Poll a queued render until it finishes. Each GET is cheap and returns fast,
// so it never hits the gateway timeout that blocked the old inline render.
async function pollRenderJob(jobId, { intervalMs = 2500, timeoutMs = 5 * 60 * 1000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await apiFetch(`/ring-studio/image/${jobId}`);
    if (job.status === "completed" || job.status === "failed") return job;
    await sleep(intervalMs);
  }
  throw new Error("Rendering timed out — the images may still be processing. Try again shortly.");
}

const fmtUsd = (n) =>
  typeof n === "number" ? `$${n.toFixed(n < 0.1 ? 4 : 3)}` : null;

const SUMMARY_ROWS = [
  ["style_no", "Style No."],
  ["ring_size", "Ring Size"],
  ["metal_weight", "Metal Weight"],
  ["gross_weight", "Gross Weight"],
];

const withUnit = (key, value) =>
  value && (key === "metal_weight" || key === "gross_weight") ? `${value} g` : value;

// Catalog Product Summary for the generated ring. Style No. is minted per render;
// the weights are an LLM estimate from the reference ring's measurements.
function ProductSummaryPanel({ summary }) {
  if (!summary) return null;
  return (
    <div className="ring-summary">
      <div className="ring-summary-head">
        <h4>Product Summary</h4>
        <span className="muted">
          {summary.estimated ? "weights estimated" : "as provided"}
        </span>
      </div>
      <table className="ring-spec">
        <tbody>
          {SUMMARY_ROWS.map(([key, label]) => (
            <tr key={key}>
              <th>{label}</th>
              <td>{withUnit(key, summary[key]) || <span className="muted">—</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {summary.note ? <p className="muted ring-summary-note">{summary.note}</p> : null}
    </div>
  );
}

// Everything generated so far, newest first (persisted server-side).
function GalleryModal({ data, loading, onClose }) {
  const entries = data?.entries || [];
  return (
    <div className="ring-modal-backdrop" onClick={onClose}>
      <div className="ring-modal" onClick={(e) => e.stopPropagation()}>
        <div className="ring-modal-head">
          <h3>Gallery {entries.length ? `(${entries.length})` : ""}</h3>
          <button className="link" onClick={onClose}>Close</button>
        </div>
        {loading ? (
          <p className="muted">Loading…</p>
        ) : data?.message ? (
          <div className="ring-image-note muted">{data.message}</div>
        ) : !entries.length ? (
          <p className="muted">Nothing generated yet.</p>
        ) : (
          <ul className="ring-gallery-list">
            {entries.map((e) => (
              <li key={e.style_no} className="ring-gallery-entry">
                <div className="ring-gallery-meta">
                  <b>{e.style_no}</b>
                  <span className="muted">
                    {[
                      e.ring_size,
                      e.metal_weight ? `${e.metal_weight} g metal` : null,
                      e.gross_weight ? `${e.gross_weight} g gross` : null,
                      e.created_at ? new Date(e.created_at).toLocaleString() : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </div>
                <div className="ring-gallery-thumbs">
                  {(e.images || []).map((img) => (
                    <a key={img.image_url} href={img.image_url} target="_blank" rel="noreferrer">
                      <img src={img.image_url} alt={img.label || img.view} />
                    </a>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ViewsGallery({ result, onDownloadZip }) {
  const notConfigured = result.status === "not_configured";
  const renderedCount = (result.views || []).filter((v) => v.status === "rendered").length;
  const cost = fmtUsd(result.cost_usd);
  return (
    <div className="ring-views">
      {result.message ? (
        <div className={notConfigured ? "ring-image-note muted" : "error-banner"}>
          {result.message}
        </div>
      ) : null}

      {renderedCount > 0 ? (
        <div className="ring-views-toolbar">
          <div className="ring-image-meta muted">
            {result.model ? `Rendered with ${result.model}` : null}
            {result.quality ? ` · ${result.quality} quality` : null}
            {cost ? (
              <>
                {" · "}
                <b>Cost: {cost}</b>
                <span className="ring-cost-note"> (est. for {renderedCount} image{renderedCount > 1 ? "s" : ""})</span>
              </>
            ) : null}
          </div>
          <button className="secondary" onClick={onDownloadZip}>
            ⬇ Download all as ZIP
          </button>
        </div>
      ) : result.model ? (
        <div className="ring-image-meta muted">Rendered with {result.model}</div>
      ) : null}

      <div className="ring-views-grid">
        {(result.views || []).map((v) => (
          <ViewCard key={v.view} v={v} />
        ))}
      </div>
    </div>
  );
}

function ViewCard({ v }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(v.prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked */
    }
  };
  return (
    <figure className="ring-view-card">
      {v.status === "rendered" && v.image_url ? (
        <a href={v.image_url} download title="Download PNG">
          <img src={v.image_url} alt={v.label} />
        </a>
      ) : v.status === "error" ? (
        <div className="ring-view-err">{v.message || "Render failed."}</div>
      ) : (
        <div className="ring-view-placeholder">
          <button className="link" onClick={copy}>
            {copied ? "Copied ✓" : "Copy prompt"}
          </button>
        </div>
      )}
      <figcaption>
        {v.label}
        {v.status === "rendered" && typeof v.cost_usd === "number" ? (
          <span className="ring-view-cost muted"> · {fmtUsd(v.cost_usd)}</span>
        ) : null}
      </figcaption>
    </figure>
  );
}
