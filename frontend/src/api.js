// Thin wrappers around the backend API. Add new endpoints here.
async function get(path, params = {}) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""),
  );
  const url = qs.toString() ? `${path}?${qs}` : path;
  return send(url);
}

async function send(url, method = "GET", body) {
  const resp = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return resp.json();
}

// Fetch a file download and save it in the browser. Returns the export warnings.
async function download(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  const disposition = resp.headers.get("content-disposition") || "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] || "pack.zip";
  let warnings = [];
  try {
    warnings = JSON.parse(resp.headers.get("x-pack-warnings") || "[]");
  } catch {
    /* ignore */
  }
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  return { name, warnings };
}

const pack = (id) => `/api/custom-packs/${encodeURIComponent(id)}`;

export const packsApi = {
  modFilters: (source) => get("/api/mods/filters", { source }),
  searchMods: (params) => get("/api/mods/search", params),
  loaderVersions: (loader, mc) => get(`/api/loaders/${loader}/versions`, { minecraft_version: mc }),
  list: () => get("/api/custom-packs"),
  get: (id) => get(pack(id)),
  create: (body) => send("/api/custom-packs", "POST", body),
  update: (id, body) => send(pack(id), "PUT", body),
  remove: async (id) => {
    const resp = await fetch(pack(id), { method: "DELETE" });
    if (!resp.ok) throw new Error(resp.statusText);
  },
  addMod: (id, body) => send(`${pack(id)}/mods`, "POST", body),
  removeMod: (id, source, projectId) =>
    send(`${pack(id)}/mods/${source}/${encodeURIComponent(projectId)}`, "DELETE"),
  exportPack: (id, format) => download(`${pack(id)}/export/${format}`),
};

export const api = {
  sources: () => get("/api/sources"),
  filters: (source) => get("/api/filters", { source }),
  search: (params) => get("/api/search", params),
  versions: (source, projectId) => get(`/api/packs/${source}/${encodeURIComponent(projectId)}/versions`),
  similar: (source, packId, params) => get(`/api/similar/${source}/${encodeURIComponent(packId)}`, params),
  similarStatus: () => get("/api/similar/status"),
  refreshSimilar: () => send("/api/similar/refresh", "POST"),
  settings: () => get("/api/settings"),
  saveCurseForgeKey: (apiKey) => send("/api/settings/curseforge", "PUT", { api_key: apiKey }),
  testCurseForgeKey: () => send("/api/settings/curseforge/test", "POST"),
};
