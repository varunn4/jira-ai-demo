import { useState, useEffect } from "react";
import { apiFetch } from "../api";
import {
  X,
  Plus,
  GitBranch,
  SpinnerGap,
  CheckCircle,
  WarningCircle,
  ArrowSquareOut,
} from "@phosphor-icons/react";

export default function CreateTicketModal({ isOpen, onClose, onTicketCreated }) {
  const [loadingContext, setLoadingContext] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [context, setContext] = useState({
    connected_repositories: [],
    projects: [{ key: "SCRUM", name: "Scrum Project" }],
    users: [],
    jira_configured: false,
  });

  const [projectKey, setProjectKey] = useState("SCRUM");
  const [issueType, setIssueType] = useState("Task");
  const [summary, setSummary] = useState("");
  const [description, setDescription] = useState(
    "### Acceptance Criteria:\n- [ ] Scenario 1: \n- [ ] Scenario 2: \n\n### Technical Context:\n"
  );
  const [assigneeId, setAssigneeId] = useState("");
  const [priority, setPriority] = useState("Medium");
  const [repoMode, setRepoMode] = useState("connected"); // "connected" | "custom"
  const [selectedRepo, setSelectedRepo] = useState("");
  const [customRepoUrl, setCustomRepoUrl] = useState("");
  const [customPat, setCustomPat] = useState("");

  const [error, setError] = useState("");
  const [success, setSuccess] = useState(null);

  useEffect(() => {
    if (isOpen) {
      setError("");
      setSuccess(null);
      setLoadingContext(true);
      apiFetch("/jira/create-context")
        .then((data) => {
          setContext(data);
          if (data.projects && data.projects.length > 0) {
            setProjectKey(data.projects[0].key);
          }
          if (data.connected_repositories && data.connected_repositories.length > 0) {
            setSelectedRepo(data.connected_repositories[0]);
          }
        })
        .catch((err) => {
          setError(err.message || "Failed to load project context");
        })
        .finally(() => setLoadingContext(false));
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!summary.trim()) {
      setError("Please enter a ticket summary");
      return;
    }

    let repoUrl = "";
    if (repoMode === "connected" && selectedRepo) {
      repoUrl = selectedRepo;
    } else if (repoMode === "custom" && customRepoUrl.trim()) {
      repoUrl = customRepoUrl.trim();
    }

    setSubmitting(true);
    setError("");
    setSuccess(null);

    try {
      const res = await apiFetch("/jira/create-ticket", {
        method: "POST",
        body: {
          project_key: projectKey,
          summary: summary.trim(),
          description: description.trim(),
          issue_type: issueType,
          assignee_id: assigneeId || null,
          priority: priority,
          github_repo_url: repoUrl || null,
          github_pat: customPat.trim() || null,
        },
      });

      setSuccess(res);
      if (onTicketCreated) {
        onTicketCreated(res);
      }
      setTimeout(() => {
        onClose();
      }, 2000);
    } catch (err) {
      setError(err.message || "Failed to create ticket");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(15, 23, 42, 0.6)",
        backdropFilter: "blur(4px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
        padding: "20px",
      }}
    >
      <div
        style={{
          background: "var(--card, #ffffff)",
          border: "1px solid var(--line, #e2e8f0)",
          borderRadius: "12px",
          width: "100%",
          maxWidth: "640px",
          maxHeight: "90vh",
          overflowY: "auto",
          boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)",
          padding: "24px 28px",
        }}
      >
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "20px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <div
              style={{
                width: "36px",
                height: "36px",
                borderRadius: "8px",
                background: "rgba(37, 99, 235, 0.1)",
                color: "#2563eb",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <Plus size={20} weight="bold" />
            </div>
            <div>
              <h3 style={{ margin: 0, fontSize: "17px", fontWeight: "700", color: "var(--ink, #0f172a)" }}>
                Create Jira Engineering Ticket
              </h3>
              <p style={{ margin: 0, fontSize: "12.5px", color: "var(--muted, #64748b)" }}>
                AI Governor will automatically validate requirements and bind repository.
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              color: "var(--muted, #64748b)",
              padding: "4px",
              width: "auto",
            }}
          >
            <X size={20} />
          </button>
        </div>

        {error && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "10px 14px",
              background: "rgba(239, 68, 68, 0.08)",
              border: "1px solid rgba(239, 68, 68, 0.2)",
              borderRadius: "6px",
              color: "#b91c1c",
              fontSize: "13px",
              marginBottom: "16px",
            }}
          >
            <WarningCircle size={16} weight="fill" />
            <span>{error}</span>
          </div>
        )}

        {success && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "10px 14px",
              background: "rgba(16, 185, 129, 0.08)",
              border: "1px solid rgba(16, 185, 129, 0.2)",
              borderRadius: "6px",
              color: "#047857",
              fontSize: "13px",
              marginBottom: "16px",
            }}
          >
            <CheckCircle size={16} weight="fill" />
            <span>
              Ticket <strong>{success.key}</strong> created successfully!
            </span>
            {success.url && (
              <a
                href={success.url}
                target="_blank"
                rel="noreferrer"
                style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: "4px", color: "#2563eb", fontWeight: "600" }}
              >
                Open in Jira <ArrowSquareOut size={13} />
              </a>
            )}
          </div>
        )}

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
          {/* Project & Issue Type */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "4px" }}>
                Jira Project *
              </label>
              <select
                value={projectKey}
                onChange={(e) => setProjectKey(e.target.value)}
                style={{ width: "100%", height: "38px", padding: "0 10px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px" }}
              >
                {context.projects.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.name} ({p.key})
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "4px" }}>
                Issue Type *
              </label>
              <select
                value={issueType}
                onChange={(e) => setIssueType(e.target.value)}
                style={{ width: "100%", height: "38px", padding: "0 10px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px" }}
              >
                <option value="Task">Task</option>
                <option value="Story">Story</option>
                <option value="Bug">Bug</option>
              </select>
            </div>
          </div>

          {/* Summary */}
          <div>
            <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "4px" }}>
              Summary *
            </label>
            <input
              type="text"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="e.g. Implement OAuth2 Social Login with Google & GitHub"
              style={{ width: "100%", height: "38px", padding: "0 12px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px", boxSizing: "border-box" }}
            />
          </div>

          {/* Description */}
          <div>
            <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "4px" }}>
              Description &amp; Acceptance Criteria *
            </label>
            <textarea
              rows={4}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              style={{ width: "100%", padding: "10px 12px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px", boxSizing: "border-box", fontFamily: "inherit" }}
            />
          </div>

          {/* Assignee & Priority */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "4px" }}>
                Assignee *
              </label>
              <select
                value={assigneeId}
                onChange={(e) => setAssigneeId(e.target.value)}
                style={{ width: "100%", height: "38px", padding: "0 10px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px" }}
              >
                <option value="">-- Select Assignee --</option>
                {context.users.map((u) => (
                  <option key={u.accountId} value={u.accountId}>
                    {u.displayName || u.email}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label style={{ display: "block", fontSize: "12px", fontWeight: "600", color: "var(--ink-soft, #334155)", marginBottom: "4px" }}>
                Priority *
              </label>
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                style={{ width: "100%", height: "38px", padding: "0 10px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px" }}
              >
                <option value="Highest">P0 - Highest</option>
                <option value="High">P1 - High</option>
                <option value="Medium">P2 - Medium</option>
                <option value="Low">P3 - Low</option>
                <option value="Lowest">P4 - Lowest</option>
              </select>
            </div>
          </div>

          {/* Repository Selection */}
          <div style={{ background: "var(--surface-2, #f8fafc)", padding: "14px", borderRadius: "8px", border: "1px solid var(--line, #e2e8f0)" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "8px" }}>
              <label style={{ fontSize: "12.5px", fontWeight: "700", color: "var(--ink, #0f172a)", display: "flex", alignItems: "center", gap: "6px" }}>
                <GitBranch size={16} style={{ color: "#2563eb" }} />
                <span>Linked GitHub Repository *</span>
              </label>
              <div style={{ display: "flex", gap: "10px", fontSize: "12px" }}>
                <label style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: "4px" }}>
                  <input
                    type="radio"
                    name="repoMode"
                    checked={repoMode === "connected"}
                    onChange={() => setRepoMode("connected")}
                  />
                  <span>Connected Repos</span>
                </label>
                <label style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: "4px" }}>
                  <input
                    type="radio"
                    name="repoMode"
                    checked={repoMode === "custom"}
                    onChange={() => setRepoMode("custom")}
                  />
                  <span>Custom Repo / PAT</span>
                </label>
              </div>
            </div>

            {repoMode === "connected" ? (
              <select
                value={selectedRepo}
                onChange={(e) => setSelectedRepo(e.target.value)}
                style={{ width: "100%", height: "38px", padding: "0 10px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px", background: "#fff" }}
              >
                {context.connected_repositories.length === 0 ? (
                  <option value="">No connected repos in workspace</option>
                ) : (
                  context.connected_repositories.map((r) => (
                    <option key={r} value={r}>
                      {r} (Connected Workspace Repo)
                    </option>
                  ))
                )}
              </select>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                <input
                  type="text"
                  value={customRepoUrl}
                  onChange={(e) => setCustomRepoUrl(e.target.value)}
                  placeholder="https://github.com/your-org/your-repo"
                  style={{ width: "100%", height: "38px", padding: "0 10px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px", background: "#fff", boxSizing: "border-box" }}
                />
                <input
                  type="password"
                  value={customPat}
                  onChange={(e) => setCustomPat(e.target.value)}
                  placeholder="Personal Access Token (PAT) for private repo"
                  style={{ width: "100%", height: "38px", padding: "0 10px", borderRadius: "6px", border: "1px solid var(--line-strong, #cbd5e1)", fontSize: "13px", background: "#fff", boxSizing: "border-box" }}
                />
              </div>
            )}
          </div>

          {/* Action Buttons */}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "10px" }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                width: "auto",
                height: "38px",
                padding: "0 16px",
                borderRadius: "6px",
                border: "1px solid var(--line-strong, #cbd5e1)",
                background: "#fff",
                color: "var(--ink-soft, #334155)",
                fontWeight: "600",
                fontSize: "13px",
                cursor: "pointer",
              }}
            >
              Cancel
            </button>

            <button
              type="submit"
              disabled={submitting || loadingContext}
              style={{
                width: "auto",
                height: "38px",
                padding: "0 22px",
                borderRadius: "6px",
                border: "none",
                background: "var(--accent-grad-strong, #2563eb)",
                color: "#fff",
                fontWeight: "600",
                fontSize: "13px",
                cursor: submitting ? "not-allowed" : "pointer",
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                boxShadow: "0 1px 3px rgba(37,99,235,0.25)",
              }}
            >
              {submitting ? (
                <>
                  <SpinnerGap size={16} className="animate-spin" />
                  <span>Creating in Jira…</span>
                </>
              ) : (
                <>
                  <Plus size={16} weight="bold" />
                  <span>Create Ticket</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
