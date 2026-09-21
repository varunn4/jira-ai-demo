import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";
import { fmtDate, fmtDuration, fmtRelative, pct } from "../lib/format";

// Maps a per-collection health string to a status dot class + label.
const HEALTH = {
  ok: { cls: "ok", text: "Healthy" },
  empty: { cls: "warn", text: "Empty" },
  missing: { cls: "warn", text: "Not built" },
  unreachable: { cls: "err", text: "Unreachable" },
  unknown: { cls: "idle", text: "Unknown" },
};

// Live-updating health panel for every embedding collection. Polls fast while a
// build job is writing embeddings, slowly otherwise. `refreshKey` lets the
// parent force an immediate refresh (e.g. right after triggering a job).
export default function EmbeddingsStatus({ refreshKey = 0 }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [building, setBuilding] = useState(false);
  const [buildMsg, setBuildMsg] = useState("");
  const timerRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch("/graph-admin/embeddings/status");
      setData(res);
      setError("");
      return res;
    } catch (err) {
      setError(err.message);
      return null;
    }
  }, []);

  const buildTestcases = useCallback(async () => {
    setBuilding(true);
    setBuildMsg("");
    try {
      const res = await apiFetch("/graph-admin/testcase-embeddings/build", {
        method: "POST",
      });
      setBuildMsg(
        res.already_running
          ? "A build is already running…"
          : "Build started — embedding test cases…",
      );
      await load(); // flips the panel into fast-poll while the job runs
    } catch (err) {
      setBuildMsg(`Failed: ${err.message}`);
    } finally {
      setBuilding(false);
    }
  }, [load]);

  useEffect(() => {
    let active = true;
    async function tick() {
      const res = await load();
      if (!active) return;
      // Poll every 3s while embeddings are being written, else every 20s.
      const delay = res && res.updating ? 3000 : 20000;
      timerRef.current = setTimeout(tick, delay);
    }
    tick();
    return () => {
      active = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [load, refreshKey]);

  const collections = data?.collections || [];
  const overallCls =
    !data || !data.reachable ? "err" : data.overall === "ok" ? "ok" : "warn";
  const totalPoints = collections.reduce((sum, c) => sum + (c.points || 0), 0);

  return (
    <div className="embed-panel">
      <button
        type="button"
        className="embed-head"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className={`embed-dot ${data?.updating ? "run" : overallCls}`} />
        <span className="embed-title">Embeddings</span>
        {data?.updating ? (
          <span className="embed-syncing">syncing…</span>
        ) : data ? (
          <span className="embed-total">{totalPoints.toLocaleString()} vectors</span>
        ) : null}
        <span className="embed-chevron">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="embed-body">
          {error && <div className="embed-error">{error}</div>}
          {!data && !error && <div className="embed-empty">Loading…</div>}

          {data && !data.reachable && (
            <div className="embed-error">
              Qdrant unreachable{data.error ? `: ${data.error}` : ""}
            </div>
          )}

          {collections.map((c) => (
            <EmbedRow key={c.key} c={c} />
          ))}

          {data && (
            <div className="embed-actions">
              <button type="button" className="embed-refresh" onClick={load}>
                Refresh now
              </button>
              <button
                type="button"
                className="embed-build"
                onClick={buildTestcases}
                disabled={building || data.updating}
                title="Embed all generated test cases so Workflow 1 can flag regressions"
              >
                {building ? "Starting…" : "Build test-case embeddings"}
              </button>
            </div>
          )}
          {buildMsg && <div className="embed-buildmsg">{buildMsg}</div>}
        </div>
      )}
    </div>
  );
}

function EmbedRow({ c }) {
  const h = HEALTH[c.health] || HEALTH.unknown;
  const p = c.progress;
  const title = `${c.key}\nLast updated: ${fmtDate(c.last_updated)}${
    c.vector_dim ? `\nVector size: ${c.vector_dim}` : ""
  }`;

  return (
    <div className={`embed-row${c.updating ? " is-updating" : ""}`} title={title}>
      <span className={`embed-dot ${c.updating ? "run" : h.cls}`} />
      <div className="embed-row-main">
        <div className="embed-row-top">
          <span className="embed-label">{c.label}</span>
          <span className="embed-count">
            {c.points != null ? c.points.toLocaleString() : "—"}
          </span>
        </div>

        {c.updating && p ? (
          <>
            <div className="embed-bar">
              <div
                className="embed-bar-fill"
                style={{ width: `${p.percent ?? pct(p.done, p.total)}%` }}
              />
            </div>
            <div className="embed-row-sub">
              <span className="embed-accent">
                {p.total ? `${p.done}/${p.total} ${p.unit}` : "indexing…"}
              </span>
              {p.eta_seconds ? (
                <span>
                  · ~{fmtDuration(p.eta_seconds)} left{p.eta_scope === "repo" ? " (repo)" : ""}
                </span>
              ) : null}
            </div>
            {p.detail ? <div className="embed-detail">{p.detail}</div> : null}
          </>
        ) : (
          <div className="embed-row-sub">
            <span>{h.text}</span>
            {c.vector_dim ? <span>· {c.vector_dim}-dim</span> : null}
            {c.last_updated ? <span>· updated {fmtRelative(c.last_updated)}</span> : null}
          </div>
        )}
      </div>
    </div>
  );
}
