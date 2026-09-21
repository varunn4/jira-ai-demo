import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { fmtDate } from "../lib/format";

// Slack channel health: discover every channel ID the digests post to
// (role map + assignee DMs + env-pinned channels), show who's behind each ID,
// and probe each with a test message — flagging the ones the bot can't post to
// (e.g. not_in_channel). Probe results are persisted server-side, so the last
// known status shows immediately on load; "Run health check" refreshes them.
export default function ChannelHealth() {
  const [channels, setChannels] = useState([]);
  const [configured, setConfigured] = useState(true);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState({}); // channel_id -> fresh probe result

  async function loadChannels() {
    setError("");
    setLoading(true);
    try {
      const res = await apiFetch("/graph-admin/channel-health/channels");
      setChannels(res.channels || []);
      setConfigured(res.configured);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadChannels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runCheck(channelIds) {
    setError("");
    setChecking(true);
    try {
      const res = await apiFetch("/graph-admin/channel-health/check", {
        method: "POST",
        body: channelIds ? { channel_ids: channelIds } : {},
      });
      // Merge fresh results by channel_id (a targeted retest updates one row).
      setResults((prev) => {
        const next = { ...prev };
        (res.results || []).forEach((r) => {
          next[r.channel_id] = r;
        });
        return next;
      });
      // Reload so persisted last_checked_at / status stay in sync.
      loadChannels();
    } catch (err) {
      setError(err.message);
    } finally {
      setChecking(false);
    }
  }

  // Effective status for a row: a fresh probe from this session wins; otherwise
  // fall back to the persisted last_status from the server.
  function rowStatus(c) {
    const fresh = results[c.channel_id];
    if (fresh) {
      return { ok: fresh.ok, error: fresh.error, checkedAt: null, live: true };
    }
    if (c.last_status) {
      return {
        ok: c.last_status === "ok",
        error: c.last_error,
        checkedAt: c.last_checked_at,
        live: false,
      };
    }
    return null;
  }

  const total = channels.length;
  const probed = channels.filter((c) => rowStatus(c)).length;
  const failed = channels.filter((c) => {
    const s = rowStatus(c);
    return s && !s.ok;
  }).length;

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 14,
          flexWrap: "wrap",
        }}
      >
        <span style={{ fontSize: 13, color: "var(--muted)" }}>
          Sends a test message to every Slack channel/DM the digests post to and
          flags the ones the bot can’t reach (e.g. <code>not_in_channel</code>).
          Last result is remembered across reloads.
        </span>
        <button
          style={{ width: "auto", minHeight: "unset", padding: "6px 16px", fontSize: 13 }}
          onClick={() => runCheck(null)}
          disabled={checking || loading}
        >
          {checking ? "Checking…" : "Run health check"}
        </button>
        <button
          className="secondary"
          style={{ width: "auto", minHeight: "unset", padding: "6px 12px", fontSize: 13 }}
          onClick={loadChannels}
          disabled={loading || checking}
        >
          Reload channels
        </button>
      </div>

      {!configured && (
        <div style={{ color: "var(--muted)", fontSize: 14, padding: "12px 0" }}>
          <code>SLACK_BOT_TOKEN</code> is not configured — discovery works but
          probes will all fail until a bot token (<code>xoxb-…</code>) with{" "}
          <code>chat:write</code> is set in the server environment.
        </div>
      )}

      <div className="stats-grid" style={{ marginBottom: 14 }}>
        <Stat value={total} label="Channels" />
        <Stat value={probed} label="Probed" />
        <Stat value={failed} label="Failed" danger={failed > 0} />
      </div>

      <table>
        <thead>
          <tr>
            <th style={{ width: "18%" }}>Channel ID</th>
            <th style={{ width: "20%" }}>Name</th>
            <th style={{ width: "22%" }}>Sources</th>
            <th style={{ width: "9%" }}>Status</th>
            <th style={{ width: "15%" }}>Error</th>
            <th style={{ width: "10%" }}>Last checked</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {channels.length === 0 ? (
            <tr>
              <td colSpan={7} style={{ color: "var(--muted)" }}>
                {loading ? "Loading…" : "No channel IDs discovered."}
              </td>
            </tr>
          ) : (
            channels.map((c) => {
              const s = rowStatus(c);
              const names = c.names || [];
              const emails = c.emails || [];
              return (
                <tr key={c.channel_id}>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>{c.channel_id}</td>
                  <td style={{ fontSize: 12 }}>
                    {names.length ? (
                      <span title={emails.join(", ")}>{names.join(", ")}</span>
                    ) : (
                      <span style={{ color: "var(--muted)" }}>—</span>
                    )}
                  </td>
                  <td style={{ fontSize: 12, color: "var(--muted)" }}>
                    {(c.sources || []).join(", ")}
                  </td>
                  <td>
                    {!s ? (
                      <span className="badge">—</span>
                    ) : s.ok ? (
                      <span className="badge ok">OK</span>
                    ) : (
                      <span className="badge err">Failed</span>
                    )}
                  </td>
                  <td
                    style={{
                      fontSize: 12,
                      color: s && !s.ok ? "var(--danger)" : "var(--muted)",
                      fontFamily: s && !s.ok ? "monospace" : undefined,
                    }}
                  >
                    {s && !s.ok ? s.error : "—"}
                  </td>
                  <td style={{ fontSize: 12, color: "var(--muted)" }}>
                    {s && s.checkedAt ? fmtDate(s.checkedAt) : s && s.live ? "just now" : "—"}
                  </td>
                  <td>
                    <button
                      className="secondary"
                      style={{
                        width: "auto",
                        minHeight: "unset",
                        padding: "4px 10px",
                        fontSize: 12,
                      }}
                      onClick={() => runCheck([c.channel_id])}
                      disabled={checking}
                    >
                      Retest
                    </button>
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>

      {error && (
        <div style={{ color: "var(--danger)", fontSize: 13, marginTop: 8 }}>{error}</div>
      )}
    </div>
  );
}

function Stat({ value, label, danger }) {
  return (
    <div className="stat-card">
      <div className="stat-value" style={danger ? { color: "var(--danger)" } : undefined}>
        {value}
      </div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
