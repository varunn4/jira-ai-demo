import { useState } from "react";
import { apiFetch } from "../api";
import { renderMarkdown } from "../lib/markdown";

const STYLES = [
  { value: "plain", label: "Plain Test Cases" },
  { value: "gherkin", label: "Gherkin" },
  { value: "pytest", label: "pytest" },
  { value: "junit", label: "JUnit 5" },
];
const EMBED_MODELS = ["codebase_bge_m3", "codebase_qwen3_0_6b", "codebase_mxbai_large"];
const TOP_K = [5, 10, 15, 20, 30, 50];
const ISSUE_TYPES = ["Bug", "Story", "Task", "Epic", "Improvement", "Sub-task"];

export default function TestCases() {
  const [form, setForm] = useState({
    audience: "qa",
    style: "plain",
    embeddingModel: "codebase_bge_m3",
    topK: 15,
    repo: "",
    issueKey: "",
    issueType: "Bug",
    summary: "",
    description: "",
  });
  const [status, setStatus] = useState({ msg: "", cls: "" });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null); // { markdown, hits, repos, files }
  const [copied, setCopied] = useState(false);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  async function generate() {
    const issueKey = form.issueKey.trim();
    const summary = form.summary.trim();
    if (!issueKey || !summary) {
      setStatus({ msg: "Issue key and summary are required", cls: "error" });
      return;
    }
    setBusy(true);
    setResult(null);
    const who = form.audience === "dev" ? "developer / code review" : "QA";
    setStatus({ msg: `Generating ${who} test cases with RepoTree… this may take 20–40 seconds`, cls: "running" });
    try {
      const data = await apiFetch("/analyze-ticket/test-cases", {
        method: "POST",
        body: {
          ticket_data: {
            issueKey,
            issueType: form.issueType,
            summary,
            description: form.description.trim() || null,
          },
          repo: form.repo.trim() || null,
          embedding_model: form.embeddingModel,
          style: form.style,
          audience: form.audience,
          top_k: parseInt(form.topK, 10),
        },
      });
      setResult({
        markdown: data.test_cases || "",
        hits: data.semantic_hits_count ?? 0,
        repos: (data.grounded_repos || []).length || data.functions_found || 0,
        files: data.files_touched_count ?? 0,
      });
      setStatus({ msg: "Done", cls: "ok" });
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
    } finally {
      setBusy(false);
    }
  }

  function copy() {
    if (!result?.markdown) return;
    navigator.clipboard.writeText(result.markdown).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }

  return (
    <div className="tc-layout">
      <div className="tc-form-col">
        <p className="tc-section-label">Audience</p>
        <div className="tc-field">
          <div className="tc-audience-toggle" role="radiogroup" aria-label="Test case audience">
            {[
              { value: "qa", label: "QA", hint: "Functional test cases (Ready for QA)" },
              { value: "dev", label: "Developer", hint: "Implementation-level cases (Code Review)" },
            ].map((opt) => (
              <button
                key={opt.value}
                type="button"
                role="radio"
                aria-checked={form.audience === opt.value}
                className={`tc-audience-btn${form.audience === opt.value ? " active" : ""}`}
                title={opt.hint}
                onClick={() => set("audience", opt.value)}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <p className="tc-optional" style={{ marginTop: 6 }}>
            {form.audience === "dev"
              ? "Cases a developer runs to verify their own implementation before code review."
              : "Functional test cases for QA validation."}
          </p>
        </div>

        <p className="tc-section-label" style={{ marginTop: 18 }}>Search settings</p>
        <div className="tc-field-row">
          <div className="tc-field">
            <label className="tc-label">Style</label>
            <select className="tc-select" value={form.style} onChange={(e) => set("style", e.target.value)}>
              {STYLES.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>
          <div className="tc-field">
            <label className="tc-label">Embedding model</label>
            <select className="tc-select" value={form.embeddingModel} onChange={(e) => set("embeddingModel", e.target.value)}>
              {EMBED_MODELS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="tc-field-row">
          <div className="tc-field">
            <label className="tc-label">Top K hits</label>
            <select className="tc-select" value={form.topK} onChange={(e) => set("topK", e.target.value)}>
              {TOP_K.map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="tc-field">
          <label className="tc-label">
            Repository <span className="tc-optional">(optional — leave blank for all repos)</span>
          </label>
          <input className="tc-input" type="text" placeholder="e.g. my-backend-repo" value={form.repo} onChange={(e) => set("repo", e.target.value)} />
        </div>

        <p className="tc-section-label" style={{ marginTop: 18 }}>Ticket fields</p>
        <div className="tc-field-row">
          <div className="tc-field">
            <label className="tc-label">Issue key <span className="tc-required">*</span></label>
            <input className="tc-input" type="text" placeholder="RFC-101" value={form.issueKey} onChange={(e) => set("issueKey", e.target.value)} />
          </div>
          <div className="tc-field">
            <label className="tc-label">Issue type</label>
            <select className="tc-select" value={form.issueType} onChange={(e) => set("issueType", e.target.value)}>
              {ISSUE_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="tc-field">
          <label className="tc-label">Summary <span className="tc-required">*</span></label>
          <input className="tc-input" type="text" placeholder="Short description of the ticket" value={form.summary} onChange={(e) => set("summary", e.target.value)} />
        </div>
        <div className="tc-field">
          <label className="tc-label">Description</label>
          <textarea className="tc-textarea" rows={5} placeholder="Full ticket description…" value={form.description} onChange={(e) => set("description", e.target.value)} />
        </div>
        <button style={{ marginTop: 18 }} disabled={busy} onClick={generate}>
          Generate Test Cases
        </button>
        <div className={`status-bar ${status.cls}`} style={{ marginTop: 10 }}>{status.msg}</div>
      </div>

      <div className="tc-result-col">
        {!result ? (
          <div className="tc-placeholder">
            Fill in the ticket details and click <strong>Generate Test Cases</strong>.
          </div>
        ) : (
          <>
            <div className="tc-stats">
              <span className="tc-stat"><span>{result.hits}</span> semantic hits</span>
              <span className="tc-stat"><span>{result.repos}</span> Repomix repos</span>
              <span className="tc-stat"><span>{result.files}</span> files</span>
              <button
                className="secondary"
                style={{ width: "auto", minHeight: 32, padding: "5px 14px", fontSize: 13, marginLeft: "auto" }}
                onClick={copy}
              >
                {copied ? "Copied!" : "Copy Markdown"}
              </button>
            </div>
            <div className="tc-output" dangerouslySetInnerHTML={{ __html: renderMarkdown(result.markdown) }} />
          </>
        )}
      </div>
    </div>
  );
}
