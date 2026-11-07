import { useEffect, useState } from "react";
import { api } from "../api.js";

const TUNNEL_STATUS = {
  healthy: "Connected",
  inactive: "Not connected yet",
  down: "Down",
  degraded: "Degraded",
  error: "Couldn't check",
};

export const MODE_HELP = {
  playit:
    "playit.gg: free for up to 4 servers. Players type the address into Minecraft with nothing to install, nothing is opened on your router, and your home IP stays hidden. You make each server's tunnel once in the playit dashboard.",
  tunnel:
    "Tunnel: nothing to open on your router and your home IP stays hidden. Each player installs cloudflared and runs one command before connecting, because Cloudflare only passes Minecraft's raw connection to clients that run it (unless you pay for Cloudflare Spectrum).",
  direct:
    "Direct: players just type the address into Minecraft. You forward each server's port on your router, and the address points at your home IP (DNS only, not hidden by Cloudflare).",
};

export default function CloudflareSettings() {
  const [status, setStatus] = useState(null);
  const [zones, setZones] = useState([]);
  const [token, setToken] = useState("");
  const [zoneId, setZoneId] = useState("");
  const [autoRoute, setAutoRoute] = useState(true);
  const [mode, setMode] = useState("tunnel");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  function apply(s) {
    setStatus(s);
    setZoneId(s.zone_id || "");
    setAutoRoute(s.zone_id ? s.auto_route : true);
    setMode(s.default_mode || "tunnel");
    setTarget(s.direct_target || "");
  }

  useEffect(() => {
    api
      .cloudflare()
      .then((s) => {
        apply(s);
        if (s.token_set) api.cloudflareZones().then(setZones).catch(() => {});
      })
      .catch((e) => setMessage({ error: true, text: e.message }));
  }, []);

  async function run(fn, text) {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      setMessage({ error: false, text });
    } catch (e) {
      setMessage({ error: true, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  const saveToken = (e) => {
    e.preventDefault();
    run(async () => {
      const z = await api.saveCloudflareToken(token);
      setZones(z);
      setToken("");
      if (!zoneId && z.length === 1) setZoneId(z[0].id);
      setStatus(await api.cloudflare());
    }, "Token saved. Now choose your domain and save.");
  };

  const saveConfig = (e) => {
    e.preventDefault();
    run(
      async () =>
        apply(
          await api.saveCloudflareConfig({
            zone_id: zoneId,
            auto_route: autoRoute,
            default_mode: mode,
            direct_target: target.trim(),
          }),
        ),
      "Saved. The app's tunnel and its cloudflared container are set up.",
    );
  };

  const sync = () => run(async () => apply(await api.syncCloudflare()), "Checked and updated.");

  const disconnect = () => {
    if (
      !window.confirm(
        "Disconnect Cloudflare? This deletes every public address the app made, the app's tunnel and its cloudflared container. Your own tunnels and DNS records are not touched.",
      )
    )
      return;
    run(async () => {
      await api.disconnectCloudflare();
      setZones([]);
      apply(await api.cloudflare());
    }, "Disconnected.");
  };

  const ready = status?.tunnel_id;

  return (
    <section className="panel">
      <h2>Cloudflare (public addresses)</h2>
      <p className="muted">
        Link your Cloudflare account and the app gives each server its own address on your domain, like{" "}
        <code>survival.{status?.zone_name || "example.com"}</code>, and removes it when the server is deleted. The app
        makes its own tunnel named <code>minecraft-server-manager</code> and runs its own <code>mcsm_cloudflared</code>{" "}
        container, so your existing tunnel and its settings are left alone.
      </p>
      <details>
        <summary>How to make the API token</summary>
        <ol className="muted">
          <li>
            Open{" "}
            <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noreferrer">
              dash.cloudflare.com/profile/api-tokens
            </a>{" "}
            and choose Create Token, then Create Custom Token.
          </li>
          <li>
            Permissions: <strong>Account › Cloudflare Tunnel › Edit</strong>, <strong>Zone › DNS › Edit</strong> and{" "}
            <strong>Zone › Zone › Read</strong>.
          </li>
          <li>Zone resources: Include › Specific zone › your domain. Account resources: your account.</li>
          <li>Create it and paste it below. The token is stored only on your server and never shown again.</li>
        </ol>
      </details>

      {status && (
        <p>
          Token:{" "}
          {status.token_set ? (
            <strong>Saved {status.token_hint && <code>{status.token_hint}</code>}</strong>
          ) : (
            <strong>Not linked yet</strong>
          )}
          {ready && (
            <>
              <br />
              Tunnel: <strong>{TUNNEL_STATUS[status.tunnel_status] ?? status.tunnel_status}</strong>
              {status.tunnel_connections > 0 && <span className="muted"> ({status.tunnel_connections} links)</span>}
              {" · "}cloudflared container: <strong>{status.connector_status}</strong>
              {status.error && <span className="error"> {status.error}</span>}
            </>
          )}
        </p>
      )}

      <form className="key-form" onSubmit={saveToken}>
        <input
          type="password"
          autoComplete="off"
          placeholder={status?.token_set ? "Paste a new token to replace it" : "Paste your Cloudflare API token"}
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <button className="primary" type="submit" disabled={busy || !token.trim()}>
          Save token
        </button>
      </form>

      {status?.token_set && (
        <form className="cf-config" onSubmit={saveConfig}>
          <label>
            Domain
            <select value={zoneId} onChange={(e) => setZoneId(e.target.value)} required>
              <option value="">Choose a domain</option>
              {zones.map((z) => (
                <option key={z.id} value={z.id}>
                  {z.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            How players connect (default for new servers)
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="tunnel">Tunnel (no port forwarding, players run cloudflared)</option>
              <option value="direct">Direct (port forwarding, players need nothing)</option>
              <option value="playit">playit.gg (free, players need nothing, up to 4 servers)</option>
            </select>
          </label>
          <p className="muted small">{MODE_HELP[mode]}</p>
          {mode === "direct" && (
            <label>
              Public IP or hostname for direct addresses (leave empty to detect and keep it updated)
              <input placeholder="Detect automatically" value={target} onChange={(e) => setTarget(e.target.value)} />
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={autoRoute} onChange={(e) => setAutoRoute(e.target.checked)} />
            Give every new server a public address automatically
          </label>
          <div className="key-form">
            <button className="primary" type="submit" disabled={busy || !zoneId}>
              {busy ? "Working..." : ready ? "Save" : "Set up tunnel"}
            </button>
            {ready && (
              <button type="button" disabled={busy} onClick={sync}>
                Check and repair
              </button>
            )}
            <button type="button" disabled={busy} onClick={disconnect}>
              Disconnect
            </button>
          </div>
        </form>
      )}
      {message && <p className={message.error ? "error" : "ok"}>{message.text}</p>}
      {status?.token_set && <PlayitAgent status={status} onStatus={setStatus} />}
    </section>
  );
}

function PlayitAgent({ status, onStatus }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  async function save(value) {
    setBusy(true);
    setMessage(null);
    try {
      onStatus(await api.savePlayitKey(value));
      setKey("");
      setMessage({ error: false, text: value ? "playit agent is running." : "playit agent removed." });
    } catch (e) {
      setMessage({ error: true, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h3>playit.gg agent</h3>
      <p className="muted">
        Lets players join with nothing installed, free for up to 4 servers. Make a free account at{" "}
        <a href="https://playit.gg" target="_blank" rel="noreferrer">
          playit.gg
        </a>
        , go to Agents, add a Docker agent and paste its secret key here. The app runs it as the{" "}
        <code>mcsm_playit</code> container.
      </p>
      <p>
        Agent:{" "}
        <strong>{status.playit_key_set ? status.playit_status : "not set up"}</strong>
      </p>
      <form
        className="key-form"
        onSubmit={(e) => {
          e.preventDefault();
          save(key.trim());
        }}
      >
        <input
          type="password"
          autoComplete="off"
          placeholder={status.playit_key_set ? "Paste a new secret key to replace it" : "playit secret key"}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
        <button className="primary" type="submit" disabled={busy || !key.trim()}>
          Save key
        </button>
        {status.playit_key_set && (
          <button type="button" disabled={busy} onClick={() => save("")}>
            Remove agent
          </button>
        )}
      </form>
      {message && <p className={message.error ? "error" : "ok"}>{message.text}</p>}
    </>
  );
}
