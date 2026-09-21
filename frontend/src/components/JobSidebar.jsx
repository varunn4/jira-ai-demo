import EmbeddingsStatus from "./EmbeddingsStatus.jsx";

const EMBED_MODELS = ["codebase_bge_m3", "codebase_qwen3_0_6b", "codebase_mxbai_large"];

export default function JobSidebar({ options, setOption, trigger, busy, embedRefreshKey }) {
  return (
    <aside>
      <label>
        <input
          type="checkbox"
          checked={options.pullLatestCode}
          onChange={(e) => setOption("pullLatestCode", e.target.checked)}
        />
        Run git pull in local clones
      </label>
      <label>
        <input
          type="checkbox"
          checked={options.fetchLatestJira}
          onChange={(e) => setOption("fetchLatestJira", e.target.checked)}
        />
        Fetch latest Jira tickets
      </label>
      <label>
        <input
          type="checkbox"
          checked={options.includeJira}
          onChange={(e) => setOption("includeJira", e.target.checked)}
        />
        Embed Jira tickets into Qdrant
      </label>
      <label>
        <input
          type="checkbox"
          checked={options.buildEmbeddings}
          onChange={(e) => setOption("buildEmbeddings", e.target.checked)}
        />
        Build codebase embeddings
      </label>
      <label className="field-block">
        <span>Codebase embedding model</span>
        <select
          value={options.embeddingModel}
          onChange={(e) => setOption("embeddingModel", e.target.value)}
        >
          {EMBED_MODELS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </label>
      <textarea
        placeholder="Optional run note"
        value={options.notes}
        onChange={(e) => setOption("notes", e.target.value)}
      />
      <div className="toolbar">
        <button disabled={busy} onClick={() => trigger("update")}>
          Update Qdrant Vector DB
        </button>
        <button className="secondary" disabled={busy} onClick={() => trigger("regenerate")}>
          Regenerate Qdrant Vector DB
        </button>
        <button className="warn" disabled={busy} onClick={() => trigger("jira_tickets_only")}>
          Embed Jira Tickets Only
        </button>
        <button className="danger" disabled={busy} onClick={() => trigger("create_new")}>
          Rebuild Qdrant Vector DB
        </button>
        <button className="secondary" disabled={busy} onClick={() => trigger("jira_tickets_only")}>
          Refresh Jira Tickets Only
        </button>
      </div>
      <EmbeddingsStatus refreshKey={embedRefreshKey} />
    </aside>
  );
}
