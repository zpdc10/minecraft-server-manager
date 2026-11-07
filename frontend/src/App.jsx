import { useState } from "react";
import Search from "./pages/Search.jsx";

// Pages are added to this list (step 2 adds "Servers").
const PAGES = [{ id: "search", label: "Find modpacks" }];

export default function App() {
  const [page, setPage] = useState("search");

  // Called when the user picks a pack version to turn into a server.
  // Step 2 replaces this with its CreateServerDialog.
  function handleCreate({ source, projectId, versionId, title }) {
    alert(`Server creation is coming soon.\n\n${title} (${source} ${projectId}, version ${versionId})`);
  }

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
      <main>{page === "search" && <Search onCreate={handleCreate} />}</main>
    </div>
  );
}
