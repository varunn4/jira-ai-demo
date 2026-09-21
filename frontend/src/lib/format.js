export function fmtDate(val) {
  if (!val) return "—";
  try {
    return new Date(val).toLocaleString();
  } catch {
    return val;
  }
}

export function formatEta(seconds) {
  seconds = Number(seconds || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `ETA ${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (minutes < 60) return `ETA ${minutes}m ${secs}s`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `ETA ${hours}h ${mins}m`;
}

export function pct(done, total) {
  return total > 0 ? Math.round((Number(done) / Number(total)) * 100) : 0;
}

// Compact duration ("45s", "8m", "1h 20m") for ETAs / elapsed times.
export function fmtDuration(seconds) {
  seconds = Number(seconds || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${hrs}h ${rem}m` : `${hrs}h`;
}

// Compact "time ago" for last-updated timestamps ("just now", "3m ago", ...).
export function fmtRelative(val) {
  if (!val) return "never";
  const then = new Date(val).getTime();
  if (!Number.isFinite(then)) return "—";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return days < 30 ? `${days}d ago` : new Date(then).toLocaleDateString();
}
