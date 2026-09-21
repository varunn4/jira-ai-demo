import { useState, useEffect } from "react";
import { apiFetch } from "../api";

export default function SettingsModal({ isOpen, onClose, onSaved }) {
  const [activeTab, setActiveTab] = useState("repos");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState("");
  const [saveError, setSaveError] = useState("");

  // Raw form data
  const [formData, setFormData] = useState({
    repository_search_root: "",
    excluded_repository_names: "",
    jira_base_url: "",
    jira_email: "",
    jira_api_token: "",
    jira_project_key: "",
    jira_excluded_project_keys: "",
    llm_provider: "groq",
    llm_model: "openai/gpt-oss-120b",
    openai_base_url: "",
    openai_api_key: "",
    anthropic_api_key: "",
    anthropic_model: "claude-3-5-sonnet-20241022",
    slack_bot_token: "",
    slack_channel_id: "",
    qdrant_url: "",
    ollama_url: "",
    n8n_base_url: "",
    n8n_api_key: "",
  });

  // Masked flags
  const [meta, setMeta] = useState({
    jira_has_token: false,
    openai_has_key: false,
    anthropic_has_key: false,
    n8n_has_key: false,
  });

  // Track if user explicitly clicked "Change/Replace" for secret fields
  const [editSecrets, setEditSecrets] = useState({
    jira_api_token: false,
    openai_api_key: false,
    anthropic_api_key: false,
    slack_bot_token: false,
    n8n_api_key: false,
  });

  // Validation/Test results
  const [repoValidation, setRepoValidation] = useState(null);
  const [validatingRepo, setValidatingRepo] = useState(false);

  const [jiraTestResult, setJiraTestResult] = useState(null);
  const [testingJira, setTestingJira] = useState(false);

  const [llmTestResult, setLlmTestResult] = useState(null);
  const [testingLlm, setTestingLlm] = useState(false);

  const [n8nTestResult, setN8nTestResult] = useState(null);
  const [testingN8n, setTestingN8n] = useState(false);

  useEffect(() => {
    if (isOpen) {
      loadCurrentSettings();
    }
  }, [isOpen]);

  async function loadCurrentSettings() {
    setLoading(true);
    setSaveSuccess("");
    setSaveError("");
    setRepoValidation(null);
    setJiraTestResult(null);
    setLlmTestResult(null);

    try {
      const data = await apiFetch("/api/settings");
      if (data) {
        setFormData({
          repository_search_root: data.repository_search_root || "",
          excluded_repository_names: data.excluded_repository_names || "",
          jira_base_url: data.jira_base_url || "",
          jira_email: data.jira_email || "",
          jira_api_token: "",
          jira_project_key: data.jira_project_key || "",
          jira_excluded_project_keys: data.jira_excluded_project_keys || "",
          llm_provider: data.llm_provider || "groq",
          llm_model: data.llm_model || "openai/gpt-oss-120b",
          openai_base_url: data.openai_base_url || "",
          openai_api_key: "",
          anthropic_api_key: "",
          anthropic_model: data.anthropic_model || "claude-3-5-sonnet-20241022",
          slack_bot_token: "",
          slack_channel_id: data.slack_channel_id || "",
          qdrant_url: data.qdrant_url || "",
          ollama_url: data.ollama_url || "",
          n8n_base_url: data.n8n_base_url || "",
          n8n_api_key: "",
        });

        setMeta({
          jira_has_token: data.jira_has_token,
          openai_has_key: data.openai_has_key,
          anthropic_has_key: data.anthropic_has_key,
          n8n_has_key: data.n8n_has_key,
        });

        setEditSecrets({
          jira_api_token: !data.jira_has_token,
          openai_api_key: !data.openai_has_key,
          anthropic_api_key: !data.anthropic_has_key,
          slack_bot_token: !data.slack_bot_token_masked,
          n8n_api_key: !data.n8n_has_key,
        });
      }
    } catch (err) {
      setSaveError(`Failed to load current settings: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }

  const handleChange = (field, val) => {
    setFormData((prev) => ({ ...prev, [field]: val }));
    setSaveSuccess("");
    setSaveError("");
  };

  const handleProviderPreset = (provider) => {
    let defModel = "openai/gpt-oss-120b";
    let defUrl = "";
    if (provider === "groq") {
      defModel = "openai/gpt-oss-120b";
      defUrl = "";
    } else if (provider === "openai") {
      defModel = "gpt-4o-mini";
      defUrl = "";
    } else if (provider === "gemini") {
      provider = "openai";
      defUrl = "https://generativelanguage.googleapis.com/v1beta/openai/";
      defModel = "gemini-2.5-flash";
    } else if (provider === "anthropic") {
      defModel = "claude-3-5-sonnet-20241022";
    } else if (provider === "mock") {
      defModel = "mock-agent";
    }

    setFormData((prev) => ({
      ...prev,
      llm_provider: provider,
      llm_model: defModel,
      openai_base_url: defUrl,
    }));
    setLlmTestResult(null);
  };

  async function handleValidateRepo() {
    setValidatingRepo(true);
    setRepoValidation(null);
    try {
      const res = await apiFetch("/api/settings/validate-repo-path", {
        method: "POST",
        body: { path: formData.repository_search_root || "." },
      });
      setRepoValidation(res);
    } catch (err) {
      setRepoValidation({
        valid: false,
        error: err.message,
        repo_count: 0,
        repos: [],
      });
    } finally {
      setValidatingRepo(false);
    }
  }

  async function handleTestJira() {
    setTestingJira(true);
    setJiraTestResult(null);
    try {
      const res = await apiFetch("/api/settings/test-jira", {
        method: "POST",
        body: {
          jira_base_url: formData.jira_base_url,
          jira_email: formData.jira_email,
          jira_api_token: formData.jira_api_token,
        },
      });
      setJiraTestResult(res);
    } catch (err) {
      setJiraTestResult({ success: false, error: err.message });
    } finally {
      setTestingJira(false);
    }
  }

  async function handleTestLlm() {
    setTestingLlm(true);
    setLlmTestResult(null);
    try {
      const res = await apiFetch("/api/settings/test-llm", {
        method: "POST",
        body: {
          llm_provider: formData.llm_provider,
          api_key:
            formData.llm_provider === "anthropic"
              ? formData.anthropic_api_key
              : formData.openai_api_key,
          llm_model:
            formData.llm_provider === "anthropic"
              ? formData.anthropic_model
              : formData.llm_model,
          openai_base_url: formData.openai_base_url,
        },
      });
      setLlmTestResult(res);
    } catch (err) {
      setLlmTestResult({ success: false, error: err.message });
    } finally {
      setTestingLlm(false);
    }
  }

  async function handleTestN8n() {
    setTestingN8n(true);
    setN8nTestResult(null);
    try {
      const res = await apiFetch("/api/settings/test-n8n", {
        method: "POST",
        body: {
          n8n_base_url: formData.n8n_base_url,
          n8n_api_key: formData.n8n_api_key,
        },
      });
      setN8nTestResult(res);
    } catch (err) {
      setN8nTestResult({ success: false, error: err.message });
    } finally {
      setTestingN8n(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setSaveSuccess("");
    setSaveError("");

    // Prepare payload only including updated secrets if edited
    const payload = { ...formData };
    if (!editSecrets.jira_api_token && !formData.jira_api_token) delete payload.jira_api_token;
    if (!editSecrets.openai_api_key && !formData.openai_api_key) delete payload.openai_api_key;
    if (!editSecrets.anthropic_api_key && !formData.anthropic_api_key) delete payload.anthropic_api_key;
    if (!editSecrets.slack_bot_token && !formData.slack_bot_token) delete payload.slack_bot_token;
    if (!editSecrets.n8n_api_key && !formData.n8n_api_key) delete payload.n8n_api_key;

    try {
      await apiFetch("/api/settings", {
        method: "POST",
        body: payload,
      });
      setSaveSuccess("Configuration saved and applied successfully!");
      if (onSaved) onSaved();
    } catch (err) {
      setSaveError(`Failed to save settings: ${err.message}`);
    } finally {
      setSaving(false);
    }
  }

  if (!isOpen) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-container" onClick={(e) => e.stopPropagation()}>
        {/* Modal Header */}
        <div className="modal-header">
          <div>
            <h2 className="modal-title">⚙️ System Configuration</h2>
            <p className="modal-subtitle">
              Manage repository discovery paths, Jira credentials, and AI inference models.
            </p>
          </div>
          <button className="modal-close-btn" onClick={onClose}>
            ✕
          </button>
        </div>

        {/* Navigation Tabs */}
        <div className="modal-tabs">
          <button
            className={`modal-tab-btn ${activeTab === "repos" ? "active" : ""}`}
            onClick={() => setActiveTab("repos")}
          >
            📁 Repositories
          </button>
          <button
            className={`modal-tab-btn ${activeTab === "jira" ? "active" : ""}`}
            onClick={() => setActiveTab("jira")}
          >
            🎫 Jira Cloud
          </button>
          <button
            className={`modal-tab-btn ${activeTab === "llm" ? "active" : ""}`}
            onClick={() => setActiveTab("llm")}
          >
            🧠 AI / LLM
          </button>
          <button
            className={`modal-tab-btn ${activeTab === "integrations" ? "active" : ""}`}
            onClick={() => setActiveTab("integrations")}
          >
            💬 Slack & Vector DB
          </button>
        </div>

        {/* Modal Content */}
        <div className="modal-body">
          {loading ? (
            <div className="app-loading" style={{ height: "200px" }}>
              Loading settings...
            </div>
          ) : (
            <>
              {saveSuccess && <div className="callout callout-success">✅ {saveSuccess}</div>}
              {saveError && <div className="callout callout-danger">❌ {saveError}</div>}

              {/* TAB: Repositories */}
              {activeTab === "repos" && (
                <div className="settings-section">
                  <div className="callout callout-info">
                    <strong>Directory Scan:</strong> Point to the root directory where your codebases are stored. Subfolders with a <code>.git</code> directory are automatically indexed for the Knowledge Graph.
                  </div>

                  <div className="form-group" style={{ marginTop: "14px" }}>
                    <label className="field-label">Repository Search Root</label>
                    <div className="input-with-action">
                      <input
                        type="text"
                        className="field-input"
                        placeholder="e.g. C:\Users\Username\Projects or /home/dev/repos"
                        value={formData.repository_search_root}
                        onChange={(e) => handleChange("repository_search_root", e.target.value)}
                      />
                      <button
                        type="button"
                        className="action-btn"
                        onClick={handleValidateRepo}
                        disabled={validatingRepo}
                      >
                        {validatingRepo ? "Scanning..." : "🔍 Scan Directory"}
                      </button>
                    </div>
                  </div>

                  {repoValidation && (
                    <div
                      className={`validation-box ${
                        repoValidation.valid && repoValidation.repo_count > 0
                          ? "box-success"
                          : repoValidation.valid
                          ? "box-warning"
                          : "box-danger"
                      }`}
                    >
                      {repoValidation.valid && repoValidation.repo_count > 0 ? (
                        <div>
                          <div className="box-title">
                            ✅ Discovered {repoValidation.repo_count} Git Repository(ies)
                          </div>
                          <div className="repo-tags-grid">
                            {repoValidation.repos.map((name) => (
                              <span key={name} className="repo-tag">
                                📦 {name}
                              </span>
                            ))}
                          </div>
                          {repoValidation.resolved_path && (
                            <div className="box-sub">Resolved Path: {repoValidation.resolved_path}</div>
                          )}
                        </div>
                      ) : repoValidation.valid ? (
                        <div>
                          <div className="box-title">⚠️ 0 Git repositories found</div>
                          <div className="box-sub">{repoValidation.message}</div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title">❌ Path Error</div>
                          <div className="box-sub">{repoValidation.error}</div>
                        </div>
                      )}
                    </div>
                  )}

                  <div className="form-group" style={{ marginTop: "14px" }}>
                    <label className="field-label">Excluded Repository Names</label>
                    <input
                      type="text"
                      className="field-input"
                      placeholder="e.g. node_modules, temp-repo, archived"
                      value={formData.excluded_repository_names}
                      onChange={(e) => handleChange("excluded_repository_names", e.target.value)}
                    />
                    <span className="field-hint">Comma-separated repository names to ignore during scanning</span>
                  </div>
                </div>
              )}

              {/* TAB: Jira Cloud */}
              {activeTab === "jira" && (
                <div className="settings-section">
                  <div className="callout callout-info">
                    <strong>Atlassian Authentication:</strong> Requires your Atlassian account email and an API token from{" "}
                    <a
                      href="https://id.atlassian.com/manage-profile/security/api-tokens"
                      target="_blank"
                      rel="noreferrer"
                      className="link-highlight"
                    >
                      Atlassian Security Settings ↗
                    </a>
                    .
                  </div>

                  <div className="form-grid-2" style={{ marginTop: "14px" }}>
                    <div className="form-group">
                      <label className="field-label">Jira Base URL</label>
                      <input
                        type="url"
                        className="field-input"
                        placeholder="https://yourcompany.atlassian.net"
                        value={formData.jira_base_url}
                        onChange={(e) => handleChange("jira_base_url", e.target.value)}
                      />
                    </div>
                    <div className="form-group">
                      <label className="field-label">Jira Account Email</label>
                      <input
                        type="email"
                        className="field-input"
                        placeholder="name@company.com"
                        value={formData.jira_email}
                        onChange={(e) => handleChange("jira_email", e.target.value)}
                      />
                    </div>
                  </div>

                  <div className="form-group">
                    <label className="field-label">Jira API Token</label>
                    {meta.jira_has_token && !editSecrets.jira_api_token ? (
                      <div className="secret-saved-row">
                        <span className="secret-indicator">🔒 Token configured in database (Masked)</span>
                        <button
                          type="button"
                          className="btn-text-action"
                          onClick={() => setEditSecrets((p) => ({ ...p, jira_api_token: true }))}
                        >
                          Change Token
                        </button>
                      </div>
                    ) : (
                      <input
                        type="password"
                        className="field-input"
                        placeholder="Enter new Jira API Token (ATATT...)"
                        value={formData.jira_api_token}
                        onChange={(e) => handleChange("jira_api_token", e.target.value)}
                      />
                    )}
                  </div>

                  <div className="form-grid-2">
                    <div className="form-group">
                      <label className="field-label">Target Project Keys</label>
                      <input
                        type="text"
                        className="field-input"
                        placeholder="e.g. SCRUM, PROJ"
                        value={formData.jira_project_key}
                        onChange={(e) => handleChange("jira_project_key", e.target.value)}
                      />
                      <span className="field-hint">Comma-separated project keys</span>
                    </div>
                    <div className="form-group">
                      <label className="field-label">Excluded Project Keys</label>
                      <input
                        type="text"
                        className="field-input"
                        placeholder="e.g. ARCHIVE, TEST"
                        value={formData.jira_excluded_project_keys}
                        onChange={(e) => handleChange("jira_excluded_project_keys", e.target.value)}
                      />
                    </div>
                  </div>

                  <div style={{ marginTop: "12px" }}>
                    <button
                      type="button"
                      className="action-btn"
                      onClick={handleTestJira}
                      disabled={testingJira}
                    >
                      {testingJira ? "Testing..." : "⚡ Test Jira Connection"}
                    </button>
                  </div>

                  {jiraTestResult && (
                    <div
                      className={`validation-box ${jiraTestResult.success ? "box-success" : "box-danger"}`}
                      style={{ marginTop: "12px" }}
                    >
                      {jiraTestResult.success ? (
                        <div>
                          <div className="box-title">✅ Jira Connection Active</div>
                          <div className="box-sub">
                            Connected as: <strong>{jiraTestResult.display_name}</strong> ({jiraTestResult.email})
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title">❌ Jira Connection Failed</div>
                          <div className="box-sub">{jiraTestResult.error}</div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* TAB: AI / LLM */}
              {activeTab === "llm" && (
                <div className="settings-section">
                  <div className="provider-selector" style={{ marginBottom: "16px" }}>
                    <label
                      className={`provider-card ${formData.llm_provider === "groq" ? "selected" : ""}`}
                      onClick={() => handleProviderPreset("groq")}
                    >
                      <input type="radio" checked={formData.llm_provider === "groq"} readOnly />
                      <div className="provider-info">
                        <span className="provider-name">⚡ Groq (Fast & Free)</span>
                      </div>
                    </label>

                    <label
                      className={`provider-card ${
                        formData.llm_provider === "openai" && !formData.openai_base_url.includes("google")
                          ? "selected"
                          : ""
                      }`}
                      onClick={() => handleProviderPreset("openai")}
                    >
                      <input
                        type="radio"
                        checked={formData.llm_provider === "openai" && !formData.openai_base_url.includes("google")}
                        readOnly
                      />
                      <div className="provider-info">
                        <span className="provider-name">🤖 OpenAI</span>
                      </div>
                    </label>

                    <label
                      className={`provider-card ${
                        formData.llm_provider === "openai" && formData.openai_base_url.includes("google")
                          ? "selected"
                          : ""
                      }`}
                      onClick={() => handleProviderPreset("gemini")}
                    >
                      <input
                        type="radio"
                        checked={formData.llm_provider === "openai" && formData.openai_base_url.includes("google")}
                        readOnly
                      />
                      <div className="provider-info">
                        <span className="provider-name">✨ Google Gemini</span>
                      </div>
                    </label>

                    <label
                      className={`provider-card ${formData.llm_provider === "anthropic" ? "selected" : ""}`}
                      onClick={() => handleProviderPreset("anthropic")}
                    >
                      <input type="radio" checked={formData.llm_provider === "anthropic"} readOnly />
                      <div className="provider-info">
                        <span className="provider-name">🔮 Anthropic Claude</span>
                      </div>
                    </label>

                    <label
                      className={`provider-card ${formData.llm_provider === "mock" ? "selected" : ""}`}
                      onClick={() => handleProviderPreset("mock")}
                    >
                      <input type="radio" checked={formData.llm_provider === "mock"} readOnly />
                      <div className="provider-info">
                        <span className="provider-name">🧪 Offline Mock</span>
                      </div>
                    </label>
                  </div>

                  {formData.llm_provider === "anthropic" ? (
                    <>
                      <div className="form-group">
                        <label className="field-label">Anthropic API Key</label>
                        {meta.anthropic_has_key && !editSecrets.anthropic_api_key ? (
                          <div className="secret-saved-row">
                            <span className="secret-indicator">🔒 Key configured in database (Masked)</span>
                            <button
                              type="button"
                              className="btn-text-action"
                              onClick={() => setEditSecrets((p) => ({ ...p, anthropic_api_key: true }))}
                            >
                              Change Key
                            </button>
                          </div>
                        ) : (
                          <input
                            type="password"
                            className="field-input"
                            placeholder="sk-ant-api03-..."
                            value={formData.anthropic_api_key}
                            onChange={(e) => handleChange("anthropic_api_key", e.target.value)}
                          />
                        )}
                      </div>
                      <div className="form-group">
                        <label className="field-label">Model Identifier</label>
                        <input
                          type="text"
                          className="field-input"
                          value={formData.anthropic_model}
                          onChange={(e) => handleChange("anthropic_model", e.target.value)}
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      {formData.llm_provider !== "mock" && (
                        <div className="form-group">
                          <label className="field-label">
                            {formData.llm_provider === "groq"
                              ? "Groq API Key"
                              : formData.openai_base_url.includes("google")
                              ? "Google Gemini API Key"
                              : "OpenAI API Key"}
                          </label>
                          {meta.openai_has_key && !editSecrets.openai_api_key ? (
                            <div className="secret-saved-row">
                              <span className="secret-indicator">🔒 Key configured in database (Masked)</span>
                              <button
                                type="button"
                                className="btn-text-action"
                                onClick={() => setEditSecrets((p) => ({ ...p, openai_api_key: true }))}
                              >
                                Change Key
                              </button>
                            </div>
                          ) : (
                            <input
                              type="password"
                              className="field-input"
                              placeholder={
                                formData.llm_provider === "groq"
                                  ? "gsk_..."
                                  : formData.openai_base_url.includes("google")
                                  ? "AIzaSy..."
                                  : "sk-proj-..."
                              }
                              value={formData.openai_api_key}
                              onChange={(e) => handleChange("openai_api_key", e.target.value)}
                            />
                          )}
                        </div>
                      )}

                      <div className="form-grid-2">
                        <div className="form-group">
                          <label className="field-label">Model Identifier</label>
                          <input
                            type="text"
                            className="field-input"
                            value={formData.llm_model}
                            onChange={(e) => handleChange("llm_model", e.target.value)}
                          />
                        </div>
                        {formData.openai_base_url && (
                          <div className="form-group">
                            <label className="field-label">API Base URL</label>
                            <input
                              type="text"
                              className="field-input"
                              value={formData.openai_base_url}
                              onChange={(e) => handleChange("openai_base_url", e.target.value)}
                            />
                          </div>
                        )}
                      </div>
                    </>
                  )}

                  <div style={{ marginTop: "12px" }}>
                    <button
                      type="button"
                      className="action-btn"
                      onClick={handleTestLlm}
                      disabled={testingLlm}
                    >
                      {testingLlm ? "Testing AI..." : "🤖 Test AI Connection"}
                    </button>
                  </div>

                  {llmTestResult && (
                    <div
                      className={`validation-box ${llmTestResult.success ? "box-success" : "box-danger"}`}
                      style={{ marginTop: "12px" }}
                    >
                      {llmTestResult.success ? (
                        <div>
                          <div className="box-title">✅ AI Inference Active</div>
                          <div className="box-sub">
                            Model: <code>{llmTestResult.model}</code> | Response: "{llmTestResult.reply}"
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title">❌ AI Test Failed</div>
                          <div className="box-sub">{llmTestResult.error}</div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* TAB: Slack & Integrations */}
              {activeTab === "integrations" && (
                <div className="settings-section">
                  <div className="form-grid-2">
                    <div className="form-group">
                      <label className="field-label">Slack Bot Token</label>
                      <input
                        type="password"
                        className="field-input"
                        placeholder="xoxb-..."
                        value={formData.slack_bot_token}
                        onChange={(e) => handleChange("slack_bot_token", e.target.value)}
                      />
                    </div>
                    <div className="form-group">
                      <label className="field-label">Default Slack Channel ID</label>
                      <input
                        type="text"
                        className="field-input"
                        placeholder="C1234567890"
                        value={formData.slack_channel_id}
                        onChange={(e) => handleChange("slack_channel_id", e.target.value)}
                      />
                    </div>
                  </div>

                  <div className="form-grid-2" style={{ marginTop: "14px" }}>
                    <div className="form-group">
                      <label className="field-label">Qdrant Vector DB URL</label>
                      <input
                        type="text"
                        className="field-input"
                        placeholder="e.g. http://qdrant:6333 or http://localhost:6333"
                        value={formData.qdrant_url}
                        onChange={(e) => handleChange("qdrant_url", e.target.value)}
                      />
                    </div>
                    <div className="form-group">
                      <label className="field-label">Ollama Embeddings URL</label>
                      <input
                        type="text"
                        className="field-input"
                        placeholder="e.g. http://ollama:11434 or http://localhost:11434"
                        value={formData.ollama_url}
                        onChange={(e) => handleChange("ollama_url", e.target.value)}
                      />
                    </div>
                  </div>

                  <div style={{ marginTop: "20px", paddingTop: "14px", borderTop: "1px dashed var(--line)" }}>
                    <div style={{ fontWeight: 600, fontSize: "13px", marginBottom: "10px", color: "var(--text)" }}>
                      ⚡ n8n Automation Engine
                    </div>
                    <div className="form-grid-2">
                      <div className="form-group">
                        <label className="field-label">n8n Base URL</label>
                        <input
                          type="text"
                          className="field-input"
                          placeholder="e.g. http://localhost:5678 or https://n8n.yourdomain.com"
                          value={formData.n8n_base_url}
                          onChange={(e) => handleChange("n8n_base_url", e.target.value)}
                        />
                      </div>
                      <div className="form-group">
                        <label className="field-label">n8n API Key</label>
                        {meta.n8n_has_key && !editSecrets.n8n_api_key ? (
                          <div className="secret-saved-row">
                            <span className="secret-indicator">🔒 Key configured in database (Masked)</span>
                            <button
                              type="button"
                              className="btn-text-action"
                              onClick={() => setEditSecrets((prev) => ({ ...prev, n8n_api_key: true }))}
                            >
                              Change Key
                            </button>
                          </div>
                        ) : (
                          <input
                            type="password"
                            className="field-input"
                            placeholder="Enter n8n API Key..."
                            value={formData.n8n_api_key}
                            onChange={(e) => handleChange("n8n_api_key", e.target.value)}
                          />
                        )}
                      </div>
                    </div>

                    <div style={{ marginTop: "12px" }}>
                      <button
                        type="button"
                        className="action-btn"
                        onClick={handleTestN8n}
                        disabled={testingN8n}
                      >
                        {testingN8n ? "Testing n8n..." : "⚡ Test n8n Connection"}
                      </button>
                    </div>

                    {n8nTestResult && (
                      <div
                        className={`validation-box ${n8nTestResult.success ? "box-success" : "box-danger"}`}
                        style={{ marginTop: "12px" }}
                      >
                        {n8nTestResult.success ? (
                          <div>
                            <div className="box-title">✅ n8n Connected Successfully</div>
                            <div className="box-sub">{n8nTestResult.message}</div>
                          </div>
                        ) : (
                          <div>
                            <div className="box-title">❌ n8n Connection Failed</div>
                            <div className="box-sub">{n8nTestResult.error}</div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Modal Footer */}
        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="button" className="btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? "Saving Changes..." : "💾 Save Settings"}
          </button>
        </div>
      </div>
    </div>
  );
}
