import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { getPackMode, prepareScript, switchPackMode } from "../api/servers.js";

const shellQuote = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;

function scriptName(url) {
  try {
    const name = decodeURIComponent(new URL(url).pathname.split("/").pop() || "");
    return /^[\w.-]+$/.test(name) ? name : "script.sh";
  } catch {
    return "script.sh";
  }
}

// A CurseForge "linkout" URL wraps the real link (double-encoded); unwrap it.
function unwrapLink(url) {
  try {
    const u = new URL(url.trim());
    const remote = u.searchParams.get("remoteUrl");
    if (remote) return decodeURIComponent(remote);
  } catch {
    /* not a URL */
  }
  return url.trim();
}

function PackModePanel({ server }) {
  const [info, setInfo] = useState(null);
  const [mode, setMode] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const i = await getPackMode(server.id);
      setInfo(i);
      setMode((m) => m || i.modes.find((x) => x !== i.current) || i.current);
      return i;
    } catch (e) {
      setError(e.message);
      return null;
    }
  }, [server.id]);

  useEffect(() => {
    load();
  }, [load]);

  const running = info?.job?.state === "running";
  useEffect(() => {
    if (!running) return;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [running, load]);

  if (!info?.supported) return null;

  async function apply() {
    const verb = ["running", "starting"].includes(server.status) ? "stop the server, " : "";
    if (!confirm(`Switch to ${mode} mode? This will ${verb}take a full backup and copy the ${mode} configs over config/.`))
      return;
    setError("");
    try {
      setInfo(await switchPackMode(server.id, mode));
    } catch (e) {
      setError(e.message);
    }
  }

  const job = info.job;
  return (
    <section className="mcsm-section mcsm-packmode">
      <h3>Pack mode</h3>
      <p className="mcsm-small mcsm-muted">
        This pack ships alternative configs in <code>config-overrides/</code>. Current mode:{" "}
        <strong>{info.current || "unknown"}</strong>
      </p>
      <div className="mcsm-inline-form">
        <select value={mode} onChange={(e) => setMode(e.target.value)} disabled={running}>
          {info.modes.map((m) => (
            <option key={m} value={m}>
              {m}
              {m === info.current ? " (current)" : ""}
            </option>
          ))}
        </select>
        <button className="mcsm-primary" onClick={apply} disabled={running || !mode}>
          {running ? "Switching..." : "Switch mode"}
        </button>
      </div>
      {job?.state && job.state !== "idle" && (
        <p className={`mcsm-small${job.state === "failed" ? " mcsm-error" : ""}`}>{job.message}</p>
      )}
      {error && <p className="mcsm-small mcsm-error">{error}</p>}
    </section>
  );
}

