import { useState } from "react";
import "./servers.css";

async function call(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return resp.json();
}

const LOADER_NAMES = { forge: "Forge", neoforge: "NeoForge", fabric: "Fabric", quilt: "Quilt" };

/**
 * Paste a CurseForge modpack link (or upload the pack zip) and build a server from it,
 * without a CurseForge API key. Calls onCreate with the same shape the search page uses.
 */
export default function CurseForgeImport({ onClose, onCreate }) {
  const [link, setLink] = useState("");
  const [pack, setPack] = useState(null);
  const [fileId, setFileId] = useState("");
  const [serverPack, setServerPack] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  function show(p) {
    setPack(p);
    setFileId(p.selected_file_id);
    setServerPack(p.selected_has_server_pack);
  }

  async function lookUp(e) {
    e.preventDefault();
    setBusy("Looking up the pack…");
    setError("");
    try {
      show(
        await call("/api/imports/curseforge/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: link }),
        }),
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function upload(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(`Uploading ${file.name}…`);
    setError("");
    try {
      show(
        await call(`/api/imports/upload?filename=${encodeURIComponent(file.name)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/zip" },
          body: file,
        }),
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function pickFile(id) {
    setFileId(id);
    setServerPack(null);
    if (pack.project_id.startsWith("upload-")) return;
    try {
      const r = await call(`/api/imports/curseforge/${pack.project_id}/files/${id}/server-pack`);
      setServerPack(r.has_server_pack);
    } catch {
      /* unknown is fine */
    }
  }

  const file = pack?.files.find((f) => f.id === fileId);

  function create() {
    onCreate({
      source: "cfimport",
      projectId: pack.project_id,
      projectSlug: pack.slug,
      versionId: file.id,
      title: pack.title,
      minecraftVersion: file.minecraft_version,
      loader: file.loader,
      // Without a server pack, the client pack has to be cleaned of client-only mods.
      clientOnly: serverPack !== true,
    });
  }

  return (
    <div className="mcsm-backdrop" onClick={onClose}>
      <div className="mcsm-dialog mcsm-import" onClick={(e) => e.stopPropagation()}>
        <h2>Import from CurseForge</h2>
        <p className="mcsm-muted mcsm-small">
          Paste a modpack's CurseForge page link. No API key needed: the pack and its mods come from CurseForge's
          public download servers.
        </p>
        <form className="mcsm-import-link" onSubmit={lookUp}>
          <input
            value={link}
            onChange={(e) => setLink(e.target.value)}
            placeholder="https://www.curseforge.com/minecraft/modpacks/…"
            autoFocus
          />
          <button type="submit" className="mcsm-primary" disabled={!link.trim() || !!busy}>
            Look up
          </button>
        </form>
        <label className="mcsm-small mcsm-muted mcsm-import-upload">
          Or upload the pack zip you downloaded from CurseForge:
          <input type="file" accept=".zip,application/zip" onChange={upload} disabled={!!busy} />
        </label>

        {busy && <p className="mcsm-muted">⏳ {busy}</p>}
        {error && <p className="mcsm-error">{error}</p>}

        {pack && (
          <div className="mcsm-import-pack">
            <div className="mcsm-import-head">
              {pack.icon_url && <img src={pack.icon_url} alt="" width={48} height={48} />}
              <div>
                <strong>{pack.title}</strong>
                {pack.summary && <div className="mcsm-muted mcsm-small">{pack.summary}</div>}
              </div>
            </div>
            {pack.files.length > 1 ? (
              <label>
                Version
                <select value={fileId} onChange={(e) => pickFile(e.target.value)}>
                  {pack.files.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.display_name} · {f.minecraft_version} {LOADER_NAMES[f.loader] || f.loader}
                      {f.release_type !== "release" ? ` (${f.release_type})` : ""}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              file && (
                <div className="mcsm-small">
                  {file.display_name} · Minecraft {file.minecraft_version} · {LOADER_NAMES[file.loader] || file.loader}
                  {file.mod_count != null && ` · ${file.mod_count} mods`}
                </div>
              )
            )}
            {file && (
              <p className="mcsm-small mcsm-muted">
                {serverPack === true
                  ? "This version has a server pack from the pack's author, which will be installed."
                  : "This version has no server pack, so the server is built from the client pack: client-only " +
                    "mods are removed and the server is test-started until it runs."}
              </p>
            )}
          </div>
        )}

        <div className="mcsm-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="mcsm-primary" disabled={!file || !!busy} onClick={create}>
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
