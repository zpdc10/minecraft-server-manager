import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteServer,
  getLogs,
  listServers,
  recreateServer,
  restartServer,
  startServer,
  stopServer,
} from "../api/servers.js";
import BackupsDialog from "../components/BackupsDialog.jsx";
import CreateServerDialog from "../components/CreateServerDialog.jsx";
import ServerManage from "../components/ServerManage.jsx";
import "../components/servers.css";
import { api } from "../api.js";

const STATUS_LABELS = {
  pulling: "Downloading image",
  creating: "Creating",
  installing: "Installing pack",
  testing: "Testing",
  starting: "Starting",
  running: "Running",
  stopping: "Stopping",
  stopped: "Stopped",
  error: "Error",
  missing: "Container missing",
  unknown: "Unknown",
};

function StatusBadge({ status }) {
  return <span className={`mcsm-badge mcsm-${status}`}>{STATUS_LABELS[status] || status}</span>;
}

const REASONS = {
  client_only: "Client-only",
  known: "Known client-only",
  missing_dependency: "Missing dependency",
  crash: "Crashed the server",
};

function BuildSummary({ build }) {
  if (!build) return null;
  const active = ["installing", "scanning", "testing"].includes(build.phase);
  const tone = build.phase === "failed" ? "mcsm-error" : build.phase === "passed" ? "mcsm-ok" : "mcsm-muted";
  return (
    <div className="mcsm-build mcsm-small">
      <div className={tone}>
        {active && "⏳ "}
        {build.phase === "passed" && "✓ "}
        {build.message}
      </div>
      {build.notes?.length > 0 && (
        <details>
          <summary>Pack download notes</summary>
          <ul className="mcsm-removed">
            {build.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </details>
      )}
      {build.removed.length > 0 && (
        <details>
          <summary>
            Removed {build.removed.length} mod{build.removed.length === 1 ? "" : "s"}
          </summary>
          <ul className="mcsm-removed">
            {build.removed.map((m) => (
              <li key={m.file}>
                <strong>{m.name || m.mod_ids[0] || m.file}</strong>{" "}
                <span className="mcsm-badge">{REASONS[m.reason] || m.reason}</span>
                <div className="mcsm-muted">
                  <code>{m.file}</code>
                  {m.detail && ` · ${m.detail}`}
                </div>
              </li>
            ))}
          </ul>
          <div className="mcsm-muted">
            Removed jars are kept in <code>.mcsm/removed-mods</code> in the server folder.
          </div>
        </details>
      )}
    </div>
  );
}

function LogsPanel({ server, onClose }) {
  const [text, setText] = useState("Loading...");
  const preRef = useRef(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      getLogs(server.id)
        .then((t) => alive && setText(t || "(no output yet)"))
        .catch((e) => alive && setText(`Could not load logs: ${e.message}`));
    load();
    const timer = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [server.id]);

  useEffect(() => {
    const el = preRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div className="mcsm-backdrop" onClick={onClose}>
      <div className="mcsm-dialog mcsm-logs" onClick={(e) => e.stopPropagation()}>
        <div className="mcsm-logs-head">
          <h2>{server.name} logs</h2>
          <button onClick={onClose}>Close</button>
        </div>
        <pre ref={preRef}>{text}</pre>
      </div>
    </div>
  );
}

function DeleteDialog({ server, onClose, onDeleted }) {
  const [deleteData, setDeleteData] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function confirm() {
    setBusy(true);
    try {
      await deleteServer(server.id, deleteData);
      onDeleted();
      onClose();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

  return (
    <div className="mcsm-backdrop" onClick={onClose}>
      <div className="mcsm-dialog" onClick={(e) => e.stopPropagation()}>
        <h2>
          {server.status === "missing" ? "Remove" : "Delete"} {server.name}?
        </h2>
        <p>
          {server.status === "missing"
            ? "This removes the server from the list. Its files stay where they are unless you tick the box."
            : "This stops and removes the server container."}
        </p>
        <label className="mcsm-check">
          <input type="checkbox" checked={deleteData} onChange={(e) => setDeleteData(e.target.checked)} />
          Also delete the world and all server files in <code>{server.data_path}</code>
        </label>
        {error && <p className="mcsm-error">{error}</p>}
        <div className="mcsm-actions">
          <button onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="mcsm-danger" onClick={confirm} disabled={busy}>
            {busy ? "Deleting..." : deleteData ? "Delete server and files" : "Delete server"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Servers() {
  const [servers, setServers] = useState(null);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState(null);
  const [logsFor, setLogsFor] = useState(null);
  const [manageId, setManageId] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const [backupsFor, setBackupsFor] = useState(null);
  const [creatingVanilla, setCreatingVanilla] = useState(false);
  const [publicRoutes, setPublicRoutes] = useState({});

  const refresh = useCallback(() => {
    listServers()
      .then((s) => {
        setServers(s);
        setError("");
      })
      .catch((e) => setError(e.message));
    api
      .publicRoutes()
      .then((routes) => setPublicRoutes(Object.fromEntries(routes.map((r) => [r.server_id, r]))))
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function act(server, fn) {
    setBusyId(server.id);
    try {
      await fn(server.id);
      refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  const host = window.location.hostname;

  return (
    <div className="mcsm-servers">
      <div className="mcsm-servers-head">
        <h1>Servers</h1>
        <button onClick={() => setCreatingVanilla(true)}>New vanilla server</button>
      </div>

      {error && <p className="mcsm-error">{error}</p>}
      {servers === null && !error && <p className="mcsm-muted">Loading...</p>}
      {servers?.length === 0 && (
        <p className="mcsm-muted">No servers yet. Search for a modpack and choose "Create server".</p>
      )}

      <div className="mcsm-list">
        {servers?.map((s) => {
          const busy = busyId === s.id;
          const isUp = ["running", "starting"].includes(s.status);
          const settingUp = ["pulling", "creating", "installing", "testing"].includes(s.status);
          const missing = s.status === "missing";
          const details = [
            s.pack_title || (s.source === "vanilla" ? "Vanilla" : s.project_slug || s.project_id || s.image),
            s.minecraft_version && `MC ${s.minecraft_version}`,
            s.memory_gb > 0 && `${s.memory_gb} GB RAM`,
            s.java_version,
          ].filter(Boolean);
          return (
            <div key={s.id} className="mcsm-card">
              <div className="mcsm-card-main">
                <div className="mcsm-card-title">
                  <strong>{s.name}</strong>
                  <StatusBadge status={s.status} />
                  {!s.managed && (
                    <span className="mcsm-badge" title={`Container ${s.container_name} (${s.image})`}>
                      Found on host
                    </span>
                  )}
                </div>
                <div className="mcsm-muted mcsm-small">{details.join(" · ")}</div>
                {s.port > 0 && (
                  <div className="mcsm-small">
                    Address: <code>{host}:{s.port}</code>
                    {publicRoutes[s.id] && (
                      <>
                        {" · "}Public: <code>{publicRoutes[s.id].hostname}</code>
                        {publicRoutes[s.id].mode === "tunnel" && <span className="mcsm-muted"> (via tunnel)</span>}
                      </>
                    )}
                  </div>
                )}
                {s.error && !s.build && <div className="mcsm-error mcsm-small">{s.error}</div>}
                {s.note && <div className="mcsm-muted mcsm-small">{s.note}</div>}
                <BuildSummary build={s.build} />
              </div>
              <div className="mcsm-card-actions">
                {missing && (
                  <button className="mcsm-primary" disabled={busy} onClick={() => act(s, recreateServer)}>
                    {busy ? "Working..." : "Recreate container"}
                  </button>
                )}
                {!isUp && !settingUp && !missing && (
                  <button disabled={busy} onClick={() => act(s, startServer)}>
                    Start
                  </button>
                )}
                {isUp && !settingUp && (
                  <>
                    <button disabled={busy} onClick={() => act(s, stopServer)}>
                      {busy ? "Working..." : "Stop"}
                    </button>
                    <button disabled={busy} onClick={() => act(s, restartServer)}>
                      Restart
                    </button>
                  </>
                )}
                {!settingUp && !missing && (
                  <button className="mcsm-primary" onClick={() => setManageId(s.id)}>
                    Manage
                  </button>
                )}
                {!missing && <button onClick={() => setLogsFor(s)}>Logs</button>}
                <button
                  disabled={settingUp || !s.files_available}
                  title={s.files_available ? "" : s.note}
                  onClick={() => setBackupsFor(s)}
                >
                  Backups
                </button>
                {s.managed && (
                  <button className="mcsm-danger" disabled={busy} onClick={() => setDeleting(s)}>
                    {missing ? "Remove" : "Delete"}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {manageId && servers?.some((s) => s.id === manageId) && (
        <ServerManage
          server={servers.find((s) => s.id === manageId)}
          onClose={() => setManageId(null)}
          onChanged={refresh}
        />
      )}
      {logsFor && <LogsPanel server={logsFor} onClose={() => setLogsFor(null)} />}
      {backupsFor && (
        <BackupsDialog
          server={servers?.find((x) => x.id === backupsFor.id) || backupsFor}
          onClose={() => setBackupsFor(null)}
          onChanged={refresh}
        />
      )}
      {deleting && <DeleteDialog server={deleting} onClose={() => setDeleting(null)} onDeleted={refresh} />}
      <CreateServerDialog
        open={creatingVanilla}
        source="vanilla"
        onClose={() => setCreatingVanilla(false)}
        onCreated={refresh}
      />
    </div>
  );
}
