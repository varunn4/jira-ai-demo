import { formatEta, pct } from "./format";

// Port of the original updateStats() — derives display values from a job record.
export function deriveStats(data, buildEmbeddings) {
  const totals = data.totals || {};
  const progress = data.progress || {};

  const repoT = totals.repositories || 0;
  const repoD = progress.repositories_done || 0;
  const jiraT = totals.jira_tickets || 0;
  const jiraD = progress.jira_tickets_done || 0;
  const embT = totals.embedding_documents || 0;
  const embD = progress.embedding_documents_done || 0;
  const jiraEmbT = totals.jira_embedding_documents || 0;
  const jiraEmbD = progress.jira_embedding_documents_done || 0;
  const codeEmbT = totals.codebase_embedding_documents || 0;
  const codeEmbD = progress.codebase_embedding_documents_done || 0;
  const repoEta = progress.repositories_eta_seconds || 0;
  const jiraEta = progress.jira_embedding_eta_seconds || 0;
  const codeEta = progress.codebase_embedding_eta_seconds || 0;
  const embEta = progress.embedding_eta_seconds || jiraEta || codeEta || 0;

  const embActive = embT > 0 || embD > 0;
  let activeEmbD = embD;
  let activeEmbT = embT;
  let activeEmbEta = embEta;
  let activeEmbKind = "";
  if (jiraEmbT > 0 && jiraEmbD < jiraEmbT) {
    activeEmbD = jiraEmbD;
    activeEmbT = jiraEmbT;
    activeEmbEta = jiraEta;
    activeEmbKind = "Jira";
  } else if (codeEmbT > 0 && codeEmbD < codeEmbT) {
    activeEmbD = codeEmbD;
    activeEmbT = codeEmbT;
    activeEmbEta = codeEta;
    activeEmbKind = "Codebase";
  } else if (codeEmbT > 0) {
    activeEmbD = codeEmbD;
    activeEmbT = codeEmbT;
    activeEmbEta = codeEta;
    activeEmbKind = "Codebase";
  } else if (jiraEmbT > 0) {
    activeEmbD = jiraEmbD;
    activeEmbT = jiraEmbT;
    activeEmbEta = jiraEta;
    activeEmbKind = "Jira";
  }

  const inferEmbeddingActive =
    !embActive && data.status === "running" && buildEmbeddings && repoT > 0 && repoD >= repoT;

  const statRepos =
    repoT > 0 ? `${repoD}/${repoT}` : data.repository_count != null ? `${data.repository_count}` : "0/0";
  const statJira = jiraT > 0 ? `${jiraD}/${jiraT}` : "0/0";
  const statEmbeddings = embActive
    ? `${activeEmbD}/${activeEmbT}${activeEmbKind ? ` ${activeEmbKind}` : ""}`
    : inferEmbeddingActive
      ? "running"
      : "—";

  let embeddingLabel = "Embedding progress";
  if (activeEmbKind === "Jira") embeddingLabel = "Jira embedding progress";
  else if (activeEmbKind === "Codebase") embeddingLabel = "Codebase embedding progress";
  if ((embActive && data.status === "running" && activeEmbT > 0 && activeEmbD === 0) || inferEmbeddingActive) {
    embeddingLabel += " (model running)";
  }

  const embeddingStarting =
    data.status === "running" && buildEmbeddings && activeEmbT > 0 && activeEmbD === 0;
  const embeddingIndeterminate = inferEmbeddingActive || embeddingStarting;
  const aggregateSuffix = activeEmbKind && embT > activeEmbT ? ` · Overall ${embD}/${embT}` : "";

  return {
    statStatus: data.status || "—",
    statAction: data.action || "—",
    statRepos,
    statJira,
    statEmbeddings,
    repoPct: pct(repoD, repoT),
    jiraPct: pct(jiraD, jiraT),
    repoEtaText: formatEta(repoEta),
    jiraEtaText: formatEta(jiraEta),
    showEmbeddingSection: embActive || inferEmbeddingActive,
    embeddingLabel,
    embeddingIndeterminate,
    embeddingPct: pct(activeEmbD, activeEmbT),
    embeddingEtaText: `${formatEta(activeEmbEta)}${aggregateSuffix}`,
  };
}

export function runningStatusMessage(data, buildEmbeddings) {
  const totals = data.totals || {};
  const progress = data.progress || {};
  const repoDone = (totals.repositories || 0) > 0 && progress.repositories_done >= totals.repositories;
  const jiraDone = (totals.jira_tickets || 0) > 0 && progress.jira_tickets_done >= totals.jira_tickets;
  const embTotal = totals.embedding_documents || 0;
  const embDone = progress.embedding_documents_done || 0;
  const jiraEmbTotal = totals.jira_embedding_documents || 0;
  const jiraEmbDone = progress.jira_embedding_documents_done || 0;
  const codeEmbTotal = totals.codebase_embedding_documents || 0;
  const codeEmbDone = progress.codebase_embedding_documents_done || 0;
  if (jiraEmbTotal > 0 && jiraEmbDone < jiraEmbTotal) return "Building Jira embeddings...";
  if (codeEmbTotal > 0 && codeEmbDone < codeEmbTotal) return "Building codebase embeddings...";
  if (embTotal > 0 && embDone < embTotal) return "Building embeddings...";
  if (embTotal === 0 && buildEmbeddings && repoDone) return "Building codebase embeddings...";
  if (repoDone && jiraDone) return "Finalizing Qdrant update...";
  return "Qdrant update running...";
}
