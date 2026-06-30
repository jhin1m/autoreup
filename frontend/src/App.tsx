import { useState } from "react";
import { Link, Route, Routes } from "react-router-dom";
import { Settings } from "./Settings";
import { LibraryPage } from "./pages/LibraryPage";
import { SeriesPage } from "./pages/SeriesPage";

/** App shell: a header + routed pages. The product is the library — a grid of series
 *  (/) each opening to its workflow (/series/:id). URL-driven, so back/refresh/deep-link
 *  all work. Cookie/session management lives in Settings, not the header chrome. */
export function App() {
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <div className="desk">
      <header className="deck">
        <Link to="/" className="mark">
          <span className="mark-dot" aria-hidden />
          <div>
            <p className="mark-name">AutoReup</p>
            <p className="mark-sub">Reup studio · 中文 → Tiếng Việt</p>
          </div>
        </Link>
        <button className="ghost" onClick={() => setSettingsOpen(true)}>⚙ Cài đặt</button>
      </header>

      <Routes>
        <Route path="/" element={<LibraryPage />} />
        <Route path="/series/:id" element={<SeriesPage />} />
      </Routes>

      {settingsOpen ? <Settings onClose={() => setSettingsOpen(false)} /> : null}
    </div>
  );
}
