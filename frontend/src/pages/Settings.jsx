import { useEffect, useState } from "react";
import { api } from "../api.js";

const SOURCE_LABELS = {
  settings: "saved here",
  env: "from the CF_API_KEY container variable",
};

export default function Settings() {
  const [cf, setCf] = useState(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    api
      .settings()
      .then((s) => setCf(s.curseforge))
      .catch((e) => setMessage({ error: true, text: e.message }));
  }, []);

  async function run(action, successText) {
    setBusy(true);
    setMessage(null);
    try {
      const status = await action();
      setCf(status);
      setMessage({ error: false, text: successText(status) });
      setKey("");
    } catch (e) {
      setMessage({ error: true, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  const save = (e) => {
    e.preventDefault();
    run(
      () => api.saveCurseForgeKey(key),
      (s) =>
        s.verified === null
          ? "Saved, but CurseForge couldn't be reached to check the key."
          : "Saved. CurseForge accepted the key.",
    );
  };
  const clear = () =>
    run(
      () => api.saveCurseForgeKey(""),
      (s) => (s.configured ? "Removed the saved key. Using the CF_API_KEY variable instead." : "Removed the key."),
    );
  const test = () =>
    run(
      () => api.testCurseForgeKey(),
      (s) => (s.verified ? "CurseForge accepted the key." : "CurseForge rejected the key."),
    );

  return (
    <div className="settings">
      <h1>Settings</h1>
      <section className="panel">
        <h2>CurseForge API key</h2>
        <p className="muted">
          CurseForge needs an API key to search modpacks and to install them on servers. Modrinth works without one.
          Apply for a free key at{" "}
          <a href="https://console.curseforge.com" target="_blank" rel="noreferrer">
            console.curseforge.com
          </a>
          ; approval can take a few days.
        </p>
        {cf && (
          <p>
            Status:{" "}
            {cf.configured ? (
              <strong>
                Key set {cf.key_hint && <code>{cf.key_hint}</code>} ({SOURCE_LABELS[cf.key_source] ?? cf.key_source})
              </strong>
            ) : (
              <strong>No key yet</strong>
            )}
          </p>
        )}
        <form className="key-form" onSubmit={save}>
          <input
            type="password"
            autoComplete="off"
            placeholder={cf?.configured ? "Paste a new key to replace it" : "Paste your API key"}
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
          <button className="primary" type="submit" disabled={busy || !key.trim()}>
            Save
          </button>
          {cf?.configured && (
            <button type="button" disabled={busy} onClick={test}>
              Test
            </button>
          )}
          {cf?.key_source === "settings" && (
            <button type="button" disabled={busy} onClick={clear}>
              Remove
            </button>
          )}
        </form>
        {message && <p className={message.error ? "error" : "ok"}>{message.text}</p>}
      </section>
      <KnownMods />
    </div>
  );
}

const DB_SOURCES = {
  remote: "up to date from GitHub",
  cache: "last copy downloaded from GitHub",
  bundled: "the copy built into this app",
};

function KnownMods() {
  const [db, setDb] = useState(null);
  const [error, setError] = useState("");

  const load = () =>
    api
      .modDb()
      .then(setDb)
      .catch((e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  async function forget(id) {
    try {
      await api.forgetLearnedMod(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <section className="panel">
      <h2>Client-only and problem mods</h2>
      <p className="muted">
        When building a server from a modpack, the app removes mods on this list before the first test start, and
        removes any mod that crashes the server while testing.
      </p>
      {error && <p className="error">{error}</p>}
      {db && (
        <>
          <p>
            Shared list: <strong>{db.shared_count} entries</strong> ({DB_SOURCES[db.source] ?? db.source})
          </p>
          <h3>Learned on this server ({db.learned.length})</h3>
          {db.learned.length === 0 ? (
            <p className="muted">Nothing yet. Mods that crash a server because they are client-only show up here.</p>
          ) : (
            <ul className="learned">
              {db.learned.map((m) => (
                <li key={m.ids.join(",")}>
                  <strong>{m.name || m.ids[0]}</strong> <code>{m.ids.join(", ")}</code>
                  {m.pack && <span className="muted"> · found in {m.pack}</span>}
                  {m.count > 1 && <span className="muted"> · {m.count} times</span>}{" "}
                  <button type="button" onClick={() => forget(m.ids[0])}>
                    Forget
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
