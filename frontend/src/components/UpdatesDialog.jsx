import { useCallback, useEffect, useState } from "react";
import { getServerUpdates, linkPack, startUpdate } from "../api/servers.js";

const PHASES = {
  stopping: "Stopping the server",
  "backing-up": "Backing up",
  installing: "Installing the new version",
  testing: "Test-starting",
  "rolling-back": "Rolling back",
  passed: "Updated",
  "rolled-back": "Rolled back",
  failed: "Failed",
};
const ACTIVE = ["stopping", "backing-up", "installing", "testing", "rolling-back"];

function when(iso) {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
}

function versionLabel(v) {
  const bits = [v.name];
  if (v.minecraft_version) bits.push(`MC ${v.minecraft_version}`);
  if (v.release_type && v.release_type !== "release") bits.push(v.release_type);
  if (v.published) bits.push(new Date(v.published).toLocaleDateString());
  return bits.join(" · ");
}

function LinkForm({ server, link, onSaved }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      onSaved(await linkPack(server.id, url));
      setUrl("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="mcsm-section" onSubmit={save}>
      <div className="mcsm-small">
        {link?.project_id ? (
          <>
            Checked against{" "}
            {link.url ? (
              <a href={link.url} target="_blank" rel="noreferrer">
                {link.title || link.slug || link.url}
              </a>
            ) : (
              `CurseForge project ${link.project_id}`
            )}{" "}
            on CurseForge
            {link.guessed && <span className="mcsm-muted"> (found by the pack's name; paste the link if it's wrong)</span>}
            .
          </>
        ) : (
          "Paste this pack's CurseForge page link so the app can check it for new versions."
        )}
      </div>
      <div className="mcsm-import-link">
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://www.curseforge.com/minecraft/modpacks/..."
          style={{ flex: 1 }}
        />
        <button disabled={busy || !url.trim()}>{busy ? "Checking..." : link?.project_id ? "Change link" : "Save link"}</button>
      </div>
      {error && <div className="mcsm-error mcsm-small">{error}</div>}
    </form>
  );
}

