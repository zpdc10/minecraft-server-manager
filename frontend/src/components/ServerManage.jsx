import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteMod,
  getLogs,
  getPlayers,
  getProperties,
  getStats,
  listMods,
  playerAction,
  saveProperties,
  sendCommand,
  setModEnabled,
  setWhitelist,
  updateSettings,
  uploadMod,
} from "../api/servers.js";
import "./servers.css";

const TABS = [
  ["console", "Console"],
  ["players", "Players"],
  ["properties", "server.properties"],
  ["mods", "Mods"],
  ["settings", "Settings"],
];

const JAVA_OPTIONS = [
  ["java8", "Java 8"],
  ["java11", "Java 11"],
  ["java16", "Java 16"],
  ["java17", "Java 17"],
  ["java21", "Java 21"],
  ["latest", "Latest"],
];

/** Poll fn every `ms` while mounted; returns [data, error, reload]. */
function usePoll(fn, ms, deps) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(fn, deps);
  const reload = useCallback(
    () =>
      load()
        .then((d) => {
          setData(d);
          setError("");
        })
        .catch((e) => setError(e.message)),
    [load],
  );
  useEffect(() => {
    reload();
    if (!ms) return undefined;
    const t = setInterval(reload, ms);
    return () => clearInterval(t);
  }, [reload, ms]);
  return [data, error, reload];
}