export default function ShellTab({ server }) {
  const holder = useRef(null);
  const term = useRef(null);
  const fit = useRef(null);
  const ws = useRef(null);
  const [root, setRoot] = useState(false);
  const [state, setState] = useState("connecting"); // connecting | open | closed
  const [note, setNote] = useState("");
  const [session, setSession] = useState(0);
  const [url, setUrl] = useState("");
  const [args, setArgs] = useState("");
  const [prep, setPrep] = useState(true);
  const [prepMsg, setPrepMsg] = useState("");

  useEffect(() => {
    const t = new Terminal({
      cursorBlink: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 13,
      scrollback: 5000,
      theme: { background: "#111318" },
    });
    const f = new FitAddon();
    t.loadAddon(f);
    t.open(holder.current);
    f.fit();
    term.current = t;
    fit.current = f;
    const send = (obj) => ws.current?.readyState === WebSocket.OPEN && ws.current.send(JSON.stringify(obj));
    const onData = t.onData((d) => send({ t: "i", d }));
    const onResize = t.onResize(({ cols, rows }) => send({ t: "r", c: cols, r: rows }));
    const ro = new ResizeObserver(() => {
      try {
        f.fit();
      } catch {
        /* hidden */
      }
    });
    ro.observe(holder.current);
    return () => {
      ro.disconnect();
      onData.dispose();
      onResize.dispose();
      t.dispose();
    };
  }, []);

  useEffect(() => {
    const t = term.current;
    if (!t) return;
    t.reset();
    setNote("");
    setState("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = new URLSearchParams({ root: String(root), cols: t.cols, rows: t.rows });
    const sock = new WebSocket(`${proto}://${location.host}/api/servers/${encodeURIComponent(server.id)}/shell?${q}`);
    sock.binaryType = "arraybuffer";
    ws.current = sock;
    sock.onopen = () => {
      setState("open");
      t.focus();
    };
    sock.onmessage = (e) => {
      if (typeof e.data !== "string") {
        t.write(new Uint8Array(e.data));
        return;
      }
      const msg = JSON.parse(e.data);
      if (msg.t === "note") setNote(msg.d);
      else if (msg.t === "error") t.write(`\r\n\x1b[31m${msg.d}\x1b[0m\r\n`);
      else if (msg.t === "exit") t.write(`\r\n\x1b[90m[shell exited${msg.code != null ? ` with code ${msg.code}` : ""}]\x1b[0m\r\n`);
    };
    sock.onclose = (e) => {
      setState("closed");
      if (e.reason) t.write(`\r\n\x1b[31m${e.reason}\x1b[0m\r\n`);
    };
    return () => sock.close();
  }, [server.id, root, session]);

  function type(text) {
    if (ws.current?.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify({ t: "i", d: text }));
      term.current?.focus();
    }
  }

  async function runScript(e) {
    e.preventDefault();
    const link = unwrapLink(url);
    if (!/^https?:\/\//i.test(link)) return;
    const name = scriptName(link);
    if (/\.(bat|cmd|ps1)$/i.test(name)) {
      setPrepMsg("That's a Windows script. Use the Mac/Linux (.sh) version of it.");
      return;
    }
    const runner = name.endsWith(".py") ? "python3" : name.endsWith(".bash") ? "bash" : "sh";
    const q = shellQuote(name);
    const extra = args.trim() ? ` ${args.trim()}` : "";
    const cmd =
      `cd /data && { curl -fsSL -o ${q} ${shellQuote(link)} || wget -qO ${q} ${shellQuote(link)}; }` +
      ` && chmod +x ${q} && ${runner} ./${q}${extra}\r`;
    if (!prep) {
      setPrepMsg("");
      type(cmd);
      return;
    }
    setPrepMsg("Stopping the server and taking a full backup. This can take a minute...");
    try {
      const r = await prepareScript(server.id);
      setPrepMsg(`${r.stopped ? "Server stopped. " : ""}Backup ${r.backup_id} saved. Running the script...`);
    } catch (err) {
      setPrepMsg(`Nothing was run: ${err.message}`);
      return;
    }
    // Stopping the server ends a shell that was inside it, so open a new one, then type the command.
    pendingCmd.current = cmd;
    setSession((n) => n + 1);
  }

  const pendingCmd = useRef("");
  useEffect(() => {
    if (state === "open" && pendingCmd.current) {
      const cmd = pendingCmd.current;
      pendingCmd.current = "";
      setTimeout(() => type(cmd), 600);
    }
  }, [state]);

  return (
    <div className="mcsm-shell">
      <div className="mcsm-shell-bar mcsm-small">
        <span className={`mcsm-badge${state === "closed" ? " mcsm-error" : ""}`}>
          {state === "open" ? "connected" : state}
        </span>
        <span className="mcsm-muted">Starts in /data, the pack folder (the one with /config).</span>
        <label className="mcsm-check">
          <input type="checkbox" checked={root} onChange={(e) => setRoot(e.target.checked)} /> root
        </label>
        <button onClick={() => setSession((n) => n + 1)}>{state === "closed" ? "Reconnect" : "New shell"}</button>
      </div>
      {note && <p className="mcsm-small mcsm-muted">{note}</p>}
      <div className="mcsm-terminal" ref={holder} />
      <form className="mcsm-inline-form mcsm-shell-script" onSubmit={runScript}>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="Run script from URL, e.g. https://raw.githubusercontent.com/.../switcher.sh"
        />
        <input className="mcsm-shell-args" value={args} onChange={(e) => setArgs(e.target.value)} placeholder="arguments" />
        <label className="mcsm-check mcsm-small">
          <input type="checkbox" checked={prep} onChange={(e) => setPrep(e.target.checked)} /> stop and back up first
        </label>
        <button type="submit" disabled={state !== "open" || !/^https?:\/\//i.test(unwrapLink(url))}>
          Run
        </button>
      </form>
      {prepMsg && <p className="mcsm-small mcsm-muted">{prepMsg}</p>}
      <PackModePanel server={server} />
    </div>
  );
}
