// Client for the /api/servers endpoints.

async function request(path, options = {}) {
  const res = await fetch(`/api/servers${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail))
        detail = body.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join(", ");
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

export const listServers = () => request("");
export const getServer = (id) => request(`/${encodeURIComponent(id)}`);
export const createServer = (body) => request("", { method: "POST", body: JSON.stringify(body) });
export const startServer = (id) => request(`/${encodeURIComponent(id)}/start`, { method: "POST" });
export const stopServer = (id) => request(`/${encodeURIComponent(id)}/stop`, { method: "POST" });
export const recreateServer = (id) => request(`/${encodeURIComponent(id)}/recreate`, { method: "POST" });
export const restartServer = (id) => request(`/${encodeURIComponent(id)}/restart`, { method: "POST" });
export const deleteServer = (id, deleteData = false) =>
  request(`/${encodeURIComponent(id)}?delete_data=${deleteData}`, { method: "DELETE" });
export const getBuild = (id) => request(`/${encodeURIComponent(id)}/build`);
export const getLogs = (id, tail = 300) => request(`/${encodeURIComponent(id)}/logs?tail=${tail}`);

// Shell helpers and pack modes
export const prepareScript = (id) => request(`/${encodeURIComponent(id)}/shell/prepare`, { method: "POST" });
export const getPackMode = (id) => request(`/${encodeURIComponent(id)}/pack-mode`);
export const switchPackMode = (id, mode) =>
  request(`/${encodeURIComponent(id)}/pack-mode`, { method: "POST", body: JSON.stringify({ mode }) });

// World backups
const backups = (id) => `/${encodeURIComponent(id)}/backups`;
export const listBackups = (id) => request(backups(id));
export const createBackup = (id, scope = "") =>
  request(backups(id), { method: "POST", body: JSON.stringify({ scope }) });
export const saveBackupSchedule = (id, schedule) =>
  request(`${backups(id)}/schedule`, { method: "PUT", body: JSON.stringify(schedule) });
export const restoreBackup = (id, backupId) =>
  request(`${backups(id)}/${encodeURIComponent(backupId)}/restore`, { method: "POST" });
export const deleteBackup = (id, backupId) =>
  request(`${backups(id)}/${encodeURIComponent(backupId)}`, { method: "DELETE" });
export const backupDownloadUrl = (id, backupId) =>
  `/api/servers${backups(id)}/${encodeURIComponent(backupId)}/download`;

// ---- Management tools (console, players, properties, settings, stats, mods) ----
const sid = (id) => `/${encodeURIComponent(id)}`;
const json = (method, body) => ({ method, body: JSON.stringify(body) });
export const sendCommand = (id, command) => request(`${sid(id)}/console`, json("POST", { command }));
export const getPlayers = (id) => request(`${sid(id)}/players`);
export const playerAction = (id, action, name, reason = "") =>
  request(`${sid(id)}/players`, json("POST", { action, name, reason }));
export const setWhitelist = (id, enabled) => request(`${sid(id)}/whitelist`, json("PUT", { enabled }));
export const getProperties = (id) => request(`${sid(id)}/properties`);
export const saveProperties = (id, properties, restart = false) =>
  request(`${sid(id)}/properties`, json("PUT", { properties, restart }));
export const updateSettings = (id, settings) => request(`${sid(id)}/settings`, json("PATCH", settings));
export const getStats = (id) => request(`${sid(id)}/stats`);
export const listMods = (id) => request(`${sid(id)}/mods`);
export const uploadMod = (id, file) =>
  request(`${sid(id)}/mods?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/java-archive" },
    body: file,
  });
export const setModEnabled = (id, file, enabled) =>
  request(`${sid(id)}/mods/${encodeURIComponent(file)}/${enabled ? "enable" : "disable"}`, { method: "POST" });
export const deleteMod = (id, file) => request(`${sid(id)}/mods/${encodeURIComponent(file)}`, { method: "DELETE" });

// ---- Modpack updates ----
export const listUpdates = (refresh = false) =>
  fetch(`/api/updates?refresh=${refresh}`).then(async (r) => {
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
    return r.json();
  });
export const getServerUpdates = (id, refresh = false) => request(`${sid(id)}/updates?refresh=${refresh}`);
export const linkPack = (id, url) => request(`${sid(id)}/updates/link`, json("PUT", { url }));
export const startUpdate = (id, versionId) =>
  request(`${sid(id)}/updates`, json("POST", { version_id: versionId }));
