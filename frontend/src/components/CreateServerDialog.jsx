import { useEffect, useState } from "react";
import { createServer, listServers } from "../api/servers.js";
import "./servers.css";

const JAVA_OPTIONS = [
  ["", "Auto (based on Minecraft version)"],
  ["java8", "Java 8"],
  ["java11", "Java 11"],
  ["java17", "Java 17"],
  ["java21", "Java 21"],
];

function nextFreePort(servers) {
  const used = new Set(servers.map((s) => s.port));
  let port = 25565;
  while (used.has(port)) port += 1;
  return port;
}

/**
 * Form for creating a server from a modpack (or a plain vanilla server).
 *
 * Props: open, onClose(), onCreated(server), and the pack being installed:
 * source ("modrinth" | "curseforge" | "vanilla"), projectId, projectSlug,
 * versionId, title, minecraftVersion, loader, and clientOnly (the pack has no
 * server version, so the server builder must strip client mods).
 */
export default function CreateServerDialog({
  open,
  onClose,
  onCreated,
  source = "vanilla",
  projectId = "",
  projectSlug = "",
  versionId = "",
  title = "",
  minecraftVersion = "",
  loader = "",
  clientOnly = false,
}) {
  const [name, setName] = useState(title);
  const [port, setPort] = useState(25565);
  const [memory, setMemory] = useState(source === "vanilla" ? 2 : 6);
  const [java, setJava] = useState("");
  const [mcVersion, setMcVersion] = useState(minecraftVersion);
  const [autoFix, setAutoFix] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setName(title || (source === "vanilla" ? "Vanilla server" : ""));
    setMcVersion(minecraftVersion);
    setMemory(source === "vanilla" ? 2 : 6);
    setJava("");
    setAutoFix(true);
    setError("");
    listServers()
      .then((servers) => setPort(nextFreePort(servers)))
      .catch(() => {});
  }, [open, title, source, minecraftVersion]);

  if (!open) return null;

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const server = await createServer({
        name,
        source,
        project_id: projectId,
        project_slug: projectSlug,
        version_id: versionId,
        pack_title: title,
        minecraft_version: mcVersion,
        loader,
        port: Number(port),
        memory_gb: Number(memory),
        java_version: java || null,
        auto_fix: source !== "vanilla" && (autoFix || clientOnly),
      });
      onCreated?.(server);
      onClose?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mcsm-backdrop" onClick={onClose}>
      <form className="mcsm-dialog" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h2>{clientOnly ? "Build server" : "Create server"}</h2>
        {source !== "vanilla" && (
          <p className="mcsm-muted">
            {title || projectSlug || projectId}
            {source === "custom" ? " (your custom pack)" : ` from ${source === "curseforge" ? "CurseForge" : "Modrinth"}`}
            {minecraftVersion && ` · Minecraft ${minecraftVersion}`}
            {loader && ` · ${loader}`}
          </p>
        )}

        <label>
          Server name
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={48} autoFocus />
        </label>

        {source === "vanilla" && (
          <label>
            Minecraft version
            <input value={mcVersion} onChange={(e) => setMcVersion(e.target.value)} placeholder="Latest" />
          </label>
        )}

        <div className="mcsm-row">
          <label>
            Port
            <input type="number" min={1024} max={65535} value={port} onChange={(e) => setPort(e.target.value)} required />
          </label>
          <label>
            RAM (GB)
            <input type="number" min={1} max={64} value={memory} onChange={(e) => setMemory(e.target.value)} required />
          </label>
        </div>

        <label>
          Java
          <select value={java} onChange={(e) => setJava(e.target.value)}>
            {JAVA_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>

        {source !== "vanilla" && (
          <label className="mcsm-check">
            <input
              type="checkbox"
              checked={autoFix || clientOnly}
              disabled={clientOnly}
              onChange={(e) => setAutoFix(e.target.checked)}
            />
            <span>
              Test and fix automatically: install the pack, remove client-only mods, test-start the server, and remove
              any mod that crashes it until it starts.
              {clientOnly && " Needed for this pack, because it is made for the client."}
            </span>
          </label>
        )}

        <p className="mcsm-muted mcsm-small">
          Players connect to <code>{window.location.hostname}:{port}</code>. The first start downloads the pack, which
          can take several minutes.
        </p>

        {error && <p className="mcsm-error">{error}</p>}

        <div className="mcsm-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="mcsm-primary" disabled={busy}>
            {busy ? "Creating..." : clientOnly ? "Build server" : "Create server"}
          </button>
        </div>
      </form>
    </div>
  );
}
