import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { AddContent } from "../library/AddContent";
import { useSeriesList } from "../queries";
import { thumbSrc } from "../util";

/** The library home: a searchable grid of series + a single "Thêm" intake that routes a
 *  pasted link (channel → many series, single video → an episode, or an empty series). */
export function LibraryPage() {
  const [query, setQuery] = useState("");
  const { data, isLoading } = useSeriesList(query);
  const navigate = useNavigate();
  const series = data?.items ?? [];

  return (
    <section className="library">
      <div className="lib-head">
        <div>
          <h2 className="lib-title">Thư viện series</h2>
          <p className="lib-sub mono">{data?.total ?? 0} series</p>
        </div>
        <input
          className="lib-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Tìm series…"
          aria-label="Tìm series"
        />
      </div>

      <AddContent onOpenSeries={(id) => navigate(`/series/${id}`)} />

      {series.length ? (
        <div className="series-grid">
          {series.map((item) => (
            <button key={item.id} className="series-card" onClick={() => navigate(`/series/${item.id}`)}>
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
        <p className="muted-note">{isLoading ? "Đang tải…" : "Chưa có series. Dán link kênh hoặc video ở ô “Thêm”."}</p>
      )}
    </section>
  );
}
