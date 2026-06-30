import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../api";
import type { Episode, Export, Series } from "../types";
import { DUB_STATE_VI, EXPORT_STATE_VI, formatDuration, thumbSrc } from "../util";
import { Preview } from "./Preview";

type Tab = "source" | "dubbed" | "export";

/** One series: manage source links, scan episodes, pick episodes to dub, then pick
 *  dubbed episodes (ordered) to merge into a long export video for YouTube. Watching a
 *  dubbed episode opens a preview modal in place — no separate studio view. */
export function SeriesDetail({ seriesId, onBack }: { seriesId: string; onBack: () => void }) {
  const [series, setSeries] = useState<Series | null>(null);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [exports, setExports] = useState<Export[]>([]);
  const [tab, setTab] = useState<Tab>("source");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState<Episode | null>(null);

  const loadSeries = useCallback(async () => setSeries(await api.getSeries(seriesId)), [seriesId]);
  const loadEpisodes = useCallback(async () => setEpisodes(await api.listEpisodes(seriesId)), [seriesId]);
  const loadExports = useCallback(async () => setExports(await api.listExports(seriesId)), [seriesId]);

  useEffect(() => {
    loadSeries().catch(() => undefined);
    loadEpisodes().catch(() => undefined);
    loadExports().catch(() => undefined);
  }, [loadSeries, loadEpisodes, loadExports]);

  // Live-refresh while anything is in flight (dubbing or rendering).
  const hasLiveWork = episodes.some((e) => e.dub_state === "translating") || exports.some((x) => x.state === "rendering");
  useEffect(() => {
    if (!hasLiveWork) return;
    const id = setInterval(() => {
      loadEpisodes().catch(() => undefined);
      loadExports().catch(() => undefined);
      loadSeries().catch(() => undefined);
    }, 4000);
    return () => clearInterval(id);
  }, [hasLiveWork, loadEpisodes, loadExports, loadSeries]);

  async function run<T>(key: string, fn: () => Promise<T>) {
    setBusy(key);
    setMessage("");
    try {
      await fn();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Có lỗi xảy ra");
    } finally {
      setBusy("");
    }
  }

  const dubbed = useMemo(() => episodes.filter((e) => e.dub_state === "dubbed" && e.dub_job_id), [episodes]);

  if (!series) {
    return (
      <section className="series-detail">
        <button className="link" onClick={onBack}>◀ Thư viện</button>
        <p className="muted-note">Đang tải…</p>
      </section>
    );
  }

  return (
    <section className="series-detail">
      <div className="sd-head">
        <button className="link" onClick={onBack}>◀ Thư viện</button>
        <h2 className="sd-title">{series.title}</h2>
        <button
          className="ghost danger"
          onClick={() =>
            window.confirm(`Xóa series “${series.title}”? Xoá luôn tập, bản dịch và file đã tạo — không hoàn tác.`) &&
            run("delete", async () => {
              await api.deleteSeries(series.id);
              onBack();
            })
          }
        >
          Xóa series
        </button>
      </div>

      <SourceBar series={series} busy={busy} onChange={loadSeries} onScan={() => run("scan", async () => {
        const result = await api.scanSeries(series.id);
        setMessage(`Quét xong: +${result.added} tập mới (tổng ${result.total}).`);
        await Promise.all([loadEpisodes(), loadSeries()]);
      })} />

      <div className="sd-tabs">
        <TabButton active={tab === "source"} onClick={() => setTab("source")}>Tập nguồn · {series.episode_count}</TabButton>
        <TabButton active={tab === "dubbed"} onClick={() => setTab("dubbed")}>Đã dịch · {series.dubbed_count}</TabButton>
        <TabButton active={tab === "export"} onClick={() => setTab("export")}>Export · {series.export_count}</TabButton>
      </div>

      {message ? <p className="lib-msg">{message}</p> : null}

      {tab === "source" ? (
        <SourceTab episodes={episodes} busy={busy} onTranslate={(ids) => run("translate", async () => {
          await api.translateEpisodes(series.id, ids);
          await Promise.all([loadEpisodes(), loadSeries()]);
          setTab("dubbed");
        })} />
      ) : null}

      {tab === "dubbed" ? (
        <DubbedTab series={series} dubbed={dubbed} onPreview={setPreview} busy={busy} onExport={(body) => run("export", async () => {
          const created = await api.createExport(series.id, body);
          await api.renderExport(created.id);
          await Promise.all([loadExports(), loadSeries()]);
          setTab("export");
        })} />
      ) : null}

      {tab === "export" ? (
        <ExportTab exports={exports} busy={busy}
          onRender={(id) => run("render", async () => { await api.renderExport(id); await loadExports(); })}
          onDelete={(id) => run("delx", async () => { await api.deleteExport(id); await Promise.all([loadExports(), loadSeries()]); })}
        />
      ) : null}

      {preview ? (
        <Preview
          episode={preview}
          onClose={() => setPreview(null)}
          onRedub={() => { setPreview(null); run("translate", async () => {
            await api.translateEpisodes(series.id, [preview.id]);
            await Promise.all([loadEpisodes(), loadSeries()]);
            setTab("dubbed");
          }); }}
          onDelete={() => { setPreview(null); run("deldub", async () => {
            await api.deleteEpisodeDub(preview.id);
            await Promise.all([loadEpisodes(), loadSeries()]);
          }); }}
        />
      ) : null}
    </section>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button className={`sd-tab ${active ? "active" : ""}`} onClick={onClick}>
      {children}
    </button>
  );
}

