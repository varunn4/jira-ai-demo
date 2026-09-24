export const DASHBOARD_TAB_KEYS = ["repos", "jira", "insights", "logs", "testcases", "similar", "workflows", "channels", "utilization", "neo4j", "rca" /*, "rings", "zoho" */];

export function hasAnyDashboardTab(hasTab) {
  return DASHBOARD_TAB_KEYS.some((t) => hasTab(t));
}

