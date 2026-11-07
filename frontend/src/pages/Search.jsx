import { useEffect, useState } from "react";
import { api } from "../api.js";
import PackCard from "../components/PackCard.jsx";
import SimilarPacks from "../components/SimilarPacks.jsx";
import VersionPicker from "../components/VersionPicker.jsx";

const SORTS = [
  ["relevance", "Relevance"],
  ["downloads", "Downloads"],
  ["follows", "Follows"],
  ["updated", "Recently updated"],
  ["newest", "Newest"],
];
const PAGE_SIZE = 24;

export default function Search({ onCreate, onOpenSettings }) {
  const [sources, setSources] = useState(["modrinth"]);
  const [source, setSource] = useState("modrinth");
  const [options, setOptions] = useState({ game_versions: [], loaders: [], categories: [] });
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [gameVersion, setGameVersion] = useState("");
  const [loader, setLoader] = useState("");
  const [category, setCategory] = useState("");
  const [sort, setSort] = useState("relevance");
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState({ hits: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [picking, setPicking] = useState(null);
  // When set, the results area shows packs that share mods with this pack.
  const [similarTo, setSimilarTo] = useState(null);

  useEffect(() => {
    api.sources().then(setSources).catch(() => {});
  }, []);

  useEffect(() => {
    api.filters(source).then(setOptions).catch((e) => setError(e.message));
  }, [source]);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 300);
    return () => clearTimeout(t);
  }, [query]);

  // Go back to page 1 whenever a filter changes.
  useEffect(() => setOffset(0), [source, debounced, gameVersion, loader, category, sort]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    api
      .search({ source, q: debounced, game_version: gameVersion, loader, category, sort, offset, limit: PAGE_SIZE })
      .then((r) => !cancelled && setResult(r))
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [source, debounced, gameVersion, loader, category, sort, offset]);

  const pages = Math.ceil(result.total / PAGE_SIZE);
  const page = Math.floor(offset / PAGE_SIZE) + 1;

  function clearFilters() {
    setQuery("");
    setGameVersion("");
    setLoader("");
    setCategory("");
    setSort("relevance");
  }

  return (
    <div className="search">
      <aside className="filters">
        {sources.length > 1 && (
          <section>
            <h3>Source</h3>
            <div className="chips">
              {sources.map((s) => (
                <Chip key={s} active={source === s} onClick={() => setSource(s)}>
                  {s}
                </Chip>
              ))}
            </div>
          </section>
        )}
        <section>
          <h3>Minecraft version</h3>
          <select value={gameVersion} onChange={(e) => setGameVersion(e.target.value)}>
            <option value="">Any version</option>
            {options.game_versions.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </section>
        <section>
          <h3>Mod loader</h3>
          <div className="chips">
            {options.loaders.map((l) => (
              <Chip key={l} active={loader === l} onClick={() => setLoader(loader === l ? "" : l)}>
                {l}
              </Chip>
            ))}
          </div>
        </section>
        <section>
          <h3>Category</h3>
          <div className="chips">
            {options.categories.map((c) => (
              <Chip key={c} active={category === c} onClick={() => setCategory(category === c ? "" : c)}>
                {c.replace(/-/g, " ")}
              </Chip>
            ))}
          </div>
        </section>
        <button className="link" onClick={clearFilters}>
          Clear filters
        </button>
      </aside>

      {similarTo && (
        <SimilarPacks
          pack={similarTo}
          gameVersion={gameVersion}
          loader={loader}
          onBack={() => setSimilarTo(null)}
          onCreate={setPicking}
          onSimilar={(p) => {
            setSimilarTo(p);
            window.scrollTo(0, 0);
          }}
        />
      )}
      <section className="results" hidden={similarTo !== null}>
        <div className="toolbar">
          <input
            className="query"
            type="search"
            placeholder="Search modpacks…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            {SORTS.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <p className="count">
          {loading ? "Searching…" : `${result.total.toLocaleString()} modpacks`}
          {error && <span className="error"> · {error}</span>}
          {error && source === "curseforge" && onOpenSettings && (
            <>
              {" "}
              <button className="link" onClick={onOpenSettings}>
                Open Settings
              </button>
            </>
          )}
        </p>
        <div className="grid">
          {result.hits.map((pack) => (
            <PackCard
              key={`${pack.source}:${pack.id}`}
              pack={pack}
              onCreate={() => setPicking(pack)}
              onSimilar={() => {
                setSimilarTo(pack);
                window.scrollTo(0, 0);
              }}
            />
          ))}
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

      {picking && (
        <VersionPicker
          pack={picking}
          preferGameVersion={gameVersion}
          onClose={() => setPicking(null)}
          onPick={(version) => {
            setPicking(null);
            onCreate({
              source: picking.source,
              projectId: picking.id,
              projectSlug: picking.slug,
              versionId: version.id,
              title: picking.title,
              minecraftVersion: version.game_versions?.[0] || "",
              loader: version.loaders?.[0] || "",
            });
          }}
        />
      )}
    </div>
  );
}

function Chip({ active, onClick, children }) {
  return (
    <button className={active ? "chip active" : "chip"} onClick={onClick}>
      {children}
    </button>
  );
}