/* ---------- source links bar ---------- */

function SourceBar({ series, busy, onChange, onScan }: { series: Series; busy: string; onChange: () => Promise<void>; onScan: () => void }) {
  const [link, setLink] = useState("");
  const [adding, setAdding] = useState(false);

  async function add(event: FormEvent) {
    event.preventDefault();
    if (!link.trim()) return;
    setAdding(true);
    try {
      await api.addSource(series.id, "douyin", link.trim());
      setLink("");
      await onChange();
    } finally {
      setAdding(false);
    }
  }

  return (
    <div className="sd-sources">
      <span className="sd-sources-tag mono">NGUỒN</span>
      <div className="sd-source-chips">
        {series.sources.map((source) => (
          <span key={source.id} className="src-chip">
            <span className="src-platform">{source.platform}</span>
            <span className="src-url">{source.url.replace(/^https?:\/\/(www\.)?/, "").slice(0, 28)}…</span>
            <button className="src-x" title="Gỡ nguồn" onClick={async () => { await api.deleteSource(series.id, source.id); await onChange(); }}>×</button>
          </span>
        ))}
      </div>
      <form className="sd-add-source" onSubmit={add}>
        <input value={link} onChange={(event) => setLink(event.target.value)} placeholder="Dán link kênh…" aria-label="Link kênh" />
        <button className="ghost tiny" disabled={adding || !link.trim()}>Thêm</button>
      </form>
      <button className="primary" disabled={busy === "scan" || !series.sources.length} onClick={onScan}>
        {busy === "scan" ? "Đang quét…" : "Quét tập mới"}
      </button>
    </div>
  );
}

/* ---------- tab: source episodes -> translate ---------- */

