import { useEffect, useState } from "react";
import { api } from "../api.js";
import { MODE_HELP } from "./CloudflareSettings.jsx";

function Copy({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={() =>
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        })
      }
    >
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export default function PublicAddressTab({ server }) {
  const [cf, setCf] = useState(null);
  const [route, setRoute] = useState(undefined);
  const [subdomain, setSubdomain] = useState("");
  const [mode, setMode] = useState("tunnel");
  const [playitAddress, setPlayitAddress] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  async function load() {
    const [status, routes] = await Promise.all([api.cloudflare(), api.publicRoutes()]);
    const r = routes.find((x) => x.server_id === server.id) || null;
    setCf(status);
    setRoute(r);
    setSubdomain(r ? r.hostname.split(".")[0] : server.id.replace(/^ext-/, ""));
    setMode(r ? r.mode : status.default_mode || "tunnel");
    setPlayitAddress(r?.playit_address || "");
  }

  useEffect(() => {
    load().catch((e) => setMessage({ ok: false, text: e.message }));
  }, [server.id]);

  async function run(fn, text) {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      await load();
      setMessage({ ok: true, text });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  if (cf === null || route === undefined) {
    return message ? <p className="mcsm-error mcsm-small">{message.text}</p> : <p className="mcsm-muted">Loading...</p>;
  }
  if (!cf.tunnel_id) {
    return (
      <p className="mcsm-muted">
        Link your Cloudflare account on the Settings page to give this server an address on your domain.
      </p>
    );
  }

  const changed =
    route &&
    (route.mode !== mode ||
      route.hostname.split(".")[0] !== subdomain.trim().toLowerCase() ||
      (mode === "playit" && playitAddress.trim() !== route.playit_address));

  return (
    <div className="mcsm-settings">
      {route ? (
        <section className="mcsm-section">
          <h3>Players connect with</h3>
          {route.mode === "tunnel" ? (
            <>
              <p className="mcsm-small">
                Each player installs{" "}
                <a
                  href="https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
                  target="_blank"
                  rel="noreferrer"
                >
                  cloudflared
                </a>{" "}
                once, runs this command and leaves it open while playing:
              </p>
              <div className="mcsm-row">
                <code className="mcsm-command">{route.player_command}</code>
                <Copy text={route.player_command} />
              </div>
              <p className="mcsm-small">
                Then in Minecraft, add the server <code>127.0.0.1:25565</code>. You can share these steps with your
                players as they are.
              </p>
            </>
          ) : route.mode === "playit" ? (
            <>
              <div className="mcsm-row">
                <code className="mcsm-command">{route.hostname}</code>
                <Copy text={route.hostname} />
              </div>
              <p className="mcsm-small">
                Players type this into Minecraft as is, with nothing to install. It leads to your playit tunnel{" "}
                <code>{route.playit_address}</code>
                {route.target && <span className="mcsm-muted"> ({route.target})</span>}. If you change this
                server&apos;s port, update the tunnel&apos;s local port in the playit dashboard too.
              </p>
            </>
          ) : (
            <>
              <div className="mcsm-row">
                <code className="mcsm-command">{route.hostname}</code>
                <Copy text={route.hostname} />
              </div>
              <p className="mcsm-small">
                Players type this into Minecraft as is. Forward port <strong>{server.port}</strong> (TCP) on your router
                to your Unraid server, or nobody outside your network can join.
              </p>
            </>
          )}
          <p className="mcsm-muted mcsm-small">
            On your own network you can still use <code>{window.location.hostname}:{server.port}</code>.
          </p>
        </section>
      ) : (
        <p className="mcsm-muted">This server has no public address yet.</p>
      )}

      <form
        className="mcsm-settings"
        onSubmit={(e) => {
          e.preventDefault();
          run(
            () =>
              api.setPublicRoute(server.id, { subdomain: subdomain.trim(), mode, playit_address: playitAddress.trim() }),
            route ? "Address updated." : "Address created. DNS can take a minute to work everywhere.",
          );
        }}
      >
        <div className="mcsm-row">
          <label>
            Address
            <span className="mcsm-row" style={{ alignItems: "center", gap: 4 }}>
              <input value={subdomain} onChange={(e) => setSubdomain(e.target.value)} required maxLength={63} />
              <span className="mcsm-muted">.{cf.zone_name}</span>
            </span>
          </label>
          <label>
            Mode
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="tunnel">Tunnel</option>
              <option value="direct">Direct</option>
              <option value="playit">playit.gg</option>
            </select>
          </label>
        </div>
        <p className="mcsm-muted mcsm-small">{MODE_HELP[mode]}</p>
        {mode === "playit" && (
          <>
            {!cf.playit_key_set && (
              <p className="mcsm-error mcsm-small">Add your playit agent key on the Settings page first.</p>
            )}
            <ol className="mcsm-small">
              <li>
                In the{" "}
                <a href="https://playit.gg/account/tunnels" target="_blank" rel="noreferrer">
                  playit dashboard
                </a>
                , add a tunnel: type <strong>Minecraft Java</strong>, agent <strong>this app&apos;s agent</strong>,
                local address <code>127.0.0.1</code>, local port <code>{server.port}</code>.
              </li>
              <li>Copy the address playit shows for it (like abc.joinmc.link) and paste it here.</li>
            </ol>
            <label>
              playit address
              <input
                placeholder="abc-def.joinmc.link"
                value={playitAddress}
                onChange={(e) => setPlayitAddress(e.target.value)}
                required
              />
            </label>
          </>
        )}
        {message && <p className={message.ok ? "mcsm-ok mcsm-small" : "mcsm-error mcsm-small"}>{message.text}</p>}
        <div className="mcsm-actions">
          {route && (
            <button
              type="button"
              className="mcsm-danger"
              disabled={busy}
              onClick={() => run(() => api.removePublicRoute(server.id), "Public address removed.")}
            >
              Remove address
            </button>
          )}
          <button type="submit" className="mcsm-primary" disabled={busy || (route && !changed)}>
            {busy ? "Working..." : route ? "Save" : "Create address"}
          </button>
        </div>
      </form>
    </div>
  );
}
