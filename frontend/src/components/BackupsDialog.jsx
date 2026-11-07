import { useCallback, useEffect, useState } from "react";
import {
  backupDownloadUrl,
  createBackup,
  deleteBackup,
  listBackups,
  restoreBackup,
  saveBackupSchedule,
} from "../api/servers.js";

const INTERVALS = [
  [30, "30 minutes"],
  [60, "1 hour"],
  [120, "2 hours"],
  [180, "3 hours"],
  [360, "6 hours"],
  [720, "12 hours"],
  [1440, "1 day"],
  [10080, "1 week"],
];

const KIND_LABELS = { manual: "Manual", scheduled: "Scheduled", "pre-restore": "Before restore" };

export function formatBytes(n) {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

function formatWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function ScheduleForm({ server, schedule, nextAt, onSaved }) {
  const [form, setForm] = useState(schedule);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);
  useEffect(() => setForm(schedule), [schedule]);

  const set = (k) => (e) => {
    const v = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    setForm((f) => ({ ...f, [k]: ["interval_minutes", "keep", "keep_days"].includes(k) ? Number(v) : v }));
    setStatus(null);
  };
  const dirty = JSON.stringify(form) !== JSON.stringify(schedule);

  async function save() {
    setBusy(true);
    try {
      await saveBackupSchedule(server.id, form);
      setStatus({ ok: true, text: "Saved." });
      onSaved();
    } catch (e) {
      setStatus({ ok: false, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mcsm-backup-schedule">
      <label className="mcsm-check">
        <input type="checkbox" checked={form.enabled} onChange={set("enabled")} />
        Back up automatically
      </label>
      <div className="mcsm-row">
        <label>
          Every
          <select value={form.interval_minutes} onChange={set("interval_minutes")} disabled={!form.enabled}>
            {INTERVALS.map(([m, label]) => (
              <option key={m} value={m}>
                {label}
              </option>
            ))}
            {!INTERVALS.some(([m]) => m === form.interval_minutes) && (
              <option value={form.interval_minutes}>{form.interval_minutes} minutes</option>
            )}
          </select>
        </label>
        <label>
          What to save
          <select value={form.scope} onChange={set("scope")}>
            <option value="world">World only</option>
            <option value="full">Whole server folder</option>
          </select>
        </label>
      </div>
      <div className="mcsm-row">
        <label>
          Keep newest
          <input type="number" min="1" max="500" value={form.keep} onChange={set("keep")} />
        </label>
        <label>
          Delete after (days, 0 = never)
          <input type="number" min="0" max="3650" value={form.keep_days} onChange={set("keep_days")} />
        </label>
      </div>
      <div className="mcsm-muted mcsm-small">
        Retention applies to automatic backups only. Manual backups stay until you delete them. A stopped server
        that hasn't changed since its last backup is skipped.
        {schedule.enabled && nextAt && <> Next backup: {formatWhen(nextAt)}.</>}
      </div>
      <div className="mcsm-actions">
        {status && <span className={status.ok ? "mcsm-ok mcsm-small" : "mcsm-error mcsm-small"}>{status.text}</span>}
        <button className="mcsm-primary" disabled={busy || !dirty} onClick={save}>
          {busy ? "Saving..." : "Save schedule"}
        </button>
      </div>
    </div>
  );
}

function ConfirmRestore({ server, backup, onCancel, onConfirm }) {
  return (
    <div className="mcsm-confirm">
      <strong>Restore {formatWhen(backup.created_at)}?</strong>
      <p className="mcsm-small">
        {server.status === "running" || server.status === "starting"
          ? "The server will be stopped, the "
          : "The "}
        {backup.scope === "full" ? "server files" : "world"} will be replaced with this backup
        {server.status === "running" || server.status === "starting" ? ", and the server started again." : "."} A
        safety backup of the current {backup.scope === "full" ? "files" : "world"} is taken first.
      </p>
      <div className="mcsm-actions">
        <button onClick={onCancel}>Cancel</button>
        <button className="mcsm-danger" onClick={onConfirm}>
          Restore
        </button>
      </div>
    </div>
  );
}

export default function BackupsDialog({ server, onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [scope, setScope] = useState("");
  const [confirming, setConfirming] = useState(null);

  const load = useCallback(
    () =>
      listBackups(server.id)
        .then((d) => {
          setData(d);
          setError("");
          return d;
        })
        .catch((e) => setError(e.message)),
    [server.id],
  );

  // Poll quickly while a backup or restore runs, slowly otherwise.
  const running = Boolean(data?.job);
  useEffect(() => {
    load();
    const timer = setInterval(load, running ? 1500 : 10000);
    return () => clearInterval(timer);
  }, [load, running]);

  const finishedAt = data?.last_result?.finished_at;
  useEffect(() => {
    if (finishedAt) onChanged?.();
  }, [finishedAt, onChanged]);

  async function act(fn) {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    }
    load();
  }

  const job = data?.job;
  const result = data?.last_result;

  return (
    <div className="mcsm-backdrop" onClick={onClose}>
      <div className="mcsm-dialog mcsm-backups" onClick={(e) => e.stopPropagation()}>
        <div className="mcsm-logs-head">
          <h2>{server.name} backups</h2>
          <button onClick={onClose}>Close</button>
        </div>

        <div className="mcsm-backup-now">
          <select value={scope} onChange={(e) => setScope(e.target.value)} disabled={running}>
            <option value="">{data?.schedule.scope === "full" ? "Whole server folder" : "World only"} (default)</option>
            <option value="world">World only</option>
            <option value="full">Whole server folder</option>
          </select>
          <button className="mcsm-primary" disabled={running || !data} onClick={() => act(() => createBackup(server.id, scope))}>
            {job?.action === "backup" ? "Backing up..." : "Back up now"}
          </button>
        </div>

        {job && <div className="mcsm-small mcsm-muted">⏳ {job.message}</div>}
        {!job && result && (
          <div className={`mcsm-small ${result.ok ? "mcsm-ok" : "mcsm-error"}`}>
            {result.message} <span className="mcsm-muted">({formatWhen(result.finished_at)})</span>
          </div>
        )}
        {error && <div className="mcsm-error mcsm-small">{error}</div>}

        {data && (
          <>
            <h3 className="mcsm-subhead">Saved backups ({data.backups.length})</h3>
            {data.backups.length === 0 ? (
              <p className="mcsm-muted mcsm-small">No backups yet.</p>
            ) : (
              <ul className="mcsm-backup-list">
                {data.backups.map((b) => (
                  <li key={b.id}>
                    <div className="mcsm-backup-row">
                      <div>
                        <strong>{formatWhen(b.created_at)}</strong>{" "}
                        <span className="mcsm-badge">{KIND_LABELS[b.kind] || b.kind}</span>{" "}
                        {b.scope === "full" && <span className="mcsm-badge">Whole folder</span>}
                        <div className="mcsm-muted mcsm-small">
                          {formatBytes(b.size_bytes)}
                          {!b.consistent && " · taken while saving couldn't be paused"}
                        </div>
                      </div>
                      <div className="mcsm-card-actions">
                        <a className="mcsm-button" href={backupDownloadUrl(server.id, b.id)} download>
                          Download
                        </a>
                        <button disabled={running} onClick={() => setConfirming(b)}>
                          Restore
                        </button>
                        <button
                          className="mcsm-danger"
                          disabled={running}
                          onClick={() => {
                            if (window.confirm(`Delete the backup from ${formatWhen(b.created_at)}?`))
                              act(() => deleteBackup(server.id, b.id));
                          }}
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                    {confirming?.id === b.id && (
                      <ConfirmRestore
                        server={server}
                        backup={b}
                        onCancel={() => setConfirming(null)}
                        onConfirm={() => {
                          setConfirming(null);
                          act(() => restoreBackup(server.id, b.id));
                        }}
                      />
                    )}
                  </li>
                ))}
              </ul>
            )}

            <h3 className="mcsm-subhead">Schedule</h3>
            <ScheduleForm server={server} schedule={data.schedule} nextAt={data.next_scheduled_at} onSaved={load} />
            <div className="mcsm-muted mcsm-small">
              Stored in <code>{data.backup_dir}</code>. Change the folder on the Settings page. Backups are kept when a
              server is deleted.
            </div>
          </>
        )}
      </div>
    </div>
  );
}
