import { useEffect, useState } from "react";
import { api } from "../api.js";

// Lets the user choose which version of a pack to build a server from.
export default function VersionPicker({ pack, preferGameVersion, onPick, onClose }) {
  const [versions, setVersions] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState("");

  useEffect(() => {
    api
      .versions(pack.source, pack.id)
      .then((vs) => {
        setVersions(vs);
        const preferred = preferGameVersion && vs.find((v) => v.game_versions.includes(preferGameVersion));
        setSelected((preferred || vs[0])?.id ?? "");
      })
      .catch((e) => setError(e.message));
  }, [pack, preferGameVersion]);

  const version = versions?.find((v) => v.id === selected);

  return (
    <div className="backdrop" onClick={onClose}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <h2>{pack.title}</h2>
        {error && <p className="error">{error}</p>}
        {!versions && !error && <p>Loading versions…</p>}
        {versions && (
          <>
            <label>
              Pack version
              <select value={selected} onChange={(e) => setSelected(e.target.value)}>
                {versions.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name} ({v.game_versions.join(", ")} · {v.loaders.join(", ")})
                  </option>
                ))}
              </select>
            </label>
            <div className="dialog-actions">
              <button onClick={onClose}>Cancel</button>
              <button className="primary" disabled={!version} onClick={() => onPick(version)}>
                Continue
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