function JobView({ job, live }) {
  const active = ACTIVE.includes(job.phase);
  const tone = job.phase === "passed" ? "mcsm-ok" : active ? "mcsm-muted" : "mcsm-error";
  const build = job.build;
  return (
    <div className="mcsm-section">
      <div className={`mcsm-small ${tone}`}>
        {active ? "⏳ " : job.phase === "passed" ? "✓ " : ""}
        <strong>{PHASES[job.phase] || job.phase}</strong>
        {": "}
        {job.message}
      </div>
      {live && build && active && <div className="mcsm-small mcsm-muted">{build.message}</div>}
      {build?.removed?.length > 0 && (
        <details>
          <summary className="mcsm-small">Mods removed while testing ({build.removed.length})</summary>
          <ul className="mcsm-removed mcsm-small">
            {build.removed.map((m) => (
              <li key={m.file}>
                <code>{m.file}</code> <span className="mcsm-muted">{m.detail}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {!live && (
        <div className="mcsm-muted mcsm-small">
          {job.from_version} → {job.to_version} · {when(job.finished_at || job.started_at)}
          {job.backup_id && <> · backup {job.backup_id}</>}
        </div>
      )}
    </div>
  );
}

/** Check one server for a newer pack version and update it (backup, test-start, roll back on failure). */
export default function UpdatesDialog({ server, onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const [choice, setChoice] = useState("");
  const [confirm, setConfirm] = useState(false);

  const load = useCallback(
    (refresh = false) => {
      if (refresh) setChecking(true);
      return getServerUpdates(server.id, refresh)
        .then((d) => {
          setData(d);
          setError("");
        })
        .catch((e) => setError(e.message))
        .finally(() => setChecking(false));
    },
    [server.id],
  );

  const running = data?.job && ACTIVE.includes(data.job.phase);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => load(), 2000);
    return () => clearInterval(t);
  }, [running, load]);
  const lastFinished = data?.history?.[0]?.finished_at;
  useEffect(() => {
    if (lastFinished) onChanged?.();
  }, [lastFinished, onChanged]);

  const check = data?.check;
  useEffect(() => {
    if (check?.latest) setChoice((c) => (check.newer.some((v) => v.id === c) ? c : check.latest.id));
  }, [check]);
  const chosen = check?.newer?.find((v) => v.id === choice);

  async function update() {
    setConfirm(false);
    setError("");
    try {
      await startUpdate(server.id, choice);
    } catch (e) {
      setError(e.message);
    }
    load();
  }

  return (
    <div className="mcsm-backdrop" onClick={onClose}>
      <div className="mcsm-dialog mcsm-backups" onClick={(e) => e.stopPropagation()}>
        <div className="mcsm-logs-head">
          <h2>{server.name} updates</h2>
          <button onClick={onClose}>Close</button>
        </div>
        <p className="mcsm-muted mcsm-small">
          Updating stops the server, backs up the whole server folder, installs the new pack version and test-starts
          it, removing client-only or crashing mods like the first build did. If it won't start, the backup is put
          back and the server is left exactly as it was.
        </p>

        {!data && !error && <p className="mcsm-muted">Checking...</p>}
        {error && <div className="mcsm-error mcsm-small">{error}</div>}

        {server.source === "cfimport" && data && <LinkForm server={server} link={data.link} onSaved={setData} />}

        {data?.job && <JobView job={data.job} live />}

        {check && !running && (
          <div className="mcsm-section">
            <div className="mcsm-section-head">
              <div className="mcsm-small">
                {check.current_name ? (
                  <>
                    Installed: <strong>{check.current_name}</strong>
                  </>
                ) : (
                  check.supported && !check.error && <span className="mcsm-muted">Installed version unknown.</span>
                )}
                {check.checked_at && <span className="mcsm-muted"> · checked {when(check.checked_at)}</span>}
              </div>
              <button disabled={checking} onClick={() => load(true)}>
                {checking ? "Checking..." : "Check again"}
              </button>
            </div>
            {!check.supported && <div className="mcsm-small mcsm-muted">{check.reason}</div>}
            {check.error && <div className="mcsm-small mcsm-error">{check.error}</div>}
            {check.supported && !check.error && !check.update_available && (
              <div className="mcsm-small mcsm-ok">✓ This is the newest version.</div>
            )}
            {check.update_available && (
              <>
                <label className="mcsm-small">
                  Update to
                  <select value={choice} onChange={(e) => setChoice(e.target.value)}>
                    {check.newer.map((v) => (
                      <option key={v.id} value={v.id}>
                        {versionLabel(v)}
                        {v.id === check.latest?.id ? " (recommended)" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                {chosen?.changes_minecraft && (
                  <div className="mcsm-small mcsm-error">
                    This version moves the server from Minecraft {server.minecraft_version} to{" "}
                    {chosen.minecraft_version}. The world is upgraded and can't be opened by the old version again
                    (the backup can).
                  </div>
                )}
                {!confirm ? (
                  <div className="mcsm-actions">
                    <button className="mcsm-primary" disabled={!chosen} onClick={() => setConfirm(true)}>
                      Update and test
                    </button>
                  </div>
                ) : (
                  <div className="mcsm-confirm">
                    <strong>Update to {chosen?.name}?</strong>
                    <p className="mcsm-small">
                      {["running", "starting"].includes(server.status)
                        ? "Players will be disconnected while the server is backed up and tested. "
                        : ""}
                      This can take several minutes for big packs.
                    </p>
                    <div className="mcsm-actions">
                      <button onClick={() => setConfirm(false)}>Cancel</button>
                      <button className="mcsm-primary" onClick={update}>
                        Update
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {data?.history?.length > 0 && (
          <>
            <h3 className="mcsm-subhead">Past updates</h3>
            {data.history.map((j, i) => (
              <JobView key={i} job={j} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}
