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

export const api = {
  sources: () => get("/api/sources"),
  filters: (source) => get("/api/filters", { source }),
  search: (params) => get("/api/search", params),
  versions: (source, projectId) => get(`/api/packs/${source}/${encodeURIComponent(projectId)}/versions`),
  settings: () => get("/api/settings"),
  saveCurseForgeKey: (apiKey) => send("/api/settings/curseforge", "PUT", { api_key: apiKey }),
  testCurseForgeKey: () => send("/api/settings/curseforge/test", "POST"),
};
