import { useState, useEffect } from "react";
import { apiFetch } from "../api";
import { GithubLogo } from "@phosphor-icons/react";
import { Button } from "./ui/button";
import {
  Settings,
  X,
  Ticket,
  Brain,
  MessageSquare,
  CheckCircle2,
  XCircle,
  Check,
  Building2,
  Link2,
  Lock,
  Search,
  Zap,
  FolderGit2,
  Bot,
  Sparkles,
  Cpu,
  FlaskConical,
  Save,
  ExternalLink,
} from "lucide-react";

export default function SettingsModal({ isOpen, onClose, onSaved }) {
  const [activeTab, setActiveTab] = useState("repos");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState("");
  const [saveError, setSaveError] = useState("");

  // Raw form data
  const [formData, setFormData] = useState({
    github_source_type: "urls",
    github_org_or_user: "",
    github_repo_urls: "",
    github_token: "",
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
    zoho_client_id: "",
    zoho_client_secret: "",
    zoho_refresh_token: "",
    zoho_org_id: "",
    zoho_accounts_base: "https://accounts.zoho.in",
    zoho_desk_base: "https://desk.zoho.in",
  });

  // Masked flags
  const [meta, setMeta] = useState({
    github_has_token: false,
    jira_has_token: false,
    openai_has_key: false,
    anthropic_has_key: false,
    slack_has_token: false,
    n8n_has_key: false,
    zoho_has_client_secret: false,
    zoho_has_refresh_token: false,
  });

  // Track if user explicitly clicked "Change/Replace" for secret fields
  const [editSecrets, setEditSecrets] = useState({
    github_token: false,
    jira_api_token: false,
    openai_api_key: false,
    anthropic_api_key: false,
    slack_bot_token: false,
    n8n_api_key: false,
    zoho_client_secret: false,
    zoho_refresh_token: false,
  });

  // Validation/Test results
  const [repoValidation, setRepoValidation] = useState(null);
  const [validatingRepo, setValidatingRepo] = useState(false);

  const [jiraTestResult, setJiraTestResult] = useState(null);
  const [testingJira, setTestingJira] = useState(false);

  const [llmTestResult, setLlmTestResult] = useState(null);
  const [testingLlm, setTestingLlm] = useState(false);

  const [slackTestResult, setSlackTestResult] = useState(null);
  const [testingSlack, setTestingSlack] = useState(false);

  const [n8nTestResult, setN8nTestResult] = useState(null);
  const [testingN8n, setTestingN8n] = useState(false);

  const [zohoTestResult, setZohoTestResult] = useState(null);
  const [testingZoho, setTestingZoho] = useState(false);

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
    setSlackTestResult(null);

    try {
      const [data, statusData] = await Promise.all([
        apiFetch("/api/settings").catch(() => null),
        apiFetch("/api/settings/status").catch(() => null),
      ]);

      if (data) {
        setFormData({
          github_source_type: data.github_source_type || "org",
          github_org_or_user: data.github_org_or_user || "",
          github_repo_urls: data.github_repo_urls || "",
          github_token: "",
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
          zoho_client_id: data.zoho_client_id || "",
          zoho_client_secret: "",
          zoho_refresh_token: "",
          zoho_org_id: data.zoho_org_id || "",
          zoho_accounts_base: data.zoho_accounts_base || "https://accounts.zoho.in",
          zoho_desk_base: data.zoho_desk_base || "https://desk.zoho.in",
        });

        setMeta({
          github_has_token: data.github_has_token,
          jira_has_token: data.jira_has_token,
          openai_has_key: data.openai_has_key,
          anthropic_has_key: data.anthropic_has_key,
          slack_has_token: data.slack_has_token,
          n8n_has_key: data.n8n_has_key,
          zoho_has_client_secret: data.zoho_has_client_secret,
          zoho_has_refresh_token: data.zoho_has_refresh_token,
        });

        setEditSecrets({
          github_token: !data.github_has_token,
          jira_api_token: !data.jira_has_token,
          openai_api_key: !data.openai_has_key,
          anthropic_api_key: !data.anthropic_has_key,
          slack_bot_token: !data.slack_has_token,
          n8n_api_key: !data.n8n_has_key,
          zoho_client_secret: !data.zoho_has_client_secret,
          zoho_refresh_token: !data.zoho_has_refresh_token,
        });
      }

      if (statusData && statusData.repositories_count > 0) {
        setRepoValidation({
          valid: true,
          repo_count: statusData.repositories_count,
          repos: statusData.repositories || [],
          message: `Found ${statusData.repositories_count} repository(ies) active in workspace.`,
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

  async function handleValidateGithub() {
    setValidatingRepo(true);
    setRepoValidation(null);
    try {
      const res = await apiFetch("/api/settings/validate-github", {
        method: "POST",
        body: {
          source_type: formData.github_source_type,
          github_org_or_user: formData.github_org_or_user,
          github_repo_urls: formData.github_repo_urls,
          github_token: formData.github_token,
        },
      });
      setRepoValidation(res);
      if (res.valid) {
        setSaveSuccess(`Synchronized ${res.repo_count} repositories from GitHub!`);
      }
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

  async function handleTestSlack() {
    setTestingSlack(true);
    setSlackTestResult(null);
    try {
      const res = await apiFetch("/api/settings/test-slack", {
        method: "POST",
        body: {
          slack_bot_token: formData.slack_bot_token,
          slack_channel_id: formData.slack_channel_id,
        },
      });
      setSlackTestResult(res);
    } catch (err) {
      setSlackTestResult({ success: false, error: err.message });
    } finally {
      setTestingSlack(false);
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

  async function handleTestZoho() {
    setTestingZoho(true);
    setZohoTestResult(null);
    try {
      const res = await apiFetch("/api/settings/test-zoho", {
        method: "POST",
        body: {
          zoho_client_id: formData.zoho_client_id,
          zoho_client_secret: formData.zoho_client_secret,
          zoho_refresh_token: formData.zoho_refresh_token,
          zoho_org_id: formData.zoho_org_id,
          zoho_accounts_base: formData.zoho_accounts_base,
          zoho_desk_base: formData.zoho_desk_base,
        },
      });
      setZohoTestResult(res);
    } catch (err) {
      setZohoTestResult({ success: false, error: err.message });
    } finally {
      setTestingZoho(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setSaveSuccess("");
    setSaveError("");

    // Prepare payload only including updated secrets if edited
    const payload = { ...formData };
    if (!editSecrets.github_token && !formData.github_token) delete payload.github_token;
    if (!editSecrets.jira_api_token && !formData.jira_api_token) delete payload.jira_api_token;
    if (!editSecrets.openai_api_key && !formData.openai_api_key) delete payload.openai_api_key;
    if (!editSecrets.anthropic_api_key && !formData.anthropic_api_key) delete payload.anthropic_api_key;
    if (!editSecrets.slack_bot_token && !formData.slack_bot_token) delete payload.slack_bot_token;
    if (!editSecrets.n8n_api_key && !formData.n8n_api_key) delete payload.n8n_api_key;
    if (!editSecrets.zoho_client_secret && !formData.zoho_client_secret) delete payload.zoho_client_secret;
    if (!editSecrets.zoho_refresh_token && !formData.zoho_refresh_token) delete payload.zoho_refresh_token;

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
            <h2 className="modal-title" style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <Settings size={20} /> System Configuration
            </h2>
            <p className="modal-subtitle">
              Manage GitHub repositories, Jira credentials, and AI inference models.
            </p>
          </div>
          <button className="modal-close-btn" onClick={onClose} style={{ display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
            <X size={16} />
          </button>
        </div>

        {/* Navigation Tabs */}
        <div className="modal-tabs">
          <button
            type="button"
            className={`modal-tab-btn ${activeTab === "repos" ? "active" : ""}`}
            onClick={() => setActiveTab("repos")}
            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <GithubLogo size={16} weight="bold" /> GitHub Repos
          </button>
          <button
            type="button"
            className={`modal-tab-btn ${activeTab === "jira" ? "active" : ""}`}
            onClick={() => setActiveTab("jira")}
            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <Ticket size={16} /> Jira Cloud
          </button>
          <button
            type="button"
            className={`modal-tab-btn ${activeTab === "llm" ? "active" : ""}`}
            onClick={() => setActiveTab("llm")}
            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <Brain size={16} /> AI / LLM
          </button>
          <button
            type="button"
            className={`modal-tab-btn ${activeTab === "integrations" ? "active" : ""}`}
            onClick={() => setActiveTab("integrations")}
            style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <MessageSquare size={16} /> Slack Integration
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
              {saveSuccess && (
                <div className="callout callout-success" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <CheckCircle2 size={16} /> {saveSuccess}
                </div>
              )}
              {saveError && (
                <div className="callout callout-danger" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <XCircle size={16} /> {saveError}
                </div>
              )}

              {/* TAB: Repositories */}
              {activeTab === "repos" && (
                <div className="settings-section">
                  <div className="callout callout-info">
                    <strong style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <GithubLogo size={16} weight="bold" /> GitHub Ingestion:
                    </strong>{" "}
                    Connect your GitHub organization or specific repository URLs. Repositories are automatically cloned into the managed workspace for graph analysis and ticket tracing.
                  </div>

                  {/* Mode Toggle */}
                  <div className="github-mode-toggle" style={{ marginTop: "14px" }}>
                    <Button
                      type="button"
                      variant="segmented"
                      active={formData.github_source_type === "org"}
                      icon={Building2}
                      onClick={() => handleChange("github_source_type", "org")}
                    >
                      GitHub Organization / Account
                    </Button>
                    <Button
                      type="button"
                      variant="segmented"
                      active={formData.github_source_type === "urls"}
                      icon={Link2}
                      onClick={() => handleChange("github_source_type", "urls")}
                    >
                      Specific Repository URLs
                    </Button>
                  </div>

                  {formData.github_source_type === "org" ? (
                    <div className="form-group">
                      <label className="field-label">GitHub Organization or Username</label>
                      <input
                        type="text"
                        name="settings_gh_org_ident"
                        autoComplete="off"
                        data-lpignore="true"
                        className="field-input"
                        placeholder="e.g. AonamiTech or your-handle"
                        value={formData.github_org_or_user}
                        onChange={(e) => handleChange("github_org_or_user", e.target.value)}
                      />
                    </div>
                  ) : (
                    <div className="form-group">
                      <label className="field-label">GitHub Repository URLs or Slugs</label>
                      <textarea
                        name="settings_gh_repos_urls"
                        autoComplete="off"
                        data-lpignore="true"
                        className="field-input"
                        style={{ minHeight: "75px", resize: "vertical" }}
                        placeholder="e.g.&#10;https://github.com/AonamiTech/trail-main&#10;https://github.com/org/service"
                        value={formData.github_repo_urls}
                        onChange={(e) => handleChange("github_repo_urls", e.target.value)}
                      />
                    </div>
                  )}

                  <div className="form-group">
                    <label className="field-label">GitHub Personal Access Token (PAT)</label>
                    {meta.github_has_token && !editSecrets.github_token ? (
                      <div className="secret-saved-row">
                        <span className="secret-indicator" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                          <Lock size={12} /> Token configured in database (Masked)
                        </span>
                        <button
                          type="button"
                          className="btn-text-action"
                          onClick={() => setEditSecrets((p) => ({ ...p, github_token: true }))}
                        >
                          Change Token
                        </button>
                      </div>
                    ) : (
                      <input
                        type="password"
                        name="settings_gh_pat_token_field"
                        autoComplete="new-password"
                        data-lpignore="true"
                        className="field-input"
                        placeholder="ghp_... or github_pat_..."
                        value={formData.github_token}
                        onChange={(e) => handleChange("github_token", e.target.value)}
                      />
                    )}
                    <span className="field-hint">Optional for public repos. Required for private repos & rate limits.</span>
                  </div>

                  <div style={{ marginTop: "14px" }}>
                    <Button
                      type="button"
                      variant="primary"
                      onClick={handleValidateGithub}
                      loading={validatingRepo}
                      loadingText="Syncing Repositories from GitHub..."
                      icon={Search}
                      disabled={validatingRepo}
                    >
                      Sync Repositories from GitHub
                    </Button>
                  </div>

                  {repoValidation && (
                    <div
                      className={`validation-box ${
                        repoValidation.valid && repoValidation.repo_count > 0
                          ? "box-success"
                          : "box-danger"
                      }`}
                      style={{ marginTop: "14px" }}
                    >
                      {repoValidation.valid && repoValidation.repo_count > 0 ? (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <CheckCircle2 size={16} color="var(--ok)" /> Active Repositories ({repoValidation.repo_count})
                          </div>
                          <div className="github-repo-card-grid">
                            {repoValidation.repos.map((name) => (
                              <div key={name} className="github-repo-card">
                                <div className="github-repo-card-head">
                                  <span className="github-repo-card-name" title={name} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                                    <FolderGit2 size={14} /> {name}
                                  </span>
                                </div>
                                <div className="github-repo-card-badges">
                                  <span className="github-badge-status" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                                    <Check size={11} strokeWidth={2.5} /> Ready
                                  </span>
                                  <span className="github-badge-branch">main</span>
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                              <XCircle size={16} color="var(--danger)" /> GitHub Sync Error
                            </span>
                            <a
                              href="https://github.com/settings/tokens"
                              target="_blank"
                              rel="noreferrer"
                              style={{ fontSize: "11.5px", color: "var(--accent-strong)", fontWeight: 650, textDecoration: "underline", display: "inline-flex", alignItems: "center", gap: 4 }}
                            >
                              Open GitHub Tokens <ExternalLink size={12} />
                            </a>
                          </div>
                          <div className="box-sub" style={{ whiteSpace: "pre-wrap", lineHeight: 1.5, marginTop: 6, fontSize: "12.5px" }}>
                            {repoValidation.error}
                          </div>
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
                      Atlassian Security Settings{" "}
                      <ExternalLink size={12} style={{ display: "inline", verticalAlign: "-1px" }} />
                    </a>
                    .
                  </div>

                  <div className="form-grid-2" style={{ marginTop: "14px" }}>
                    <div className="form-group">
                      <label className="field-label">Jira Base URL</label>
                      <input
                        type="url"
                        name="settings_jira_base_url"
                        autoComplete="off"
                        data-lpignore="true"
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
                        name="settings_jira_account_email"
                        autoComplete="off"
                        data-lpignore="true"
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
                        <span className="secret-indicator" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                          <Lock size={12} /> Token configured in database (Masked)
                        </span>
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
                        name="settings_jira_api_token_field"
                        autoComplete="new-password"
                        data-lpignore="true"
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
                        name="settings_jira_proj_keys"
                        autoComplete="off"
                        data-lpignore="true"
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
                        name="settings_jira_excl_keys"
                        autoComplete="off"
                        data-lpignore="true"
                        className="field-input"
                        placeholder="e.g. ARCHIVE, TEST"
                        value={formData.jira_excluded_project_keys}
                        onChange={(e) => handleChange("jira_excluded_project_keys", e.target.value)}
                      />
                    </div>
                  </div>

                  <div style={{ marginTop: "12px" }}>
                    <Button
                      type="button"
                      variant="outline"
                      className="action-btn"
                      onClick={handleTestJira}
                      loading={testingJira}
                      loadingText="Testing..."
                      icon={Zap}
                      disabled={testingJira}
                    >
                      Test Jira Connection
                    </Button>
                  </div>

                  {jiraTestResult && (
                    <div
                      className={`validation-box ${jiraTestResult.success ? "box-success" : "box-danger"}`}
                      style={{ marginTop: "12px" }}
                    >
                      {jiraTestResult.success ? (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <CheckCircle2 size={16} color="var(--ok)" /> Jira Connection Active
                          </div>
                          <div className="box-sub">
                            Connected as: <strong>{jiraTestResult.display_name}</strong> ({jiraTestResult.email})
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <XCircle size={16} color="var(--danger)" /> Jira Connection Failed
                          </div>
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
                        <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <Zap size={14} /> Groq (Fast & Free)
                        </span>
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
                        <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <Bot size={14} /> OpenAI
                        </span>
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
                        <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <Sparkles size={14} /> Google Gemini
                        </span>
                      </div>
                    </label>

                    <label
                      className={`provider-card ${formData.llm_provider === "anthropic" ? "selected" : ""}`}
                      onClick={() => handleProviderPreset("anthropic")}
                    >
                      <input type="radio" checked={formData.llm_provider === "anthropic"} readOnly />
                      <div className="provider-info">
                        <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <Cpu size={14} /> Anthropic Claude
                        </span>
                      </div>
                    </label>

                    <label
                      className={`provider-card ${formData.llm_provider === "mock" ? "selected" : ""}`}
                      onClick={() => handleProviderPreset("mock")}
                    >
                      <input type="radio" checked={formData.llm_provider === "mock"} readOnly />
                      <div className="provider-info">
                        <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <FlaskConical size={14} /> Offline Mock
                        </span>
                      </div>
                    </label>
                  </div>

                  {formData.llm_provider === "anthropic" ? (
                    <>
                      <div className="form-group">
                        <label className="field-label">Anthropic API Key</label>
                        {meta.anthropic_has_key && !editSecrets.anthropic_api_key ? (
                          <div className="secret-saved-row">
                            <span className="secret-indicator" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                              <Lock size={12} /> Key configured in database (Masked)
                            </span>
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
                            name="settings_anthropic_api_key_field"
                            autoComplete="new-password"
                            data-lpignore="true"
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
                          name="settings_anthropic_model_field"
                          autoComplete="off"
                          data-lpignore="true"
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
                              <span className="secret-indicator" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                                <Lock size={12} /> Key configured in database (Masked)
                              </span>
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
                              name="settings_openai_api_key_field"
                              autoComplete="new-password"
                              data-lpignore="true"
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
                            name="settings_model_id_field"
                            autoComplete="off"
                            data-lpignore="true"
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
                              name="settings_base_url_field"
                              autoComplete="off"
                              data-lpignore="true"
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
                    <Button
                      type="button"
                      variant="outline"
                      className="action-btn"
                      onClick={handleTestLlm}
                      loading={testingLlm}
                      loadingText="Testing AI..."
                      icon={Bot}
                      disabled={testingLlm}
                    >
                      Test AI Connection
                    </Button>
                  </div>

                  {llmTestResult && (
                    <div
                      className={`validation-box ${llmTestResult.success ? "box-success" : "box-danger"}`}
                      style={{ marginTop: "12px" }}
                    >
                      {llmTestResult.success ? (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <CheckCircle2 size={16} color="var(--ok)" /> AI Inference Active
                          </div>
                          <div className="box-sub">
                            Model: <code>{llmTestResult.model}</code> | Response: "{llmTestResult.reply}"
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <XCircle size={16} color="var(--danger)" /> AI Test Failed
                          </div>
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
                      {meta.slack_has_token && !editSecrets.slack_bot_token ? (
                        <div className="secret-saved-row">
                          <span className="secret-indicator" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                            <Lock size={12} /> Token configured in database (Masked)
                          </span>
                          <button
                            type="button"
                            className="btn-text-action"
                            onClick={() => setEditSecrets((p) => ({ ...p, slack_bot_token: true }))}
                          >
                            Change Token
                          </button>
                        </div>
                      ) : (
                        <input
                          type="password"
                          name="settings_slack_bot_token_field"
                          autoComplete="new-password"
                          data-lpignore="true"
                          className="field-input"
                          placeholder="xoxb-..."
                          value={formData.slack_bot_token}
                          onChange={(e) => handleChange("slack_bot_token", e.target.value)}
                        />
                      )}
                    </div>
                    <div className="form-group">
                      <label className="field-label">Default Slack Channel ID</label>
                      <input
                        type="text"
                        name="settings_slack_channel_id_field"
                        autoComplete="off"
                        data-lpignore="true"
                        className="field-input"
                        placeholder="e.g. C0C3N8E9807"
                        value={formData.slack_channel_id}
                        onChange={(e) => handleChange("slack_channel_id", e.target.value)}
                      />
                    </div>
                  </div>

                  <div style={{ marginTop: "12px" }}>
                    <Button
                      type="button"
                      variant="outline"
                      className="action-btn"
                      onClick={handleTestSlack}
                      loading={testingSlack}
                      loadingText="Testing Slack..."
                      icon={MessageSquare}
                      disabled={testingSlack}
                    >
                      Test Slack Bot Ping
                    </Button>
                  </div>

                  {slackTestResult && (
                    <div
                      className={`validation-box ${slackTestResult.success ? "box-success" : "box-danger"}`}
                      style={{ marginTop: "12px" }}
                    >
                      {slackTestResult.success ? (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <CheckCircle2 size={16} color="var(--ok)" /> Slack Connection Verified!
                          </div>
                          <div className="box-sub">{slackTestResult.message}</div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <XCircle size={16} color="var(--danger)" /> Slack Test Failed
                          </div>
                          <div className="box-sub">{slackTestResult.error}</div>
                        </div>
                      )}
                    </div>
                  )}

                  <div className="callout callout-info" style={{ marginTop: "14px" }}>
                    <strong style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <MessageSquare size={16} /> Slack Bot Integration:
                    </strong>{" "}
                    Alerts, reviews, and autonomous approvals are dispatched directly to your Slack channel. Replying in threads allows interactive requirement refinement.
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Modal Footer */}
        <div className="modal-footer">
          <Button type="button" variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="primary"
            onClick={handleSave}
            loading={saving}
            loadingText="Saving Changes..."
            icon={Save}
          >
            Save Settings
          </Button>
        </div>
      </div>
    </div>
  );
}
