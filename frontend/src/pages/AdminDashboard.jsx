import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import { apiFetch } from "../api";
import { deriveStats, runningStatusMessage } from "../lib/graphJob";
import Header from "../components/Header.jsx";
import SetupWizard from "../components/SetupWizard.jsx";
import Repositories from "../tabs/Repositories.jsx";
import JiraTickets from "../tabs/JiraTickets.jsx";
import Logs from "../tabs/Logs.jsx";
import TestCases from "../tabs/TestCases.jsx";
import SimilarTickets from "../tabs/SimilarTickets.jsx";
import Workflows from "../tabs/Workflows.jsx";
import ChannelHealth from "../tabs/ChannelHealth.jsx";
import Utilization from "../tabs/Utilization.jsx";
import Neo4jGraph from "../tabs/Neo4jGraph.jsx";
import RCA from "../tabs/RCA.jsx";
import RingStudio from "../tabs/RingStudio.jsx";
import ZohoTickets from "../tabs/ZohoTickets.jsx";

// Tab registry — order matches the original UI. "docs" is intentionally absent:
// the Documentation portal is a separate route, not a tab here.
const TABS = [
  { key: "repos", label: "Repositories", Component: Repositories },
  { key: "jira", label: "Jira Tickets & Insights", Component: JiraTickets },
  { key: "logs", label: "Logs", Component: Logs },
  { key: "testcases", label: "Test Cases", Component: TestCases },
  { key: "similar", label: "Similar Tickets", Component: SimilarTickets },
  { key: "workflows", label: "Workflows", Component: Workflows },
  { key: "channels", label: "Channel Health", Component: ChannelHealth },
  { key: "utilization", label: "Utilization", Component: Utilization },
  { key: "neo4j", label: "Neo4j Graph", Component: Neo4jGraph },
  { key: "rca", label: "RCA", Component: RCA },
  { key: "rings", label: "Ring Studio", Component: RingStudio },
  { key: "zoho", label: "Zoho Tickets", Component: ZohoTickets },
];

const TAB_SLUG_MAP = {
  repos: "repos",
  repositories: "repos",
  repositries: "repos",
  jira: "jira",
  "jira-tickets": "jira",
  "jira-tickets-and-insights": "jira",
  logs: "logs",
  testcases: "testcases",
  "test-cases": "testcases",
  similar: "similar",
  "similar-tickets": "similar",
  workflows: "workflows",
  channels: "channels",
  "channel-health": "channels",
  utilization: "utilization",
  neo4j: "neo4j",
  "neo4j-graph": "neo4j",
  rca: "rca",
  rings: "rings",
  "ring-studio": "rings",
  zoho: "zoho",
  "zoho-tickets": "zoho",
};

const TAB_TO_PATH = {
  repos: "/dashboard/repositories",
  jira: "/dashboard/jira-tickets",
  logs: "/dashboard/logs",
  testcases: "/dashboard/test-cases",
  similar: "/dashboard/similar-tickets",
  workflows: "/dashboard/workflows",
  channels: "/dashboard/channel-health",
  utilization: "/dashboard/utilization",
  neo4j: "/dashboard/neo4j-graph",
  rca: "/dashboard/rca",
  rings: "/dashboard/ring-studio",
  zoho: "/dashboard/zoho-tickets",
};

const DEFAULT_OPTIONS = {
  pullLatestCode: true,
  fetchLatestJira: true,
  includeJira: true,
  buildEmbeddings: true,
  embeddingModel: "codebase_bge_m3",
  notes: "",
};

