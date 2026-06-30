import { useEffect, useState } from "react";
import * as api from "./api";
import { Library } from "./library/Library";
import { SeriesDetail } from "./library/SeriesDetail";
import type { ProviderSession } from "./types";

/** App shell. The product IS the library: a grid of series, each opening to its
 *  workflow (source episodes → dub → export). Watching a dubbed episode happens in a
 *  preview modal inside the series — there is no separate single-video "studio". */
export function App() {
  const [providers, setProviders] = useState<ProviderSession[]>([]);
  const [cookiePlatform, setCookiePlatform] = useState<string | null>(null);
  const [seriesId, setSeriesId] = useState<string | null>(null);

  useEffect(() => {
    api.loadProviders().then(setProviders).catch(() => undefined);
  }, []);

  return (
    <div className="desk">
      <header className="deck">
        <div className="mark">
          <span className="mark-dot" aria-hidden />
          <div>
            <p className="mark-name">AutoReup</p>
            <p className="mark-sub">Reup studio · 中文 → Tiếng Việt</p>
          </div>
        </div>
        <ProviderBar providers={providers} onPick={setCookiePlatform} />
      </header>

      {seriesId ? (
        <SeriesDetail seriesId={seriesId} onBack={() => setSeriesId(null)} />
      ) : (
        <Library onOpen={setSeriesId} />
      )}

      {cookiePlatform ? (
        <CookieDialog
          session={providers.find((session) => session.platform === cookiePlatform)}
          onClose={() => setCookiePlatform(null)}
          onSaved={() => api.loadProviders().then(setProviders).catch(() => undefined)}
        />
      ) : null}
    </div>
  );
}

/* ---------- provider sessions (cookie management) ---------- */

function ProviderBar({ providers, onPick }: { providers: ProviderSession[]; onPick: (platform: string) => void }) {
  if (!providers.length) return null;
  return (
    <div className="providers">
      {providers.map((session) => (
        <button
          key={session.platform}
          className={`prov ${session.configured ? "ok" : "off"}`}
          onClick={() => onPick(session.platform)}
          title={session.note}
        >
          <span className="prov-dot" aria-hidden />
          {session.label}
          <span className="prov-state">{session.configured ? "phiên ✓" : "cần cookie"}</span>
        </button>
      ))}
    </div>
  );
}

function CookieDialog({ session, onClose, onSaved }: { session?: ProviderSession; onClose: () => void; onSaved: () => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!session) return null;
  async function save() {
    setBusy(true);
    setError("");
    try {
      await api.saveCookies(session!.platform, value);
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không lưu được cookie");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h3>Phiên {session.label}</h3>
        <p className="muted-note">{session.note}</p>
        {session.required_missing.length ? (
          <p className="mono small">Cần: {session.required_missing.join(", ")}</p>
        ) : null}
        <textarea
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder='Dán cookie JSON từ Cookie-Editor ({"cookies":[…]}), file cookies.txt, hoặc header Cookie…'
          rows={6}
        />
        {error ? <p className="fault">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Đóng
          </button>
          <button onClick={save} disabled={busy || value.trim().length < 4}>
            {busy ? "Đang lưu…" : "Lưu phiên"}
          </button>
        </div>
      </div>
    </div>
  );
}