function SourceTab({ episodes, busy, onTranslate }: { episodes: Episode[]; busy: string; onTranslate: (ids: string[]) => void }) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toggle = (id: string) => setSelected((prev) => {
    const next = new Set(prev);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const selectable = episodes.filter((e) => e.dub_state === "none" || e.dub_state === "failed");
  const allSel = selectable.length > 0 && selectable.every((e) => selected.has(e.id));

  if (!episodes.length) return <p className="muted-note">Chưa có tập. Thêm link kênh rồi bấm “Quét tập mới”.</p>;

  return (
    <>
      <div className="sd-bar">
        <button className="link" onClick={() => setSelected(allSel ? new Set() : new Set(selectable.map((e) => e.id)))}>
          {allSel ? "Bỏ chọn" : "Chọn tất cả chưa dịch"}
        </button>
        <button className="primary" disabled={!selected.size || busy === "translate"} onClick={() => onTranslate([...selected])}>
          {busy === "translate" ? "Đang tạo…" : `Dịch ${selected.size || ""} tập`}
        </button>
      </div>
      <div className="ep-grid">
        {episodes.map((ep) => (
          <EpisodeCard key={ep.id} ep={ep} selected={selected.has(ep.id)} onToggle={() => toggle(ep.id)} />
        ))}
      </div>
    </>
  );
}

/* ---------- tab: dubbed -> compose export ---------- */

function DubbedTab({ series, dubbed, busy, onPreview, onExport }: {
  series: Series;
  dubbed: Episode[];
  busy: string;
  onPreview: (episode: Episode) => void;
  onExport: (body: { title: string; description: string; hashtags: string[]; episode_job_ids: string[] }) => void;
}) {
  const [order, setOrder] = useState<string[]>([]); // selected episode ids, in click order
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [hashtags, setHashtags] = useState("#trungsinh #quantruong #thuyetminh");

  const toggle = (id: string) => setOrder((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  const jobIds = order.map((id) => dubbed.find((e) => e.id === id)?.dub_job_id).filter(Boolean) as string[];
  const totalDur = order.reduce((sum, id) => sum + (dubbed.find((e) => e.id === id)?.duration || 0), 0);

  if (!dubbed.length) return <p className="muted-note">Chưa có tập nào đã dịch xong. Dịch ở tab “Tập nguồn” trước.</p>;

  return (
    <>
      <p className="sd-hint">Chọn các tập (theo thứ tự bấm) để ghép thành một video dài, rồi đặt tiêu đề YouTube.</p>
      <div className="ep-grid">
        {dubbed.map((ep) => {
          const idx = order.indexOf(ep.id);
          return (
            <EpisodeCard
              key={ep.id}
              ep={ep}
              selected={idx >= 0}
              order={idx >= 0 ? idx + 1 : undefined}
              onToggle={() => toggle(ep.id)}
              onOpen={() => onPreview(ep)}
            />
          );
        })}
      </div>
      <div className="export-compose panel">
        <div className="ec-row">
          <span className="ec-tag mono">{order.length} tập · ~{formatDuration(totalDur)}</span>
        </div>
        <input className="lib-input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={`Tiêu đề YouTube (vd: ${series.title} — Full)`} />
        <textarea className="lib-input ec-desc" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Mô tả video…" rows={3} />
        <input className="lib-input" value={hashtags} onChange={(e) => setHashtags(e.target.value)} placeholder="#hashtag …" />
        <button
          className="primary"
          disabled={!jobIds.length || !title.trim() || busy === "export"}
          onClick={() => onExport({ title: title.trim(), description, hashtags: hashtags.split(/\s+/).filter((t) => t.startsWith("#")), episode_job_ids: jobIds })}
        >
          {busy === "export" ? "Đang dựng…" : `Ghép & Export ${order.length || ""} tập ▶`}
        </button>
      </div>
    </>
  );
}

/* ---------- tab: exports ---------- */

function ExportTab({ exports, busy, onRender, onDelete }: {
  exports: Export[];
  busy: string;
  onRender: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  if (!exports.length) return <p className="muted-note">Chưa có bản ghép. Tạo ở tab “Đã dịch”.</p>;
  return (
    <div className="export-list">
      {exports.map((x) => (
        <article key={x.id} className="export-row panel">
          <div className="ex-meta">
            <p className="ex-title">{x.title}</p>
            <p className="mono tiny">
              <span className={`ex-state ${x.state}`}>{EXPORT_STATE_VI[x.state]}</span>
              {" · "}{x.episode_job_ids.length} tập{x.duration ? ` · ${formatDuration(x.duration)}` : ""}
              {x.error ? <span className="rose"> · {x.error}</span> : null}
            </p>
          </div>
          <div className="ex-actions">
            {x.state === "done" && x.output_path ? (
              <a className="ghost tiny" href={`/media/${x.output_path}`} download>Tải xuống</a>
            ) : null}
            {(x.state === "draft" || x.state === "failed") ? (
              <button className="ghost tiny" disabled={busy === "render"} onClick={() => onRender(x.id)}>Render</button>
            ) : null}
            {x.state === "rendering" ? <span className="mono tiny amber">đang ghép…</span> : null}
            <button className="ghost tiny danger" onClick={() => onDelete(x.id)}>✕</button>
          </div>
        </article>
      ))}
    </div>
  );
}

/* ---------- shared episode card ---------- */

function EpisodeCard({ ep, selected, order, onToggle, onOpen }: {
  ep: Episode;
  selected: boolean;
  order?: number;
  onToggle: () => void;
  onOpen?: () => void;
}) {
  return (
    <article className={`ep-card ${selected ? "sel" : ""}`}>
      <label className="ep-pick">
        <input type="checkbox" checked={selected} onChange={onToggle} />
        {order ? <span className="ep-order">{order}</span> : null}
      </label>
      <button className="ep-main" onClick={onOpen ?? onToggle} title={ep.title}>
        <div className="ep-thumb">{ep.thumbnail_url ? <img src={thumbSrc(ep.thumbnail_url)} alt="" loading="lazy" /> : <span className="reel" aria-hidden />}</div>
        <div className="ep-meta">
          <p className="ep-title">{ep.title}</p>
          <p className="mono tiny">
            <span className={`dub-dot ${ep.dub_state}`} />{DUB_STATE_VI[ep.dub_state]}
            {ep.duration ? ` · ${formatDuration(ep.duration)}` : ""}
          </p>
        </div>
      </button>
    </article>
  );
}
