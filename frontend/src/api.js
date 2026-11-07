// Thin wrappers around the backend API. Add new endpoints here.
async function get(path, params = {}) {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""),
  );
  const url = qs.toString() ? `${path}?${qs}` : path;
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
  return resp.json();
}

export const api = {
  sources: () => get("/api/sources"),
  filters: (source) => get("/api/filters", { source }),
  search: (params) => get("/api/search", params),
  versions: (source, projectId) => get(`/api/packs/${source}/${encodeURIComponent(projectId)}/versions`),
};