function formatUptime(startedAt) {
  const secs = Math.max(0, (Date.now() - Date.parse(startedAt)) / 1000);
  if (!Number.isFinite(secs)) return "";
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function StatsBar({ server }) {
  const [stats] = usePoll(() => getStats(server.id), 5000, [server.id]);
  if (!stats) return <div className="mcsm-stats mcsm-muted mcsm-small">Loading stats...</div>;
  if (!stats.running) return <div className="mcsm-stats mcsm-muted mcsm-small">Server is not running</div>;
  const memPct = stats.memory_limit_mb ? (stats.memory_used_mb / stats.memory_limit_mb) * 100 : 0;
  return (
    <div className="mcsm-stats mcsm-small">
      <span>
        CPU <strong>{stats.cpu_percent}%</strong>
        <span className="mcsm-muted"> of {stats.cpu_count} cores</span>
      </span>
      <span className="mcsm-meter-wrap">
        RAM{" "}
        <strong>
          {(stats.memory_used_mb / 1024).toFixed(1)} / {(stats.memory_limit_mb / 1024).toFixed(1)} GB
        </strong>
        <span className="mcsm-meter">
          <span style={{ width: `${Math.min(100, memPct)}%` }} />
        </span>
      </span>
      {stats.started_at && <span className="mcsm-muted">Up {formatUptime(stats.started_at)}</span>}
    </div>
  );
}

// ---- Console ------------------------------------------------------------------

function ConsoleTab({ server, running }) {
  const [logs] = usePoll(() => getLogs(server.id, 500), 2500, [server.id]);
  const [command, setCommand] = useState("");
  const [history, setHistory] = useState([]);
  const [replies, setReplies] = useState([]);
  const [pos, setPos] = useState(-1);
  const [busy, setBusy] = useState(false);
  const preRef = useRef(null);
  const stick = useRef(true);

  useEffect(() => {
    const el = preRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [logs]);

  async function submit(e) {
    e.preventDefault();
    const cmd = command.trim();
    if (!cmd) return;
    setBusy(true);
    setHistory((h) => [cmd, ...h.filter((x) => x !== cmd)].slice(0, 50));
    setPos(-1);
    try {
      const r = await sendCommand(server.id, cmd);
      setReplies((rs) => [{ cmd: r.command, out: r.output || "(no reply)", ok: true }, ...rs].slice(0, 20));
      setCommand("");
    } catch (err) {
      setReplies((rs) => [{ cmd, out: err.message, ok: false }, ...rs].slice(0, 20));
    } finally {
      setBusy(false);
    }
  }

  function onKey(e) {
    if (e.key === "ArrowUp" && history.length) {
      e.preventDefault();
      const p = Math.min(pos + 1, history.length - 1);
      setPos(p);
      setCommand(history[p]);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      const p = pos - 1;
      setPos(Math.max(p, -1));
      setCommand(p >= 0 ? history[p] : "");
    }
  }

  return (
    <div className="mcsm-console">
      <pre
        ref={preRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {logs ?? "Loading..."}
      </pre>
      <form className="mcsm-console-input" onSubmit={submit}>
        <span className="mcsm-muted">/</span>
        <input
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={onKey}
          placeholder={running ? "Type a command, e.g. say Hello or time set day" : "Start the server to send commands"}
          disabled={!running || busy}
          autoFocus
        />
        <button type="submit" className="mcsm-primary" disabled={!running || busy || !command.trim()}>
          Send
        </button>
      </form>
      {replies.length > 0 && (
        <div className="mcsm-replies mcsm-small">
          {replies.map((r, i) => (
            <div key={i} className={r.ok ? "" : "mcsm-error"}>
              <code>/{r.cmd}</code> <span className="mcsm-muted">→</span> {r.out}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- Players ------------------------------------------------------------------

function AddPlayer({ label, onAdd, disabled, placeholder = "Player name" }) {
  const [name, setName] = useState("");
  return (
    <form
      className="mcsm-inline-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) onAdd(name.trim()).then((ok) => ok && setName(""));
      }}
    >
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder={placeholder} disabled={disabled} />
      <button type="submit" disabled={disabled || !name.trim()}>
        {label}
      </button>
    </form>
  );
}

function PlayerList({ title, entries, empty, actions }) {
  return (
    <section className="mcsm-section">
      {title && (
        <h3>
          {title} <span className="mcsm-muted">({entries.length})</span>
        </h3>
      )}
      {entries.length === 0 ? (
        <p className="mcsm-muted mcsm-small">{empty}</p>
      ) : (
        <ul className="mcsm-plist">
          {entries.map((p) => (
            <li key={p.uuid || p.name}>
              <span>
                <strong>{p.name}</strong>
                {p.level != null && <span className="mcsm-badge">level {p.level}</span>}
                {p.reason && <span className="mcsm-muted mcsm-small"> · {p.reason}</span>}
              </span>
              <span className="mcsm-plist-actions">{actions(p)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PlayersTab({ server }) {
  const [data, error, reload] = usePoll(() => getPlayers(server.id), 5000, [server.id]);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);

  const ready = data?.console_ready;

  async function run(fn) {
    setBusy(true);
    try {
      const r = await fn();
      setMessage({ ok: true, text: r.output || "Done" });
      await reload();
      return true;
    } catch (e) {
      setMessage({ ok: false, text: e.message });
      return false;
    } finally {
      setBusy(false);
    }
  }
  const act = (action, name, reason = "") => run(() => playerAction(server.id, action, name, reason));

  function kickOrBan(action, name) {
    const reason = window.prompt(`Reason for ${action === "kick" ? "kicking" : "banning"} ${name} (optional)`, "");
    if (reason === null) return;
    act(action, name, reason);
  }

  if (!data) return <p className="mcsm-muted">{error || "Loading..."}</p>;
  const isOp = new Set(data.ops.map((o) => o.name.toLowerCase()));
  const dis = !ready || busy;

  return (
    <div className="mcsm-players">
      {!data.running && <p className="mcsm-muted">The server is stopped. Start it to change players.</p>}
      {data.running && !ready && <p className="mcsm-muted">Waiting for the server to finish starting...</p>}
      {message && <p className={message.ok ? "mcsm-ok mcsm-small" : "mcsm-error mcsm-small"}>{message.text}</p>}

      <PlayerList
        title={`Online${data.max_players ? ` (max ${data.max_players})` : ""}`}
        entries={data.online.map((name) => ({ name }))}
        empty={data.running ? "Nobody is online." : "Server is not running."}
        actions={(p) => (
          <>
            {isOp.has(p.name.toLowerCase()) ? (
              <button disabled={dis} onClick={() => act("deop", p.name)}>
                De-op
              </button>
            ) : (
              <button disabled={dis} onClick={() => act("op", p.name)}>
                Op
              </button>
            )}
            <button disabled={dis} onClick={() => kickOrBan("kick", p.name)}>
              Kick
            </button>
            <button className="mcsm-danger" disabled={dis} onClick={() => kickOrBan("ban", p.name)}>
              Ban
            </button>
          </>
        )}
      />

      <section className="mcsm-section">
        <div className="mcsm-section-head">
          <h3>
            Whitelist <span className="mcsm-muted">({data.whitelist.length})</span>
          </h3>
          <label className="mcsm-check mcsm-small">
            <input
              type="checkbox"
              checked={data.whitelist_enabled}
              disabled={busy || (data.running && !ready)}
              onChange={(e) => run(() => setWhitelist(server.id, e.target.checked))}
            />
            Only whitelisted players can join
          </label>
        </div>
        <AddPlayer label="Add" disabled={dis} onAdd={(n) => act("whitelist_add", n)} />
        <PlayerList
          title=""
          entries={data.whitelist}
          empty="No one is on the whitelist."
          actions={(p) => (
            <button disabled={dis} onClick={() => act("whitelist_remove", p.name)}>
              Remove
            </button>
          )}
        />
      </section>

      <section className="mcsm-section">
        <h3>
          Operators <span className="mcsm-muted">({data.ops.length})</span>
        </h3>
        <AddPlayer label="Make op" disabled={dis} onAdd={(n) => act("op", n)} />
        <PlayerList
          title=""
          entries={data.ops}
          empty="No operators."
          actions={(p) => (
            <button disabled={dis} onClick={() => act("deop", p.name)}>
              De-op
            </button>
          )}
        />
      </section>

      <section className="mcsm-section">
        <h3>
          Bans <span className="mcsm-muted">({data.banned.length + data.banned_ips.length})</span>
        </h3>
        <div className="mcsm-row">
          <AddPlayer label="Ban" disabled={dis} onAdd={(n) => act("ban", n)} />
          <AddPlayer label="Ban IP" placeholder="IP address" disabled={dis} onAdd={(n) => act("ban_ip", n)} />
        </div>
        <PlayerList
          title=""
          entries={[...data.banned, ...data.banned_ips.map((b) => ({ ...b, ip: true }))]}
          empty="No bans."
          actions={(p) => (
            <button disabled={dis} onClick={() => act(p.ip ? "pardon_ip" : "pardon", p.name)}>
              Unban
            </button>
          )}
        />
      </section>
    </div>
  );
}

// ---- server.properties ----------------------------------------------------------

function PropertiesTab({ server, running }) {
  const [info, error, reload] = usePoll(() => getProperties(server.id), 0, [server.id]);
  const [edits, setEdits] = useState({});
  const [filter, setFilter] = useState("");
  const [newKey, setNewKey] = useState("");
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);

  if (!info) return <p className="mcsm-muted">{error || "Loading..."}</p>;
  if (!info.exists)
    return <p className="mcsm-muted">server.properties doesn&apos;t exist yet. Start the server once to create it.</p>;

  const props = { ...info.properties, ...edits };
  const changed = Object.keys(edits).filter((k) => edits[k] !== info.properties[k]);
  const keys = Object.keys(props)
    .sort()
    .filter((k) => k.includes(filter.toLowerCase()));

  async function save(restart) {
    setBusy(true);
    setMessage(null);
    try {
      await saveProperties(server.id, Object.fromEntries(changed.map((k) => [k, edits[k]])), restart);
      setEdits({});
      await reload();
      setMessage({
        ok: true,
        text: restart ? "Saved. The server is restarting." : running ? "Saved. Restart the server to apply." : "Saved.",
      });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mcsm-props">
      <div className="mcsm-props-head">
        <input placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="mcsm-muted mcsm-small">{changed.length ? `${changed.length} unsaved` : ""}</span>
        <button disabled={busy || !changed.length} onClick={() => setEdits({})}>
          Discard
        </button>
        <button className="mcsm-primary" disabled={busy || !changed.length} onClick={() => save(false)}>
          Save
        </button>
        {running && (
          <button className="mcsm-primary" disabled={busy || !changed.length} onClick={() => save(true)}>
            Save and restart
          </button>
        )}
      </div>
      {message && <p className={message.ok ? "mcsm-ok mcsm-small" : "mcsm-error mcsm-small"}>{message.text}</p>}
      <div className="mcsm-props-grid">
        {keys.map((k) => {
          const v = props[k];
          const locked = info.locked[k];
          const isBool = info.properties[k] === "true" || info.properties[k] === "false";
          const set = (val) => setEdits((e) => ({ ...e, [k]: val }));
          return (
            <label key={k} className={changed.includes(k) ? "mcsm-changed" : ""} title={locked || ""}>
              <code>{k}</code>
              {isBool ? (
                <select value={v} disabled={!!locked} onChange={(e) => set(e.target.value)}>
                  <option value="true">true</option>
                  <option value="false">false</option>
                </select>
              ) : (
                <input
                  value={k === "rcon.password" ? "••••••" : v}
                  disabled={!!locked}
                  onChange={(e) => set(e.target.value)}
                />
              )}
              {locked && <span className="mcsm-muted mcsm-small">🔒 {locked}</span>}
            </label>
          );
        })}
      </div>
      <form
        className="mcsm-inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          const k = newKey.trim();
          if (k && !(k in props)) setEdits((ed) => ({ ...ed, [k]: "" }));
          setNewKey("");
          setFilter("");
        }}
      >
        <input placeholder="Add a property" value={newKey} onChange={(e) => setNewKey(e.target.value)} />
        <button type="submit" disabled={!newKey.trim()}>
          Add
        </button>
      </form>
    </div>
  );
}

// ---- Mods -----------------------------------------------------------------------

function formatSize(bytes) {
  return bytes > 2 ** 20 ? `${(bytes / 2 ** 20).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;
}

function ModsTab({ server, running }) {
  const [mods, error, reload] = usePoll(() => listMods(server.id), 0, [server.id]);
  const [filter, setFilter] = useState("");
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);

  async function run(fn, ok) {
    setBusy(true);
    try {
      await fn();
      await reload();
      setMessage({ ok: true, text: `${ok}${running ? " Restart the server to apply." : ""}` });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  async function upload(files) {
    const list = [...files];
    if (!list.length) return;
    await run(async () => {
      for (const f of list) await uploadMod(server.id, f);
    }, `Added ${list.length} mod${list.length === 1 ? "" : "s"}.`);
    if (fileRef.current) fileRef.current.value = "";
  }

  if (!mods) return <p className="mcsm-muted">{error || "Loading..."}</p>;
  const shown = mods.filter((m) => m.file.toLowerCase().includes(filter.toLowerCase()));
  const enabled = mods.filter((m) => m.enabled).length;

  return (
    <div className="mcsm-mods">
      <div className="mcsm-props-head">
        <input placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="mcsm-muted mcsm-small">
          {enabled} enabled{mods.length > enabled ? `, ${mods.length - enabled} disabled` : ""}
        </span>
        <button className="mcsm-primary" disabled={busy} onClick={() => fileRef.current?.click()}>
          Add mod jars
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".jar"
          multiple
          hidden
          onChange={(e) => upload(e.target.files)}
        />
      </div>
      {message && <p className={message.ok ? "mcsm-ok mcsm-small" : "mcsm-error mcsm-small"}>{message.text}</p>}
      {mods.length === 0 ? (
        <p className="mcsm-muted">This server has no mods folder yet.</p>
      ) : (
        <ul className="mcsm-plist">
          {shown.map((m) => (
            <li key={m.file} className={m.enabled ? "" : "mcsm-disabled"}>
              <span>
                <code>{m.file.replace(/\.disabled$/, "")}</code>{" "}
                <span className="mcsm-muted mcsm-small">{formatSize(m.size)}</span>
                {!m.enabled && <span className="mcsm-badge">disabled</span>}
              </span>
              <span className="mcsm-plist-actions">
                <button
                  disabled={busy}
                  onClick={() =>
                    run(
                      () => setModEnabled(server.id, m.file, !m.enabled),
                      m.enabled ? "Mod disabled." : "Mod enabled.",
                    )
                  }
                >
                  {m.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  className="mcsm-danger"
                  disabled={busy}
                  onClick={() =>
                    window.confirm(`Remove ${m.file}? It is moved to .mcsm/removed-mods in the server folder.`) &&
                    run(() => deleteMod(server.id, m.file), "Mod removed.")
                  }
                >
                  Remove
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---- Settings -------------------------------------------------------------------

function SettingsTab({ server, running, onChanged }) {
  const [name, setName] = useState(server.name);
  const [port, setPort] = useState(server.port);
  const [memory, setMemory] = useState(server.memory_gb);
  const [java, setJava] = useState(server.java_version);
  const [motd, setMotd] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    getProperties(server.id)
      .then((p) => setMotd(p.properties.motd ?? server.name))
      .catch(() => setMotd(server.name));
  }, [server.id, server.name]);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const updated = await updateSettings(server.id, {
        name,
        port: Number(port),
        memory_gb: Number(memory),
        java_version: java,
        motd,
      });
      setMessage({ ok: true, text: running ? "Saved. The server is restarting with the new settings." : "Saved." });
      onChanged?.(updated);
    } catch (err) {
      setMessage({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="mcsm-settings" onSubmit={submit}>
      <label>
        Server name
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={48} />
      </label>
      <label>
        Message of the day (shown in the server list)
        <input value={motd} onChange={(e) => setMotd(e.target.value)} maxLength={200} />
      </label>
      <div className="mcsm-row">
        <label>
          Port
          <input type="number" min={1024} max={65535} value={port} onChange={(e) => setPort(e.target.value)} required />
        </label>
        <label>
          RAM (GB)
          <input type="number" min={1} max={64} value={memory} onChange={(e) => setMemory(e.target.value)} required />
        </label>
        <label>
          Java
          <select value={java} onChange={(e) => setJava(e.target.value)}>
            {JAVA_OPTIONS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="mcsm-muted mcsm-small">
        Saving recreates the server&apos;s container. The world and all files are kept.
        {running && " The server stops and starts again, so players are disconnected."}
        {java !== server.java_version && " A new Java image may need to download first, which can take a minute."}
      </p>
      {message && <p className={message.ok ? "mcsm-ok mcsm-small" : "mcsm-error mcsm-small"}>{message.text}</p>}
      <div className="mcsm-actions">
        <button type="submit" className="mcsm-primary" disabled={busy}>
          {busy ? "Saving..." : running ? "Save and restart" : "Save"}
        </button>
      </div>
    </form>
  );
}

// ---- Dialog ---------------------------------------------------------------------

export default function ServerManage({ server, onClose, onChanged, initialTab = "console" }) {
  const [tab, setTab] = useState(initialTab);
  const running = ["running", "starting"].includes(server.status);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="mcsm-backdrop" onClick={onClose}>
      <div className="mcsm-dialog mcsm-manage" onClick={(e) => e.stopPropagation()}>
        <div className="mcsm-logs-head">
          <h2>{server.name}</h2>
          <button onClick={onClose}>Close</button>
        </div>
        <StatsBar server={server} />
        {!server.managed && (
          <p className="mcsm-muted mcsm-small">
            This server's container wasn't created by this app, so its settings are managed in Unraid's Docker tab.
            {server.note && ` ${server.note}`}
          </p>
        )}
        <div className="mcsm-tabs">
          {TABS.filter(([key]) => server.managed || key !== "settings").map(([key, label]) => (
            <button key={key} className={`tab${tab === key ? " active" : ""}`} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </div>
        <div className="mcsm-tab-body">
          {tab === "console" && <ConsoleTab server={server} running={running} />}
          {tab === "players" && <PlayersTab server={server} />}
          {tab === "properties" && <PropertiesTab server={server} running={running} />}
          {tab === "mods" && <ModsTab server={server} running={running} />}
          {tab === "settings" && <SettingsTab server={server} running={running} onChanged={onChanged} />}
        </div>
      </div>
    </div>
  );
}
