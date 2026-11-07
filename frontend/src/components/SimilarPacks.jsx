import { useEffect, useState } from "react";
import { api } from "../api.js";
import PackCard from "./PackCard.jsx";

const PAGE_SIZE = 24;
const SORTS = [
  ["similarity", "Most similar"],
  ["shared", "Most shared mods"],
];

// Packs that share mods with `pack`, ranked by the backend's local mod index.
// The MC version and loader filters come from the search sidebar.
export default function SimilarPacks({ pack, gameVersion, loader, onBack, onCreate, onSimilar }) {
  const [sort, setSort] = useState("similarity");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => setOffset(0), [pack, gameVersion, loader, sort]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    api
      .similar(pack.source, pack.id, { game_version: gameVersion, loader, sort, offset, limit: PAGE_SIZE })
      .then((r) => !cancelled && setData(r))
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [pack, gameVersion, loader, sort, offset, tick]);

  // While the index is being built, refresh the results every few seconds.
  const running = data?.index.state === "running";
  useEffect(() => {
    if (!running) return;
    const t = setTimeout(() => setTick((n) => n + 1), 4000);
    return () => clearTimeout(t);
  }, [running, data]);

  const index = data?.index;
  const pages = data ? Math.ceil(data.total / PAGE_SIZE) : 0;
  const page = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <section className="results">
      <div className="similar-head">
        <button onClick={onBack}>← Back to search</button>
        <div className="similar-title">
          {pack.icon_url ? <img src={pack.icon_url} alt="" /> : <div className="icon-placeholder" />}
          <div>
            <h2>Packs similar to {pack.title}</h2>
            <div className="muted">
              {data ? `${data.mod_count} mods compared` : "Reading its mod list…"}
              {(gameVersion || loader) && ` · filtered to ${[gameVersion, loader].filter(Boolean).join(" ")}`}
            </div>
          </div>
        </div>
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          {SORTS.map(([v, label]) => (
            <option key={v} value={v}>
              {label}
            </option>
          ))}
        </select>
      </div>

      {index && <IndexBar index={index} onRefreshed={() => setTick((n) => n + 1)} />}

      <p className="count">
        {loading && !data ? "Comparing mod lists…" : data && `${data.total.toLocaleString()} packs share mods`}
        {error && <span className="error"> · {error}</span>}
      </p>
      {data && data.mod_count === 0 && !loading && (
        <p className="muted">
          No mods could be read from this pack's latest version, so there is nothing to compare yet.
        </p>
      )}

      <div className="grid">
        {data?.results.map((r) => (
          <PackCard
            key={r.pack.id}
            pack={r.pack}
            onCreate={() => onCreate(r.pack)}
            onSimilar={() => onSimilar(r.pack)}
          >
            <SharedMods result={r} total={data.mod_count} />
          </PackCard>
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
  );
}

function SharedMods({ result, total }) {
  const [open, setOpen] = useState(false);
  const pct = Math.round(result.score * 100);
  const shown = open ? result.shared_mods : result.shared_mods.slice(0, 6);
  return (
    <div className="shared">
      <div className="shared-score">
        <div className="meter" title="Similarity, weighted so rare mods count more than common libraries">
          <span style={{ width: `${Math.max(pct, 2)}%` }} />
        </div>
        <span>
          <strong>{pct}%</strong> match · {result.shared_count} of {total} mods shared
        </span>
      </div>
      <div className="shared-mods">
        {shown.map((m) => (
          <a
            key={m.id}
            className="tag"
            href={m.slug ? `https://modrinth.com/mod/${m.slug}` : undefined}
            target="_blank"
            rel="noreferrer"
          >
            {m.title}
          </a>
        ))}
        {result.shared_mods.length > 6 && (
          <button className="link" onClick={() => setOpen(!open)}>
            {open ? "Show fewer" : `+${result.shared_mods.length - 6} more`}
          </button>
        )}
      </div>
    </div>
  );
}

function IndexBar({ index, onRefreshed }) {
  const [busy, setBusy] = useState(false);
  if (index.state === "running") {
    const pct = index.total ? Math.round((index.done / index.total) * 100) : 0;
    return (
      <div className="index-bar">
        <div>
          Building the mod index: {index.message} {index.total > 0 && `${index.done} of ${index.total} packs`}
          <span className="muted"> · results fill in as it goes</span>
        </div>
        <div className="meter">
          <span style={{ width: `${pct}%` }} />
        </div>
      </div>
    );
  }
  const last = index.last_refresh ? new Date(index.last_refresh * 1000).toLocaleDateString() : "never";
  return (
    <div className="index-bar muted">
      {index.state === "error" && <div className="error">{index.message}</div>}
      Comparing against {index.indexed.toLocaleString()} indexed packs (updated {last}).{" "}
      <button
        className="link"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          api
            .refreshSimilar()
            .then(onRefreshed)
            .finally(() => setBusy(false));
        }}
      >
        Refresh index
      </button>
    </div>
  );
}
