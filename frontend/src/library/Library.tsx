import { FormEvent, useCallback, useEffect, useState } from "react";
import * as api from "../api";
import type { Series } from "../types";
import { thumbSrc } from "../util";

/** Series library — the grid of all series with search + create, the home of the
 *  content-management flow. Opening a series hands off to SeriesDetail. */
export function Library({ onOpen }: { onOpen: (seriesId: string) => void }) {
  const [series, setSeries] = useState<Series[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newLink, setNewLink] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const refresh = useCallback(async (q = "") => {
    try {
      const page = await api.listSeries(q);
      setSeries(page.items);
      setTotal(page.total);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không tải được thư viện");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const id = setTimeout(() => refresh(query), 250);
    return () => clearTimeout(id);
  }, [query, refresh]);

  async function submitCreate(event: FormEvent) {
    event.preventDefault();
    if (!newTitle.trim()) return;
    setBusy(true);
    setMessage("");
    try {
      const sources = newLink.trim() ? [{ platform: "douyin", url: newLink.trim() }] : [];
      const created = await api.createSeries(newTitle.trim(), sources);
      setNewTitle("");
      setNewLink("");
      setCreating(false);
      await refresh(query);
      onOpen(created.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không tạo được series");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="library">
      <div className="lib-head">
        <div>
          <h2 className="lib-title">Thư viện series</h2>
          <p className="lib-sub mono">{total} series</p>
        </div>
        <div className="lib-actions">
          <input
            className="lib-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Tìm series…"
            aria-label="Tìm series"
          />
          <button className="primary" onClick={() => setCreating((value) => !value)}>
            {creating ? "Đóng" : "+ Series"}
          </button>
        </div>
      </div>

      {creating ? (
        <form className="lib-create panel" onSubmit={submitCreate}>
          <input
            className="lib-input"
            value={newTitle}
            onChange={(event) => setNewTitle(event.target.value)}
            placeholder="Tên series (vd: Trùng Sinh Báo Thù)"
            autoFocus
          />
          <input
            className="lib-input"
            value={newLink}
            onChange={(event) => setNewLink(event.target.value)}
            placeholder="Link kênh nguồn (tuỳ chọn, thêm sau cũng được)"
          />
          <button className="primary" disabled={busy || !newTitle.trim()}>
            {busy ? "Đang tạo…" : "Tạo series"}
          </button>
        </form>
      ) : null}

      {message ? <p className="lib-msg">{message}</p> : null}

      {series.length ? (
        <div className="series-grid">
          {series.map((item) => (
            <button key={item.id} className="series-card" onClick={() => onOpen(item.id)}>
              <div className="series-cover">
                {item.cover_url ? <img src={thumbSrc(item.cover_url)} alt="" loading="lazy" /> : <span className="reel" aria-hidden />}
                {item.export_count ? <span className="series-badge">{item.export_count} bản ghép</span> : null}
              </div>
              <div className="series-body">
                <p className="series-name">{item.title}</p>
                <p className="mono tiny series-stat">
                  {item.episode_count} tập · <span className="jade">{item.dubbed_count} đã dịch</span>
                </p>
              </div>
            </button>
          ))}
        </div>
      ) : (
        <p className="muted-note">Chưa có series. Bấm “+ Series” để tạo và dán link kênh.</p>
      )}
    </section>
  );
}
