function compact(n) {
  return Intl.NumberFormat("en", { notation: "compact" }).format(n);
}

export default function PackCard({ pack, onCreate }) {
  const versions = pack.game_versions;
  const versionLabel =
    versions.length === 0 ? "" : versions.length === 1 ? versions[0] : `${versions[0]} – ${versions[versions.length - 1]}`;

  return (
    <article className="card">
      <div className="card-head">
        {pack.icon_url ? <img src={pack.icon_url} alt="" loading="lazy" /> : <div className="icon-placeholder" />}
        <div>
          <a href={pack.url} target="_blank" rel="noreferrer" className="title">
            {pack.title}
          </a>
          <div className="author">by {pack.author}</div>
        </div>
      </div>
      <p className="desc">{pack.description}</p>
      <div className="tags">
        {pack.loaders.map((l) => (
          <span key={l} className="tag loader">
            {l}
          </span>
        ))}
        {versionLabel && <span className="tag">{versionLabel}</span>}
      </div>
      <div className="card-foot">
        <span className="stats">⬇ {compact(pack.downloads)} · ♥ {compact(pack.follows)}</span>
        <button className="primary" onClick={onCreate}>
          Create server
        </button>
      </div>
    </article>
  );
}