export default function AdminDashboard() {
  const { hasTab } = useAuth();
  const canRepos = hasTab("repos");
  const { tab: pathTab } = useParams();
  const navigate = useNavigate();

  const visibleTabs = useMemo(
    () => TABS.filter((t) => hasTab(t.key) || (t.key === "jira" && hasTab("insights"))),
    [hasTab],
  );

  const activeTab = useMemo(() => {
    if (pathTab) {
      const normalized = TAB_SLUG_MAP[pathTab.toLowerCase()] || pathTab.toLowerCase();
      if (visibleTabs.some((t) => t.key === normalized)) {
        return normalized;
      }
    }
    return visibleTabs[0]?.key || "repos";
  }, [pathTab, visibleTabs]);

  const handleTabChange = (key) => {
    const targetPath = TAB_TO_PATH[key] || `/dashboard/${key}`;
    navigate(targetPath);
  };

  // Graph-job + repository state (shared by the sidebar and Repositories tab).
  const [repos, setRepos] = useState([]);
  const [excluded, setExcluded] = useState([]);
  const [selected, setSelected] = useState(() => new Set());
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [job, setJob] = useState(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState({ msg: canRepos ? "Loading repositories..." : "", cls: "" });
  const [needsSetup, setNeedsSetup] = useState(false);
  const [codeReportGenerating, setCodeReportGenerating] = useState(false);
  // Bumped to force the live embeddings-status panel to refresh immediately
  // (on job start and on job completion).
  const [embedRefreshKey, setEmbedRefreshKey] = useState(0);
  const pollRef = useRef(null);

  const setOption = (key, value) => setOptions((o) => ({ ...o, [key]: value }));

  // Check setup status
  const checkSetupStatus = useCallback(async () => {
    if (!canRepos) return false;
    try {
      const res = await apiFetch("/api/settings/status");
      if (res && res.setup_complete === false) {
        setNeedsSetup(true);
        return true;
      }
      setNeedsSetup(false);
      return false;
    } catch {
      return false;
    }
  }, [canRepos]);

  // Load repositories and their freshly-computed activity scores. On mount we
  // select all repos; on a manual recalc we preserve the current selection.
  const loadRepositories = useCallback(
    async ({ keepSelection = false } = {}) => {
      const data = await apiFetch("/graph-admin/repositories");
      const list = data.repositories || [];
      setRepos(list);
      setExcluded(data.excluded_repositories || []);
      if (!keepSelection) {
        setSelected(new Set(list.filter((r) => (r.activity_score ?? 0) > 0).map((r) => r.name)));
      }
      return data.repository_count;
    },
    [],
  );

  // Load repositories and check onboarding setup on mount
  useEffect(() => {
    if (!canRepos) return;
    let active = true;
    (async () => {
      try {
        const setupRequired = await checkSetupStatus();
        if (setupRequired) {
          if (active) setStatus({ msg: "Setup configuration required to start", cls: "warning" });
          return;
        }
        const count = await loadRepositories();
        if (active) setStatus({ msg: `${count} repositories ready`, cls: "ok" });
      } catch (err) {
        if (active) setStatus({ msg: `Repository load failed: ${err.message}`, cls: "error" });
      }
    })();
    return () => {
      active = false;
    };
  }, [canRepos, checkSetupStatus, loadRepositories]);

  useEffect(() => () => pollRef.current && clearInterval(pollRef.current), []);

  function startPolling(jobId) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(() => pollJob(jobId), 2000);
  }

  async function pollJob(jobId) {
    try {
      const data = await apiFetch(`/graph-admin/jobs/${jobId}`);
      setJob(data);
      if (data.status === "running") {
        setStatus({ msg: runningStatusMessage(data, options.buildEmbeddings), cls: "running" });
      }
      if (data.status === "completed" || data.status === "failed") {
        clearInterval(pollRef.current);
        pollRef.current = null;
        setBusy(false);
        setEmbedRefreshKey((k) => k + 1);
        setStatus({
          msg:
            data.status === "completed"
              ? "Job completed successfully"
              : `Job failed: ${data.error || "unknown"}`,
          cls: data.status === "completed" ? "ok" : "error",
        });
      }
    } catch {
      /* ignore transient poll errors */
    }
  }

  async function trigger(action) {
    const sel = [...selected];
    if (action !== "jira_tickets_only" && sel.length === 0) {
      setStatus({ msg: "Select at least one repository", cls: "error" });
      return;
    }
    setBusy(true);
    setJob(null);
    setStatus({
      msg:
        action === "jira_tickets_only"
          ? "Starting Jira-only job..."
          : `Starting job for ${sel.length} selected repos...`,
      cls: "running",
    });
    try {
      const data = await apiFetch("/graph-admin/trigger", {
        method: "POST",
        body: {
          action,
          repositories: sel,
          pull_latest_code: options.pullLatestCode,
          fetch_latest_jira_tickets: options.fetchLatestJira,
          include_jira_tickets: options.includeJira,
          build_embeddings: options.buildEmbeddings,
          embedding_model: options.embeddingModel,
          notes: options.notes || null,
        },
      });
      setStatus({
        msg: `Job started (${String(data.job_id).slice(0, 8)}...) for ${data.repository_count || sel.length} repos`,
        cls: "running",
      });
      setJob(data);
      setEmbedRefreshKey((k) => k + 1);
      startPolling(data.job_id);
    } catch (err) {
      setStatus({ msg: err.message, cls: "error" });
      setBusy(false);
    }
  }

  const stats = job ? deriveStats(job, options.buildEmbeddings) : null;
  const ActiveComponent = visibleTabs.find((t) => t.key === activeTab)?.Component;

  return (
    <>
      <Header
        onSettingsSaved={async () => {
          await loadRepositories();
          await checkSetupStatus();
        }}
      />
      {needsSetup && (
        <SetupWizard
          onComplete={async () => {
            setNeedsSetup(false);
            const count = await loadRepositories();
            setStatus({ msg: `${count} repositories ready`, cls: "ok" });
          }}
        />
      )}
      <main className="dash full-width-layout">
        <section className="dash-main">
          {visibleTabs.length === 0 ? (
            <EmptyState hasDocs={hasTab("docs")} />
          ) : (
            <>
              <div className="tab-bar-container">
                <nav className="tab-nav">
                  {visibleTabs.map((t) => (
                    <button
                      key={t.key}
                      className={`tab-btn${activeTab === t.key ? " active" : ""}`}
                      onClick={() => handleTabChange(t.key)}
                    >
                      {t.label}
                    </button>
                  ))}
                </nav>
                {status.msg && (
                  <div className={`status-pill ${status.cls}`}>{status.msg}</div>
                )}
              </div>

              {ActiveComponent && (
                <div className="tab-pane-wrapper">
                  <ActiveComponent
                    repos={repos}
                    excluded={excluded}
                    selected={selected}
                    setSelected={setSelected}
                    reloadRepos={loadRepositories}
                    embeddingModel={options.embeddingModel}
                    setStatus={setStatus}
                    downloading={codeReportGenerating}
                    setDownloading={setCodeReportGenerating}
                    // Controls passed to Repositories tab
                    options={options}
                    setOption={setOption}
                    trigger={trigger}
                    busy={busy}
                    job={job}
                    stats={stats}
                    embedRefreshKey={embedRefreshKey}
                  />
                </div>
              )}
            </>
          )}
        </section>
      </main>
    </>
  );
}

function EmptyState({ hasDocs }) {
  return (
    <div style={{ padding: "40px 0", color: "var(--muted)" }}>
      <p>Your role does not have access to any dashboard tabs.</p>
      {hasDocs && (
        <p>
          You can open the <Link to="/docs-portal">Documentation portal</Link>.
        </p>
      )}
    </div>
  );
}
