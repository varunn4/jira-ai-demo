import { useState, useEffect } from "react";
import { apiFetch } from "../api";
import { GithubLogo } from "@phosphor-icons/react";
import {
  Check,
  Building2,
  Link2,
  ExternalLink,
  Eye,
  EyeOff,
  Loader2,
  Search,
  CheckCircle2,
  FolderGit2,
  XCircle,
  Ticket,
  KeyRound,
  Zap,
  Brain,
  Bot,
  Sparkles,
  Cpu,
  FlaskConical,
  Rocket,
  Mail,
  Settings,
  ArrowLeft,
  ArrowRight,
  AlertCircle,
} from "lucide-react";

export default function SetupWizard({ onComplete }) {
  const [step, setStep] = useState(1);
  const totalSteps = 4;

  // Form State
  const [formData, setFormData] = useState({
    github_source_type: "urls", // 'org' or 'urls'
    github_org_or_user: "",
    github_repo_urls: "",
    github_token: "",
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

  const [showToken, setShowToken] = useState(false);

  // Validation / Test States
  const [repoValidation, setRepoValidation] = useState(null); // { valid, repo_count, repos, cloned_details, message, error }
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
        const [settingsRes, statusRes] = await Promise.all([
          apiFetch("/api/settings").catch(() => null),
          apiFetch("/api/settings/status").catch(() => null),
        ]);

        if (settingsRes) {
          setFormData((prev) => ({
            ...prev,
            github_source_type: settingsRes.github_source_type || "urls",
            github_org_or_user: settingsRes.github_org_or_user || "",
            github_repo_urls: settingsRes.github_repo_urls || "",
            jira_base_url: settingsRes.jira_base_url || "",
            jira_email: settingsRes.jira_email || "",
            jira_project_key: settingsRes.jira_project_key || "",
            llm_provider: settingsRes.llm_provider || "groq",
            llm_model: settingsRes.llm_model || "openai/gpt-oss-120b",
            openai_base_url: settingsRes.openai_base_url || "",
            anthropic_model: settingsRes.anthropic_model || "claude-3-5-sonnet-20241022",
            slack_channel_id: settingsRes.slack_channel_id || "",
          }));
        }

        if (statusRes && statusRes.repositories_count > 0) {
          setRepoValidation({
            valid: true,
            repo_count: statusRes.repositories_count,
            repos: statusRes.repositories || [],
            message: `Found ${statusRes.repositories_count} repository(ies) ready in workspace.`,
          });
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

  // 1. Validate & Sync GitHub Repositories
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

  const isStep1Valid = Boolean(repoValidation && repoValidation.valid && repoValidation.repo_count > 0);

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
    if (step === 1) return isStep1Valid;
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
            Configure your GitHub repositories, Jira integration, and AI provider to unlock the AI Governor dashboard.
          </p>

          {/* Stepper Indicator */}
          <div className="stepper-bar">
            <div className={`step-item ${step >= 1 ? "active" : ""} ${step > 1 ? "completed" : ""}`}>
              <div className="step-circle">{step > 1 ? <Check size={14} strokeWidth={2.5} /> : "1"}</div>
              <span className="step-label">GitHub Repos</span>
            </div>
            <div className="step-line" />
            <div className={`step-item ${step >= 2 ? "active" : ""} ${step > 2 ? "completed" : ""}`}>
              <div className="step-circle">{step > 2 ? <Check size={14} strokeWidth={2.5} /> : "2"}</div>
              <span className="step-label">Jira Cloud</span>
            </div>
            <div className="step-line" />
            <div className={`step-item ${step >= 3 ? "active" : ""} ${step > 3 ? "completed" : ""}`}>
              <div className="step-circle">{step > 3 ? <Check size={14} strokeWidth={2.5} /> : "3"}</div>
              <span className="step-label">AI Engine</span>
            </div>
            <div className="step-line" />
            <div className={`step-item ${step >= 4 ? "active" : ""} ${step > 4 ? "completed" : ""}`}>
              <div className="step-circle">{step > 4 ? <Check size={14} strokeWidth={2.5} /> : "4"}</div>
              <span className="step-label">Finish</span>
            </div>
          </div>
        </div>

        {/* Wizard Body */}
        <div className="wizard-body">
          {saveError && <div className="callout callout-danger">{saveError}</div>}

          {/* STEP 1: GitHub Repositories Integration */}
          {step === 1 && (
            <div className="wizard-step-content">
              <div className="step-intro">
                <span className="step-icon">
                  <GithubLogo size={24} weight="bold" />
                </span>
                <div>
                  <h3 className="step-heading">GitHub Repositories Integration</h3>
                  <p className="step-desc">
                    Connect your GitHub organization, user account, or specific repository URLs. Repositories are synchronized directly to the server workspace for Knowledge Graph construction, ticket tracing, and AI Root Cause Analysis.
                  </p>
                </div>
              </div>

              {/* Source Mode Toggle */}
              <div className="github-mode-toggle">
                <button
                  type="button"
                  className={`github-mode-btn ${formData.github_source_type === "org" ? "active" : ""}`}
                  onClick={() => handleChange("github_source_type", "org")}
                  style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: "6px" }}
                >
                  <Building2 size={15} /> GitHub Organization / User
                </button>
                <button
                  type="button"
                  className={`github-mode-btn ${formData.github_source_type === "urls" ? "active" : ""}`}
                  onClick={() => handleChange("github_source_type", "urls")}
                  style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: "6px" }}
                >
                  <Link2 size={15} /> Specific Repository URLs
                </button>
              </div>

              {formData.github_source_type === "org" ? (
                <div className="form-group">
                  <label className="field-label">
                    GitHub Organization or Username <span className="req">*</span>
                  </label>
                  <input
                    type="text"
                    name="gh_org_ident_custom"
                    autoComplete="off"
                    data-lpignore="true"
                    className="field-input"
                    placeholder="e.g. AonamiTech or your-github-handle"
                    value={formData.github_org_or_user}
                    onChange={(e) => handleChange("github_org_or_user", e.target.value)}
                  />
                  <span className="field-hint">
                    All public and authorized repositories under this account will be automatically discovered and synchronized.
                  </span>
                </div>
              ) : (
                <div className="form-group">
                  <label className="field-label">
                    GitHub Repository URLs or Slugs <span className="req">*</span>
                  </label>
                  <textarea
                    name="gh_repos_list_custom"
                    autoComplete="off"
                    data-lpignore="true"
                    className="field-input"
                    style={{ minHeight: "80px", resize: "vertical", marginTop: "4px" }}
                    placeholder="e.g.&#10;https://github.com/AonamiTech/trail-main&#10;https://github.com/org/backend-service&#10;or org/repo"
                    value={formData.github_repo_urls}
                    onChange={(e) => handleChange("github_repo_urls", e.target.value)}
                  />
                  <span className="field-hint">
                    Enter one or more GitHub repository URLs (one per line or comma-separated).
                  </span>
                </div>
              )}

              {/* Personal Access Token (PAT) Input */}
              <div className="form-group" style={{ marginTop: "14px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <label className="field-label" style={{ marginBottom: 0 }}>
                    GitHub Personal Access Token (PAT) <span style={{ fontWeight: 400, color: "var(--muted)" }}>(Optional for public repos)</span>
                  </label>
                  <a
                    href="https://github.com/settings/tokens"
                    target="_blank"
                    rel="noreferrer"
                    style={{ fontSize: "11.5px", color: "var(--accent-strong)", fontWeight: 650, display: "inline-flex", alignItems: "center", gap: "4px" }}
                  >
                    Generate GitHub Token <ExternalLink size={12} />
                  </a>
                </div>
                <div className="input-with-action" style={{ marginTop: "4px" }}>
                  <input
                    type={showToken ? "text" : "password"}
                    name="gh_pat_token_secret"
                    autoComplete="new-password"
                    data-lpignore="true"
                    className="field-input"
                    placeholder="ghp_... or github_pat_..."
                    value={formData.github_token}
                    onChange={(e) => handleChange("github_token", e.target.value)}
                  />
                  <button
                    type="button"
                    className="action-btn"
                    onClick={() => setShowToken(!showToken)}
                    style={{ minWidth: "76px", display: "inline-flex", alignItems: "center", justifyContent: "center", gap: "5px" }}
                  >
                    {showToken ? (
                      <>
                        <EyeOff size={14} /> Hide
                      </>
                    ) : (
                      <>
                        <Eye size={14} /> Show
                      </>
                    )}
                  </button>
                </div>
                <span className="field-hint">
                  Required for <strong>private repositories</strong> and to prevent GitHub API rate limits. Token requires standard <code>repo</code> scope.
                </span>
              </div>

              {/* Action Button */}
              <div style={{ marginTop: "18px" }}>
                <button
                  type="button"
                  className="btn-primary"
                  style={{ width: "100%", minHeight: "42px", display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}
                  onClick={handleValidateGithub}
                  disabled={
                    validatingRepo ||
                    (formData.github_source_type === "org"
                      ? !formData.github_org_or_user.trim()
                      : !formData.github_repo_urls.trim())
                  }
                >
                  {validatingRepo ? (
                    <>
                      <Loader2 size={16} className="spin" /> Connecting & Syncing Repositories from GitHub...
                    </>
                  ) : (
                    <>
                      <Search size={16} /> Authenticate & Sync Repositories
                    </>
                  )}
                </button>
              </div>

              {/* Sync Results */}
              {repoValidation && (
                <div
                  className={`validation-box ${
                    repoValidation.valid && repoValidation.repo_count > 0
                      ? "box-success"
                      : "box-danger"
                  }`}
                  style={{ marginTop: "16px" }}
                >
                  {repoValidation.valid && repoValidation.repo_count > 0 ? (
                    <div>
                      <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <CheckCircle2 size={16} color="var(--success, #10b981)" /> Successfully Connected & Synced {repoValidation.repo_count} Repository(ies)
                      </div>
                      <div className="box-sub" style={{ marginBottom: "8px" }}>
                        {repoValidation.message || "All repositories are ready and available in the server workspace for graph analysis."}
                      </div>
                      <div className="github-repo-card-grid">
                        {repoValidation.repos.map((name) => (
                          <div key={name} className="github-repo-card">
                            <div className="github-repo-card-head">
                              <span className="github-repo-card-name" title={name} style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
                                <GithubLogo size={14} weight="bold" style={{ flexShrink: 0 }} /> {name}
                              </span>
                            </div>
                            <div className="github-repo-card-badges">
                              <span className="github-badge-status" style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
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
                          <XCircle size={16} color="var(--danger, #ef4444)" /> GitHub Connection / Sync Failed
                        </span>
                        <a
                          href="https://github.com/settings/tokens"
                          target="_blank"
                          rel="noreferrer"
                          style={{ fontSize: "11.5px", color: "var(--accent-strong)", fontWeight: 650, textDecoration: "underline", display: "inline-flex", alignItems: "center", gap: "4px" }}
                        >
                          Open GitHub Tokens <ExternalLink size={12} />
                        </a>
                      </div>
                      <div className="box-sub" style={{ whiteSpace: "pre-wrap", lineHeight: 1.5, marginTop: 6, fontSize: "12.5px" }}>
                        {repoValidation.error || "Unable to find or clone repositories from GitHub. Please check the organization name, repository URL, or provide a GitHub Personal Access Token."}
                      </div>
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
                <span className="step-icon">
                  <Ticket size={24} />
                </span>
                <div>
                  <h3 className="step-heading">Jira Cloud Integration</h3>
                  <p className="step-desc">
                    Connect to your Jira Cloud workspace to ingest issues, automate ticket triage, and generate regression test suites.
                  </p>
                </div>
              </div>

              <div className="callout callout-info">
                <strong style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                  <KeyRound size={15} /> How to get your Jira API Token:
                </strong>{" "}
                Log in to your Atlassian account, go to{" "}
                <a
                  href="https://id.atlassian.com/manage-profile/security/api-tokens"
                  target="_blank"
                  rel="noreferrer"
                  className="link-highlight"
                  style={{ display: "inline-flex", alignItems: "center", gap: 3 }}
                >
                  Atlassian API Tokens <ExternalLink size={11} />
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
                    name="jira_auth_user_email"
                    autoComplete="off"
                    data-lpignore="true"
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
                  name="jira_custom_api_secret"
                  autoComplete="new-password"
                  data-lpignore="true"
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
                  style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                >
                  {testingJira ? (
                    <>
                      <Loader2 size={15} className="spin" /> Connecting to Jira...
                    </>
                  ) : (
                    <>
                      <Zap size={15} /> Test Jira Connection
                    </>
                  )}
                </button>
              </div>

              {jiraTestResult && (
                <div
                  className={`validation-box ${jiraTestResult.success ? "box-success" : "box-danger"}`}
                  style={{ marginTop: "12px" }}
                >
                  {jiraTestResult.success ? (
                    <div>
                      <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <CheckCircle2 size={16} color="var(--success, #10b981)" /> Jira Connection Verified!
                      </div>
                      <div className="box-sub">
                        Authenticated as: <strong>{jiraTestResult.display_name}</strong> ({jiraTestResult.email})
                      </div>
                    </div>
                  ) : (
                    <div>
                      <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <XCircle size={16} color="var(--danger, #ef4444)" /> Jira Connection Failed
                      </div>
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
                <span className="step-icon">
                  <Brain size={24} />
                </span>
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
                    <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      <Zap size={15} /> Groq (Recommended - Free & Fast)
                    </span>
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
                    <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      <Bot size={15} /> OpenAI (Official)
                    </span>
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
                    <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      <Sparkles size={15} /> Google Gemini (Free Tier)
                    </span>
                    <span className="provider-desc">Gemini 2.5 Flash via AI Studio OpenAI compatibility.</span>
                  </div>
                </label>

                <label
                  className={`provider-card ${formData.llm_provider === "anthropic" ? "selected" : ""}`}
                  onClick={() => handleProviderChange("anthropic")}
                >
                  <input type="radio" name="provider" checked={formData.llm_provider === "anthropic"} readOnly />
                  <div className="provider-info">
                    <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      <Cpu size={15} /> Anthropic Claude
                    </span>
                    <span className="provider-desc">Claude 3.5 Sonnet / Claude 3.5 Haiku.</span>
                  </div>
                </label>

                <label
                  className={`provider-card ${formData.llm_provider === "mock" ? "selected" : ""}`}
                  onClick={() => handleProviderChange("mock")}
                >
                  <input type="radio" name="provider" checked={formData.llm_provider === "mock"} readOnly />
                  <div className="provider-info">
                    <span className="provider-name" style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      <FlaskConical size={15} /> Offline Mock (Zero Cost)
                    </span>
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
                          name="ai_anthropic_secret_key"
                          autoComplete="new-password"
                          data-lpignore="true"
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
                          name="ai_anthropic_model_name"
                          autoComplete="off"
                          data-lpignore="true"
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
                          name="ai_generic_secret_key"
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
                        {formData.llm_provider === "groq" && (
                          <span className="field-hint">
                            Get a free Groq key at{" "}
                            <a href="https://console.groq.com/keys" target="_blank" rel="noreferrer" style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                              console.groq.com/keys <ExternalLink size={11} />
                            </a>
                          </span>
                        )}
                        {formData.openai_base_url.includes("google") && (
                          <span className="field-hint">
                            Get a free Gemini API key at{" "}
                            <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer" style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                              aistudio.google.com <ExternalLink size={11} />
                            </a>
                          </span>
                        )}
                      </div>

                      <div className="form-grid-2">
                        <div className="form-group">
                          <label className="field-label">Model Identifier</label>
                          <input
                            type="text"
                            name="ai_generic_model_id"
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
                              name="ai_generic_base_url"
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
                      style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                    >
                      {testingLlm ? (
                        <>
                          <Loader2 size={15} className="spin" /> Testing AI connection...
                        </>
                      ) : (
                        <>
                          <Bot size={15} /> Test AI Model Ping
                        </>
                      )}
                    </button>
                  </div>

                  {llmTestResult && (
                    <div
                      className={`validation-box ${llmTestResult.success ? "box-success" : "box-danger"}`}
                      style={{ marginTop: "12px" }}
                    >
                      {llmTestResult.success ? (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <CheckCircle2 size={16} color="var(--success, #10b981)" /> AI Provider Connected Successfully!
                          </div>
                          <div className="box-sub">
                            Model: <code>{llmTestResult.model}</code> | Response: "{llmTestResult.reply}"
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="box-title" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <XCircle size={16} color="var(--danger, #ef4444)" /> AI Connection Failed
                          </div>
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
                <span className="step-icon">
                  <Rocket size={24} />
                </span>
                <div>
                  <h3 className="step-heading">Review & Complete Setup</h3>
                  <p className="step-desc">
                    Confirm your configuration. Once saved, your settings will be stored securely in the database and applied immediately.
                  </p>
                </div>
              </div>

              <div className="summary-card">
                <div className="summary-row">
                  <span className="summary-label" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <GithubLogo size={16} weight="bold" /> GitHub Source:
                  </span>
                  <span className="summary-value">
                    {formData.github_source_type === "org"
                      ? `Org / Account: ${formData.github_org_or_user || "Default"}`
                      : `Specific URLs (${formData.github_repo_urls ? formData.github_repo_urls.split('\n').filter(Boolean).length : 0} repos)`}
                  </span>
                </div>
                <div className="summary-row">
                  <span className="summary-label" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <FolderGit2 size={15} /> Synced Repositories:
                  </span>
                  <span className="summary-value">
                    {repoValidation?.repo_count || 0} repository(ies) active in workspace
                  </span>
                </div>
                <div className="summary-row">
                  <span className="summary-label" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Ticket size={15} /> Jira Cloud Workspace:
                  </span>
                  <span className="summary-value">{formData.jira_base_url || "Not configured"}</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Mail size={15} /> Jira Account:
                  </span>
                  <span className="summary-value">{formData.jira_email || "Not configured"}</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Brain size={15} /> AI Provider:
                  </span>
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
                      name="slack_bot_token_field"
                      autoComplete="new-password"
                      data-lpignore="true"
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
                      name="slack_channel_id_field"
                      autoComplete="off"
                      data-lpignore="true"
                      className="field-input"
                      placeholder="C1234567890"
                      value={formData.slack_channel_id}
                      onChange={(e) => handleChange("slack_channel_id", e.target.value)}
                    />
                  </div>
                </div>
              </div>

              <div className="callout callout-info" style={{ marginTop: "16px" }}>
                <strong style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                  <Settings size={15} /> Need to change these later?
                </strong>{" "}
                You can open the Settings menu anytime from the{" "}
                <strong style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  <Settings size={14} /> Settings
                </strong>{" "}
                button in the top navigation bar.
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
              style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
            >
              <ArrowLeft size={16} /> Back
            </button>
          ) : (
            <div />
          )}

          {step < totalSteps ? (
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {!canContinue && (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: "12px", color: "var(--danger, #f87171)", fontWeight: 600 }}>
                  <AlertCircle size={14} />
                  {step === 1 && "Authenticate & sync at least 1 GitHub repository"}
                  {step === 2 && "Fill Jira URL, Email, and Token to proceed"}
                  {step === 3 && `Enter API Key for ${formData.llm_provider}`}
                </span>
              )}
              <button
                type="button"
                className="btn-primary"
                onClick={() => setStep((s) => s + 1)}
                disabled={!canContinue}
                style={{ opacity: canContinue ? 1 : 0.5, cursor: canContinue ? "pointer" : "not-allowed", display: "inline-flex", alignItems: "center", gap: "6px" }}
              >
                Continue to Step {step + 1} <ArrowRight size={16} />
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="btn-primary-finish"
              onClick={handleSaveAndFinish}
              disabled={saving}
              style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}
            >
              {saving ? (
                <>
                  <Loader2 size={16} className="spin" /> Saving Configuration...
                </>
              ) : (
                <>
                  <Sparkles size={16} /> Save & Launch Dashboard
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
