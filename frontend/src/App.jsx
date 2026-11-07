import { useState } from "react";
import CreateServerDialog from "./components/CreateServerDialog.jsx";
import PackBuilder from "./pages/PackBuilder.jsx";
import Search from "./pages/Search.jsx";
import Servers from "./pages/Servers.jsx";
import Settings from "./pages/Settings.jsx";

const PAGES = [
  { id: "search", label: "Find modpacks" },
  { id: "builder", label: "Build a pack" },
  { id: "servers", label: "Servers" },
  { id: "settings", label: "Settings" },
];

export default function App() {
  const [page, setPage] = useState("search");
  // The pack version the user picked to turn into a server, or null.
  const [creating, setCreating] = useState(null);

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">⛏ Server Manager</span>
        <nav>
          {PAGES.map((p) => (
            <button key={p.id} className={page === p.id ? "tab active" : "tab"} onClick={() => setPage(p.id)}>
              {p.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {page === "search" && <Search onCreate={setCreating} onOpenSettings={() => setPage("settings")} />}
        {page === "builder" && <PackBuilder onCreate={setCreating} onOpenSettings={() => setPage("settings")} />}
        {page === "servers" && <Servers />}
        {page === "settings" && <Settings />}
      </main>
      <CreateServerDialog
        open={creating !== null}
        {...(creating || {})}
        onClose={() => setCreating(null)}
        onCreated={() => setPage("servers")}
      />
    </div>
  );
}
