import { useState, useEffect } from "react";
import { apiFetch } from "../api";

export default function SetupWizard({ onComplete }) {
  const [step, setStep] = useState(1);
  const totalSteps = 4;

  // Form State
  const [formData, setFormData] = useState({
    repository_search_root: "",
    jira_base_url: "",
    jira_email: "",
    jira_api_token: "",
    jira_project_key: "",
    llm_provider: "groq",
    llm_model: "openai/gpt-oss-120b",
    openai_api_key: "",
    openai_base_url: "",
    anthropic_api_key: "",
    anthropic_model: "claude-3-5-sonnet-20241022",
    slack_bot_token: "",
    slack_channel_id: "",
  });

  // Validation / Test States
  const [repoValidation, setRepoValidation] = useState(null); // { valid, count, repos, message, error }
  const [validatingRepo, setValidatingRepo] = useState(false);

  const [jiraTestResult, setJiraTestResult] = useState(null); // { success, message, error, displayName }
  const [testingJira, setTestingJira] = useState(false);

  const [llmTestResult, setLlmTestResult] = useState(null); // { success, message, error, reply }
  const [testingLlm, setTestingLlm] = useState(false);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  // Load existing settings if available
  useEffect(() => {
    (async () => {
      try {
        const res = await apiFetch("/api/settings");
        if (res) {
          setFormData((prev) => ({
            ...prev,
            repository_search_root: res.repository_search_root || "",
            jira_base_url: res.jira_base_url || "",
            jira_email: res.jira_email || "",
            jira_project_key: res.jira_project_key || "",
            llm_provider: res.llm_provider || "groq",
            llm_model: res.llm_model || "openai/gpt-oss-120b",
            openai_base_url: res.openai_base_url || "",
            anthropic_model: res.anthropic_model || "claude-3-5-sonnet-20241022",
            slack_channel_id: res.slack_channel_id || "",
          }));
        }
      } catch {
        /* Ignore if unconfigured */
      }
    })();
  }, []);

  const handleChange = (field, value) => {
    setFormData((prev) => ({ ...prev, [field]: value }));
    setSaveError("");
  };

  // Provider presets helper
  const handleProviderChange = (provider) => {
    let defaultModel = "openai/gpt-oss-120b";
    let defaultBaseUrl = "";
    if (provider === "groq") {
      defaultModel = "openai/gpt-oss-120b";
      defaultBaseUrl = "";
    } else if (provider === "openai") {
      defaultModel = "gpt-4o-mini";
      defaultBaseUrl = "";
    } else if (provider === "gemini") {
      // Configured as OpenAI compatible endpoint
      provider = "openai";
      defaultBaseUrl = "https://generativelanguage.googleapis.com/v1beta/openai/";
      defaultModel = "gemini-2.5-flash";
    } else if (provider === "anthropic") {
      defaultModel = "claude-3-5-sonnet-20241022";
    } else if (provider === "mock") {
      defaultModel = "mock-agent";
    }

    setFormData((prev) => ({
      ...prev,
      llm_provider: provider,
      llm_model: defaultModel,
      openai_base_url: defaultBaseUrl,
    }));
    setLlmTestResult(null);
  };

  // 1. Validate Repo Path
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
      setRepoValidation({ valid: false, error: err.message, repo_count: 0, repos: [] });
    } finally {
      setValidatingRepo(false);
    }
  }

  // 2. Test Jira
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

  // 3. Test LLM
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

  // Dismiss Wizard
  async function handleDismiss() {
    try {
      localStorage.setItem("jira_ai_setup_dismissed", "true");
      await apiFetch("/api/settings/dismiss", { method: "POST" });
    } catch {
      /* ignore */
    }
    if (onComplete) {
      onComplete();
    }
  }

  // 4. Save and Finish
  async function handleSaveAndFinish() {
    setSaving(true);
    setSaveError("");
    try {
      await apiFetch("/api/settings", {
        method: "POST",
        body: { ...formData, setup_completed: "true" },
      });
      localStorage.setItem("jira_ai_setup_dismissed", "true");
      try {
        await apiFetch("/api/settings/dismiss", { method: "POST" });
      } catch {
        /* ignore */
      }
      if (onComplete) {
        onComplete();
      }
    } catch (err) {
      setSaveError(`Failed to save settings: ${err.message}`);
    } finally {
      setSaving(false);
    }
  }

  const isStep2Valid = Boolean(
    formData.jira_base_url.trim() &&
    formData.jira_email.trim() &&
    formData.jira_api_token.trim()
  );

  const isStep3Valid = (() => {
    if (formData.llm_provider === "anthropic") return Boolean(formData.anthropic_api_key.trim());
    if (formData.llm_provider === "openai" || formData.llm_provider === "groq" || formData.llm_provider === "gemini") {
      return Boolean(formData.openai_api_key.trim());
    }
    return true;
  })();

  const canContinue = (() => {
    if (step === 2) return isStep2Valid;
    if (step === 3) return isStep3Valid;
    return true;
  })();

  return (
    <div className="modal-backdrop" style={{ backdropFilter: "blur(6px)", zIndex: 9999 }}>
      <div className="wizard-card">
        {/* Wizard Header */}
        <div className="wizard-header">
          <div className="wizard-title-row" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <span className="wizard-badge" style={{ background: "rgba(99, 102, 241, 0.2)", color: "#818cf8", border: "1px solid rgba(99, 102, 241, 0.3)" }}>
                Mandatory Initial Setup
              </span>
              <h2 className="wizard-title">Connect Your Workspace</h2>
            </div>
            <div style={{ fontSize: "11.5px", color: "var(--muted)", fontWeight: 600, background: "var(--surface-2)", padding: "4px 10px", borderRadius: "12px" }}>
              Step {step} of {totalSteps}
            </div>
          </div>
          <p className="wizard-subtitle">
            Configure your development workspace, Jira integration, and AI provider to unlock the AI Governor dashboard.
          </p>

          {/* Stepper Indicator */}
          <div className="stepper-bar">
            <div className={`step-item ${step >= 1 ? "active" : ""} ${step > 1 ? "completed" : ""}`}>
              <div className="step-circle">{step > 1 ? "✓" : "1"}</div>
              <span className="step-label">Repositories</span>
            </div>
            <div className="step-line" />
            <div className={`step-item ${step >= 2 ? "active" : ""} ${step > 2 ? "completed" : ""}`}>
              <div className="step-circle">{step > 2 ? "✓" : "2"}</div>
              <span className="step-label">Jira Cloud</span>
            </div>
            <div className="step-line" />
            <div className={`step-item ${step >= 3 ? "active" : ""} ${step > 3 ? "completed" : ""}`}>
              <div className="step-circle">{step > 3 ? "✓" : "3"}</div>
              <span className="step-label">AI Engine</span>
            </div>
            <div className="step-line" />
            <div className={`step-item ${step >= 4 ? "active" : ""} ${step > 4 ? "completed" : ""}`}>
              <div className="step-circle">{step > 4 ? "✓" : "4"}</div>
              <span className="step-label">Finish</span>
            </div>
          </div>
        </div>

        {/* Wizard Body */}
        <div className="wizard-body">
          {saveError && <div className="callout callout-danger">{saveError}</div>}

          {/* STEP 1: Git Repositories */}
          {step === 1 && (
            <div className="wizard-step-content">
              <div className="step-intro">
                <span className="step-icon">📁</span>
                <div>
                  <h3 className="step-heading">Git Repositories Root Path</h3>
                  <p className="step-desc">
                    Specify the local folder containing your Git codebases. Jira AI scans this directory to extract code symbols, build the knowledge graph, and perform automated root cause analysis.
                  </p>
                </div>
              </div>

              <div className="callout callout-info">
                <strong>💡 Quick Tip:</strong> Enter the absolute directory path where your repositories reside (e.g. <code>C:\Users\YourName\Projects</code> on Windows or <code>/Users/name/projects</code> on Mac/Linux), or use <code>.</code> for current folder.
              </div>

              <div className="form-group" style={{ marginTop: "16px" }}>
                <label className="field-label">
                  Local Repository Directory Path <span className="req">*</span>
                </label>
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
                    {validatingRepo ? "Scanning..." : "🔍 Scan & Validate"}
                  </button>
                </div>
              </div>

              {/* Scan Results */}
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
                      <div className="box-title">⚠️ Directory exists, but 0 Git repositories found</div>
                      <div className="box-sub">
                        {repoValidation.message || "Make sure the folder contains subfolders with git (.git) repositories."}
                      </div>
                    </div>
                  ) : (
                    <div>
                      <div className="box-title">❌ Path Validation Failed</div>
                      <div className="box-sub">{repoValidation.error}</div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* STEP 2: Jira Cloud */}
          {step === 2 && (
            <div className="wizard-step-content">
              <div className="step-intro">
                <span className="step-icon">🎫</span>
                <div>
                  <h3 className="step-heading">Jira Cloud Integration</h3>
                  <p className="step-desc">
                    Connect to your Jira Cloud workspace to ingest issues, automate ticket triage, and generate regression test suites.
                  </p>
                </div>
              </div>

              <div className="callout callout-info">
                <strong>🔑 How to get your Jira API Token:</strong> Log in to your Atlassian account, go to{" "}
                <a
                  href="https://id.atlassian.com/manage-profile/security/api-tokens"
                  target="_blank"
                  rel="noreferrer"
                  className="link-highlight"
                >
                  Atlassian API Tokens ↗
                </a>
                , click <em>Create API token</em>, and paste it below.
              </div>

              <div className="form-grid-2" style={{ marginTop: "16px" }}>
                <div className="form-group">
                  <label className="field-label">
                    Jira Workspace URL <span className="req">*</span>
                  </label>
                  <input
                    type="url"
                    className="field-input"
                    placeholder="https://yourcompany.atlassian.net"
                    value={formData.jira_base_url}
                    onChange={(e) => handleChange("jira_base_url", e.target.value)}
                  />
                  <span className="field-hint">Your Atlassian domain URL</span>
                </div>

                <div className="form-group">
                  <label className="field-label">
                    Atlassian Email <span className="req">*</span>
                  </label>
                  <input
                    type="email"
                    className="field-input"
                    placeholder="name@company.com"
                    value={formData.jira_email}
                    onChange={(e) => handleChange("jira_email", e.target.value)}
                  />
                  <span className="field-hint">The email address tied to your Atlassian account</span>
                </div>
              </div>

              <div className="form-group">
                <label className="field-label">
                  Jira API Token <span className="req">*</span>
                </label>
                <input
                  type="password"
                  className="field-input"
                  placeholder="Paste your Jira API Token (ATATT...)"
                  value={formData.jira_api_token}
                  onChange={(e) => handleChange("jira_api_token", e.target.value)}
                />
              </div>

              <div className="form-group">
                <label className="field-label">Project Keys (Optional)</label>
                <input
                  type="text"
                  className="field-input"
                  placeholder="e.g. SCRUM, PROJ, BACKEND"
                  value={formData.jira_project_key}
                  onChange={(e) => handleChange("jira_project_key", e.target.value)}
                />
                <span className="field-hint">Comma-separated project keys to filter ticket ingestion</span>
              </div>

              <div style={{ marginTop: "12px", display: "flex", alignItems: "center", gap: "12px" }}>
                <button
                  type="button"
                  className="action-btn"
                  onClick={handleTestJira}
                  disabled={testingJira || !formData.jira_base_url || !formData.jira_email || !formData.jira_api_token}
                >
                  {testingJira ? "Connecting to Jira..." : "⚡ Test Jira Connection"}
                </button>
              </div>

              {jiraTestResult && (
                <div
                  className={`validation-box ${jiraTestResult.success ? "box-success" : "box-danger"}`}
                  style={{ marginTop: "12px" }}
                >
                  {jiraTestResult.success ? (
                    <div>
                      <div className="box-title">✅ Jira Connection Verified!</div>
                      <div className="box-sub">
                        Authenticated as: <strong>{jiraTestResult.display_name}</strong> ({jiraTestResult.email})
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

          {/* STEP 3: AI Provider */}
          {step === 3 && (
            <div className="wizard-step-content">
              <div className="step-intro">
                <span className="step-icon">🧠</span>
                <div>
                  <h3 className="step-heading">AI & LLM Provider</h3>
                  <p className="step-desc">
                    Choose the intelligence engine for root cause reasoning, knowledge graph queries, and test case generation.
                  </p>
                </div>
              </div>

              {/* Provider Selection Cards */}
              <div className="provider-selector">
                <label
                  className={`provider-card ${formData.llm_provider === "groq" ? "selected" : ""}`}
                  onClick={() => handleProviderChange("groq")}
                >
                  <input type="radio" name="provider" checked={formData.llm_provider === "groq"} readOnly />
                  <div className="provider-info">
                    <span className="provider-name">⚡ Groq (Recommended - Free & Fast)</span>
                    <span className="provider-desc">Ultra-fast inference with Llama 3 / GPT-OSS models.</span>
                  </div>
                </label>

                <label
                  className={`provider-card ${
                    formData.llm_provider === "openai" && !formData.openai_base_url.includes("google")
                      ? "selected"
                      : ""
                  }`}
                  onClick={() => handleProviderChange("openai")}
                >
                  <input
                    type="radio"
                    name="provider"
                    checked={formData.llm_provider === "openai" && !formData.openai_base_url.includes("google")}
                    readOnly
                  />
                  <div className="provider-info">
                    <span className="provider-name">🤖 OpenAI (Official)</span>
                    <span className="provider-desc">GPT-4o, GPT-4o-mini via OpenAI API.</span>
                  </div>
                </label>

                <label
                  className={`provider-card ${
                    formData.llm_provider === "openai" && formData.openai_base_url.includes("google")
                      ? "selected"
                      : ""
                  }`}
                  onClick={() => handleProviderChange("gemini")}
                >
                  <input
                    type="radio"
                    name="provider"
                    checked={formData.llm_provider === "openai" && formData.openai_base_url.includes("google")}
                    readOnly
                  />
                  <div className="provider-info">
                    <span className="provider-name">✨ Google Gemini (Free Tier)</span>
                    <span className="provider-desc">Gemini 2.5 Flash via AI Studio OpenAI compatibility.</span>
                  </div>
                </label>

                <label
                  className={`provider-card ${formData.llm_provider === "anthropic" ? "selected" : ""}`}
                  onClick={() => handleProviderChange("anthropic")}
                >
                  <input type="radio" name="provider" checked={formData.llm_provider === "anthropic"} readOnly />
                  <div className="provider-info">
                    <span className="provider-name">🔮 Anthropic Claude</span>
                    <span className="provider-desc">Claude 3.5 Sonnet / Claude 3.5 Haiku.</span>
                  </div>
                </label>

                <label
                  className={`provider-card ${formData.llm_provider === "mock" ? "selected" : ""}`}
                  onClick={() => handleProviderChange("mock")}
                >
                  <input type="radio" name="provider" checked={formData.llm_provider === "mock"} readOnly />
                  <div className="provider-info">
                    <span className="provider-name">🧪 Offline Mock (Zero Cost)</span>
                    <span className="provider-desc">Simulated responses without any external API keys.</span>
                  </div>
                </label>
              </div>

              {/* Provider Config Inputs */}
              {formData.llm_provider !== "mock" && (
                <div style={{ marginTop: "16px" }}>
                  {formData.llm_provider === "anthropic" ? (
                    <>
                      <div className="form-group">
                        <label className="field-label">
                          Anthropic API Key <span className="req">*</span>
                        </label>
                        <input
                          type="password"
                          className="field-input"
                          placeholder="sk-ant-api03-..."
                          value={formData.anthropic_api_key}
                          onChange={(e) => handleChange("anthropic_api_key", e.target.value)}
                        />
                      </div>
                      <div className="form-group">
                        <label className="field-label">Anthropic Model</label>
                        <input
                          type="text"
                          className="field-input"
                          placeholder="claude-3-5-sonnet-20241022"
                          value={formData.anthropic_model}
                          onChange={(e) => handleChange("anthropic_model", e.target.value)}
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="form-group">
                        <label className="field-label">
                          {formData.llm_provider === "groq"
                            ? "Groq API Key"
                            : formData.openai_base_url.includes("google")
                            ? "Google Gemini API Key"
                            : "OpenAI API Key"}{" "}
                          <span className="req">*</span>
                        </label>
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
                        {formData.llm_provider === "groq" && (
                          <span className="field-hint">
                            Get a free Groq key at{" "}
                            <a href="https://console.groq.com/keys" target="_blank" rel="noreferrer">
                              console.groq.com/keys ↗
                            </a>
                          </span>
                        )}
                        {formData.openai_base_url.includes("google") && (
                          <span className="field-hint">
                            Get a free Gemini API key at{" "}
                            <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">
                              aistudio.google.com ↗
                            </a>
                          </span>
                        )}
                      </div>

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
                      disabled={
                        testingLlm ||
                        (formData.llm_provider === "anthropic"
                          ? !formData.anthropic_api_key
                          : !formData.openai_api_key)
                      }
                    >
                      {testingLlm ? "Testing AI connection..." : "🤖 Test AI Model Ping"}
                    </button>
                  </div>

                  {llmTestResult && (
                    <div
                      className={`validation-box ${llmTestResult.success ? "box-success" : "box-danger"}`}
                      style={{ marginTop: "12px" }}
                    >
                      {llmTestResult.success ? (
                        <div>
                          <div className="box-title">✅ AI Provider Connected Successfully!</div>
                          <div className="box-sub">
                            Model: <code>{llmTestResult.model}</code> | Response: "{llmTestResult.reply}"
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title">❌ AI Connection Failed</div>
                          <div className="box-sub">{llmTestResult.error}</div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* STEP 4: Summary & Finish */}
          {step === 4 && (
            <div className="wizard-step-content">
              <div className="step-intro">
                <span className="step-icon">🚀</span>
                <div>
                  <h3 className="step-heading">Review & Complete Setup</h3>
                  <p className="step-desc">
                    Confirm your configuration. Once saved, your settings will be stored securely in the database and applied immediately.
                  </p>
                </div>
              </div>

              <div className="summary-card">
                <div className="summary-row">
                  <span className="summary-label">📁 Repositories Path:</span>
                  <span className="summary-value">
                    {formData.repository_search_root || "Default (current workspace)"}
                  </span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">🎫 Jira Cloud Workspace:</span>
                  <span className="summary-value">{formData.jira_base_url || "Not configured"}</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">📧 Jira Account:</span>
                  <span className="summary-value">{formData.jira_email || "Not configured"}</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">🧠 AI Provider:</span>
                  <span className="summary-value">
                    <strong>{formData.llm_provider.toUpperCase()}</strong> ({formData.llm_model})
                  </span>
                </div>
              </div>

              {/* Optional Slack section */}
              <div className="collapsible-section" style={{ marginTop: "16px" }}>
                <div className="section-title-sm">Optional: Slack Notifications</div>
                <div className="form-grid-2" style={{ marginTop: "8px" }}>
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
                    <label className="field-label">Slack Channel ID</label>
                    <input
                      type="text"
                      className="field-input"
                      placeholder="C1234567890"
                      value={formData.slack_channel_id}
                      onChange={(e) => handleChange("slack_channel_id", e.target.value)}
                    />
                  </div>
                </div>
              </div>

              <div className="callout callout-info" style={{ marginTop: "16px" }}>
                <strong>⚙️ Need to change these later?</strong> You can open the Settings menu anytime from the <strong>⚙️ Settings</strong> button in the top navigation bar.
              </div>
            </div>
          )}
        </div>

        {/* Wizard Footer Navigation */}
        <div className="wizard-footer">
          {step > 1 ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setStep((s) => s - 1)}
              disabled={saving}
            >
              ← Back
            </button>
          ) : (
            <div />
          )}

          {step < totalSteps ? (
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {!canContinue && (
                <span style={{ fontSize: "12px", color: "var(--danger, #f87171)", fontWeight: 600 }}>
                  {step === 2 && "⚠️ Fill Jira URL, Email, and Token to proceed"}
                  {step === 3 && `⚠️ Enter API Key for ${formData.llm_provider}`}
                </span>
              )}
              <button
                type="button"
                className="btn-primary"
                onClick={() => setStep((s) => s + 1)}
                disabled={!canContinue}
                style={{ opacity: canContinue ? 1 : 0.5, cursor: canContinue ? "pointer" : "not-allowed" }}
              >
                Continue to Step {step + 1} →
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="btn-primary-finish"
              onClick={handleSaveAndFinish}
              disabled={saving}
            >
              {saving ? "Saving Configuration..." : "✨ Save & Launch Dashboard"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
