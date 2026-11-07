import { useEffect, useState } from "react";
import { api, packsApi } from "../api.js";
import "./packs.css";

const LOADERS = ["fabric", "forge", "neoforge", "quilt"];
const SORTS = [
  ["relevance", "Relevance"],
  ["downloads", "Downloads"],
  ["follows", "Follows"],
  ["updated", "Recently updated"],
];
const PAGE_SIZE = 20;

function compact(n) {
  return Intl.NumberFormat("en", { notation: "compact" }).format(n);
}

/** Pick a pack to edit, or start a new one. */
export default function PackBuilder({ onCreate, onOpenSettings }) {
  const [packId, setPackId] = useState(null);
  if (packId) {
    return (
      <PackEditor packId={packId} onBack={() => setPackId(null)} onCreate={onCreate} onOpenSettings={onOpenSettings} />
    );
  }
  return <PackList onOpen={setPackId} />;
}

function PackList({ onOpen }) {
  const [packs, setPacks] = useState(null);
  const [versions, setVersions] = useState([]);
  const [name, setName] = useState("");
  const [mc, setMc] = useState("");
  const [loader, setLoader] = useState("fabric");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const reload = () =>
    packsApi
      .list()
      .then(setPacks)
      .catch((e) => setError(e.message));

  useEffect(() => {
    reload();
    api
      .filters("modrinth")
      .then((f) => {
        setVersions(f.game_versions);
        setMc((v) => v || f.game_versions[0] || "");
      })
      .catch(() => {});
  }, []);

  async function create(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const pack = await packsApi.create({ name, minecraft_version: mc, loader });
      onOpen(pack.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(pack) {
    if (!window.confirm(`Delete the pack "${pack.name}"? Servers made from it keep running.`)) return;
    await packsApi.remove(pack.id).catch((e) => setError(e.message));
    reload();
  }

  return (
    <div className="pb-list">
      <form className="panel pb-new" onSubmit={create}>
        <h2>New custom pack</h2>
        <p className="muted">
          Pick a Minecraft version and loader, then browse Modrinth and CurseForge and add mods. Dependencies are added
          for you. When it's ready, create a server from it or export it for the Modrinth App, CurseForge app, GDLauncher
          or Prism.
        </p>
        <div className="pb-new-row">
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={64} placeholder="My pack" />
          </label>
          <label>
            Minecraft
            <select value={mc} onChange={(e) => setMc(e.target.value)} required>
              {versions.map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
          <label>
            Loader
            <select value={loader} onChange={(e) => setLoader(e.target.value)}>
              {LOADERS.map((l) => (
                <option key={l}>{l}</option>
              ))}
            </select>
          </label>
          <button className="primary" disabled={busy || !mc}>
            {busy ? "Creating…" : "Create pack"}
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </form>

      <h2>Your packs</h2>
      {packs === null && <p className="muted">Loading…</p>}
      {packs?.length === 0 && <p className="muted">No packs yet.</p>}
      <div className="pb-packs">
        {packs?.map((p) => (
          <article key={p.id} className="card">
            <div>
              <button className="link title" onClick={() => onOpen(p.id)}>
                {p.name}
              </button>
              <div className="author">
                Minecraft {p.minecraft_version} · {p.loader} · {p.mods.length} mods
              </div>
            </div>
            {p.issues.some((i) => i.severity === "error") && <span className="error">Has problems</span>}
            <div className="card-foot">
              <button className="link pb-danger" onClick={() => remove(p)}>
                Delete
              </button>
              <button className="primary" onClick={() => onOpen(p.id)}>
                Open
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

function PackEditor({ packId, onBack, onCreate, onOpenSettings }) {
  const [pack, setPack] = useState(null);
  const [mcVersions, setMcVersions] = useState([]);
  const [loaderVersions, setLoaderVersions] = useState([]);
  const [message, setMessage] = useState(null); // { kind: "ok" | "error", lines: [] }
  const [busy, setBusy] = useState("");

  useEffect(() => {
    packsApi
      .get(packId)
      .then(setPack)
      .catch((e) => setMessage({ kind: "error", lines: [e.message] }));
    api
      .filters("modrinth")
      .then((f) => setMcVersions(f.game_versions))
      .catch(() => {});
  }, [packId]);

  useEffect(() => {
    if (!pack) return;
    packsApi
      .loaderVersions(pack.loader, pack.minecraft_version)
      .then(setLoaderVersions)
      .catch(() => setLoaderVersions([]));
  }, [pack?.loader, pack?.minecraft_version]);

  if (!pack) {
    return (
      <div>
        <button className="link" onClick={onBack}>
          ← All packs
        </button>
        {message && <p className="error">{message.lines.join(" ")}</p>}
      </div>
    );
  }

  async function run(label, fn) {
    setBusy(label);
    setMessage(null);
    try {
      await fn();
    } catch (e) {
      setMessage({ kind: "error", lines: [e.message] });
    } finally {
      setBusy("");
    }
  }

  const update = (changes) =>
    run("Saving…", async () => {
      const r = await packsApi.update(pack.id, changes);
      setPack(r.pack);
      const lines = [];
      if (r.updated.length) lines.push(`Switched versions for: ${r.updated.join(", ")}.`);
      if (r.unavailable.length)
        lines.push(`No version for the new target, left as they were: ${r.unavailable.join(", ")}.`);
      if (lines.length) setMessage({ kind: r.unavailable.length ? "error" : "ok", lines });
    });

  function retarget(changes) {
    if (
      pack.mods.length &&
      !window.confirm("Changing the Minecraft version or loader re-picks a version of every mod. Continue?")
    )
      return;
    update(changes);
  }

  const addMod = (hit) =>
    run(`Adding ${hit.title}…`, async () => {
      const r = await packsApi.addMod(pack.id, { source: hit.source, project_id: hit.id });
      setPack(r.pack);
      const lines = [`Added ${r.added.join(", ")}.`, ...r.notes];
      setMessage({ kind: "ok", lines });
    });

  const removeMod = (m) =>
    run(`Removing ${m.title}…`, async () => {
      setPack(await packsApi.removeMod(pack.id, m.source, m.project_id));
    });

  const exportPack = (format) =>
    run("Building export…", async () => {
      const { name, warnings } = await packsApi.exportPack(pack.id, format);
      setMessage({ kind: "ok", lines: [`Downloaded ${name}.`, ...warnings] });
    });

  const errors = pack.issues.filter((i) => i.severity === "error");
  const warnings = pack.issues.filter((i) => i.severity === "warning");
  const issuesFor = (m) => pack.issues.filter((i) => i.mod === `${m.source}:${m.project_id}` && i.severity !== "info");
  const inPack = new Set(pack.mods.map((m) => `${m.source}:${m.project_id}`));
  const slugs = new Set(pack.mods.map((m) => m.slug));
  const userMods = pack.mods.filter((m) => !m.added_as_dependency);
  const deps = pack.mods.filter((m) => m.added_as_dependency);

  return (
    <div className="pb-editor">
      <div className="pb-head">
        <button className="link" onClick={onBack}>
          ← All packs
        </button>
        <input
          className="pb-name"
          defaultValue={pack.name}
          key={`name-${pack.name}`}
          onBlur={(e) => e.target.value.trim() && e.target.value !== pack.name && update({ name: e.target.value })}
        />
        <div className="pb-actions">
          <button disabled={!!busy || !pack.mods.length} onClick={() => exportPack("mrpack")}>
            Export .mrpack
          </button>
          <button disabled={!!busy || !pack.mods.length} onClick={() => exportPack("curseforge")}>
            Export CurseForge zip
          </button>
          <button
            className="primary"
            disabled={!!busy || errors.length > 0}
            title={errors.length ? "Fix the problems below first" : ""}
            onClick={() =>
              onCreate({
                source: "custom",
                projectId: pack.id,
                title: pack.name,
                minecraftVersion: pack.minecraft_version,
                loader: pack.loader,
              })
            }
          >
            Create server
          </button>
        </div>
      </div>

      <div className="pb-target">
        <label>
          Minecraft
          <select
            value={pack.minecraft_version}
            disabled={!!busy}
            onChange={(e) => retarget({ minecraft_version: e.target.value })}
          >
            {!mcVersions.includes(pack.minecraft_version) && <option>{pack.minecraft_version}</option>}
            {mcVersions.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </label>
        <label>
          Loader
          <select value={pack.loader} disabled={!!busy} onChange={(e) => retarget({ loader: e.target.value })}>
            {LOADERS.map((l) => (
              <option key={l}>{l}</option>
            ))}
          </select>
        </label>
        <label>
          Loader version
          <select
            value={pack.loader_version}
            disabled={!!busy}
            onChange={(e) => update({ loader_version: e.target.value })}
          >
            {!loaderVersions.includes(pack.loader_version) && <option>{pack.loader_version}</option>}
            {loaderVersions.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </label>
        <label>
          Pack version
          <input
            defaultValue={pack.version}
            key={`ver-${pack.version}`}
            onBlur={(e) => e.target.value.trim() && e.target.value !== pack.version && update({ version: e.target.value })}
          />
        </label>
        <label>
          Author
          <input
            defaultValue={pack.author}
            key={`author-${pack.author}`}
            onBlur={(e) => e.target.value !== pack.author && update({ author: e.target.value })}
          />
        </label>
      </div>

      {busy && <p className="muted">{busy}</p>}
      {message && (
        <div className={message.kind === "error" ? "pb-msg pb-msg-error" : "pb-msg"}>
          {message.lines.map((l, i) => (
            <div key={i}>{l}</div>
          ))}
        </div>
      )}
      {(errors.length > 0 || warnings.length > 0) && (
        <div className="pb-issues">
          {errors.map((i, n) => (
            <div key={`e${n}`} className="error">
              ✖ {i.message}
            </div>
          ))}
          {warnings.map((i, n) => (
            <div key={`w${n}`} className="pb-warn">
              ⚠ {i.message}
            </div>
          ))}
        </div>
      )}

      <div className="pb-columns">
        <section className="panel pb-mods">
          <h3>Mods in this pack ({pack.mods.length})</h3>
          {pack.mods.length === 0 && <p className="muted">Nothing yet. Search for mods on the right and add them.</p>}
          {userMods.map((m) => (
            <ModRow key={`${m.source}:${m.project_id}`} mod={m} issues={issuesFor(m)} onRemove={() => removeMod(m)} busy={!!busy} />
          ))}
          {deps.length > 0 && <h4 className="muted">Added as dependencies</h4>}
          {deps.map((m) => (
            <ModRow key={`${m.source}:${m.project_id}`} mod={m} issues={issuesFor(m)} onRemove={() => removeMod(m)} busy={!!busy} />
          ))}
        </section>
        <ModBrowser pack={pack} inPack={inPack} slugs={slugs} onAdd={addMod} busy={!!busy} onOpenSettings={onOpenSettings} />
      </div>
    </div>
  );
}

function SideTags({ client, server }) {
  return (
    <>
      {server === "unsupported" && <span className="tag pb-tag-client">client only</span>}
      {client === "unsupported" && <span className="tag pb-tag-server">server only</span>}
    </>
  );
}

function ModRow({ mod, issues, onRemove, busy }) {
  return (
    <div className="pb-mod">
      {mod.icon_url ? <img src={mod.icon_url} alt="" loading="lazy" /> : <div className="icon-placeholder" />}
      <div className="pb-mod-main">
        <a href={mod.url} target="_blank" rel="noreferrer" className="title">
          {mod.title}
        </a>
        <div className="pb-mod-meta">
          <span className="muted">{mod.version_number}</span>
          <span className="tag">{mod.source}</span>
          <SideTags client={mod.client_side} server={mod.server_side} />
        </div>
        {issues.map((i, n) => (
          <div key={n} className={i.severity === "error" ? "error pb-small" : "pb-warn pb-small"}>
            {i.message}
          </div>
        ))}
      </div>
      <button className="link pb-danger" disabled={busy} onClick={onRemove} title="Remove from pack">
        Remove
      </button>
    </div>
  );
}

function ModBrowser({ pack, inPack, slugs, onAdd, busy, onOpenSettings }) {
  const [source, setSource] = useState("modrinth");
  const [categories, setCategories] = useState([]);
  const [category, setCategory] = useState("");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [sort, setSort] = useState("relevance");
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState({ hits: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setCategory("");
    packsApi
      .modFilters(source)
      .then((f) => setCategories(f.categories))
      .catch(() => setCategories([]));
  }, [source]);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 300);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => setOffset(0), [source, debounced, category, sort, pack.minecraft_version, pack.loader]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    packsApi
      .searchMods({
        source,
        q: debounced,
        game_version: pack.minecraft_version,
        loader: pack.loader,
        category,
        sort,
        offset,
        limit: PAGE_SIZE,
      })
      .then((r) => !cancelled && setResult(r))
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [source, debounced, category, sort, offset, pack.minecraft_version, pack.loader]);

  const pages = Math.ceil(result.total / PAGE_SIZE);
  const page = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <section className="panel pb-browser">
      <h3>
        Add mods <span className="muted pb-small">for Minecraft {pack.minecraft_version} · {pack.loader}</span>
      </h3>
      <div className="chips">
        {["modrinth", "curseforge"].map((s) => (
          <button key={s} className={source === s ? "chip active" : "chip"} onClick={() => setSource(s)}>
            {s}
          </button>
        ))}
      </div>
      <div className="pb-search-row">
        <input type="search" placeholder="Search mods…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">All categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c.replace(/-/g, " ")}
            </option>
          ))}
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          {SORTS.map(([v, label]) => (
            <option key={v} value={v}>
              {label}
            </option>
          ))}
        </select>
      </div>
      <p className="count">
        {loading ? "Searching…" : `${result.total.toLocaleString()} mods`}
        {error && <span className="error"> · {error}</span>}
        {error && source === "curseforge" && (
          <>
            {" "}
            <button className="link" onClick={onOpenSettings}>
              Open Settings
            </button>
          </>
        )}
      </p>
      <div className="pb-results">
        {result.hits.map((hit) => {
          const added = inPack.has(`${hit.source}:${hit.id}`) || slugs.has(hit.slug);
          return (
            <div key={`${hit.source}:${hit.id}`} className="pb-mod">
              {hit.icon_url ? <img src={hit.icon_url} alt="" loading="lazy" /> : <div className="icon-placeholder" />}
              <div className="pb-mod-main">
                <a href={hit.url} target="_blank" rel="noreferrer" className="title">
                  {hit.title}
                </a>
                <span className="muted pb-small"> by {hit.author}</span>
                <div className="pb-desc">{hit.description}</div>
                <div className="pb-mod-meta">
                  <span className="muted pb-small">⬇ {compact(hit.downloads)}</span>
                  <SideTags client={hit.client_side} server={hit.server_side} />
                </div>
              </div>
              <button className={added ? "" : "primary"} disabled={added || busy} onClick={() => onAdd(hit)}>
                {added ? "Added" : "Add"}
              </button>
            </div>
          );
        })}
      </div>
      {pages > 1 && (
        <div className="pager">
          <button disabled={page <= 1} onClick={() => setOffset(offset - PAGE_SIZE)}>
            ← Prev
          </button>
          <span>
            Page {page} of {pages}
          </span>
          <button disabled={page >= pages} onClick={() => setOffset(offset + PAGE_SIZE)}>
            Next →
          </button>
        </div>
      )}
    </section>
  );
}
