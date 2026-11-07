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
export const restartServer = (id) => request(`/${encodeURIComponent(id)}/restart`, { method: "POST" });
export const deleteServer = (id, deleteData = false) =>
  request(`/${encodeURIComponent(id)}?delete_data=${deleteData}`, { method: "DELETE" });
export const getBuild = (id) => request(`/${encodeURIComponent(id)}/build`);
export const getLogs = (id, tail = 300) => request(`/${encodeURIComponent(id)}/logs?tail=${tail}`);

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
