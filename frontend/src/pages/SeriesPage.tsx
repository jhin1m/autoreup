import { FormEvent, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Preview } from "../library/Preview";
import {
  useAddSource,
  useCreateExport,
  useDeleteEpisodeDub,
  useDeleteExport,
  useDeleteSeries,
  useDeleteSource,
  useEpisodes,
  useExports,
  useRenderExport,
  useScanSeries,
  useSeries,
  useTranslate,
} from "../queries";
import { useToast } from "../toast";
import type { Episode, Series } from "../types";
import { DUB_STATE_VI, EXPORT_STATE_VI, formatDuration, thumbSrc } from "../util";

type Tab = "source" | "dubbed" | "export";

/** One series workflow. Tab lives in the URL (?tab=) so back/refresh keep your place;
 *  all data comes from query hooks and stays fresh via invalidation. */
export function SeriesPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get("tab") as Tab) || "source";
  const setTab = (next: Tab) => setSearchParams({ tab: next });

  const { toast, confirm } = useToast();
  const { data: series } = useSeries(id);
  const { data: episodes = [] } = useEpisodes(id);
  const { data: exports = [] } = useExports(id);
  const [preview, setPreview] = useState<Episode | null>(null);

  const scan = useScanSeries(id);
  const translate = useTranslate(id);
  const deleteDub = useDeleteEpisodeDub(id);
  const createExport = useCreateExport(id);
  const deleteSeries = useDeleteSeries();
  const dubbed = episodes.filter((e) => e.dub_state === "dubbed" && e.dub_job_id);

  if (!series) {
    return (
      <section className="series-detail">
        <button className="link" onClick={() => navigate("/")}>◀ Thư viện</button>
        <p className="muted-note">Đang tải…</p>
      </section>
    );
  }

  async function doTranslate(ids: string[]) {
    try {
      await translate.mutateAsync(ids);
      toast(`Đang dịch ${ids.length} tập`, "success");
      setTab("dubbed");
    } catch (error) {
      toast(error instanceof Error ? error.message : "Lỗi tạo job dịch", "error");
    }
  }

  return (
    <section className="series-detail">
      <div className="sd-head">
        <button className="link" onClick={() => navigate("/")}>◀ Thư viện</button>
        <h2 className="sd-title">{series.title}</h2>
        <button
          className="ghost danger"
          onClick={async () => {
            if (await confirm(`Xóa series “${series.title}”? Xoá luôn tập, bản dịch và file đã tạo — không hoàn tác.`)) {
              await deleteSeries.mutateAsync(series.id);
              navigate("/");
            }
          }}
        >
          Xóa series
        </button>
      </div>

      <SourceBar series={series} scanning={scan.isPending} onScan={async () => {
        try {
          const result = await scan.mutateAsync();
          toast(`Quét xong: +${result.added} tập mới (tổng ${result.total})`, "success");
        } catch (error) {
          toast(error instanceof Error ? error.message : "Không quét được", "error");
        }
      }} />

      <div className="sd-tabs">
        <TabButton active={tab === "source"} onClick={() => setTab("source")}>Tập nguồn · {series.episode_count}</TabButton>
        <TabButton active={tab === "dubbed"} onClick={() => setTab("dubbed")}>Đã dịch · {series.dubbed_count}</TabButton>
        <TabButton active={tab === "export"} onClick={() => setTab("export")}>Export · {series.export_count}</TabButton>
      </div>

      {tab === "source" ? <SourceTab episodes={episodes} pending={translate.isPending} onTranslate={doTranslate} /> : null}

      {tab === "dubbed" ? (
        <DubbedTab series={series} dubbed={dubbed} onPreview={setPreview} pending={createExport.isPending}
          onExport={async (body) => {
            try {
              await createExport.mutateAsync(body);
              toast("Đang ghép video dài…", "success");
              setTab("export");
            } catch (error) {
              toast(error instanceof Error ? error.message : "Không ghép được", "error");
            }
          }}
        />
      ) : null}

      {tab === "export" ? <ExportTab seriesId={id} exports={exports} /> : null}

      {preview ? (
        <Preview
          episode={preview}
          onClose={() => setPreview(null)}
          onRedub={() => { setPreview(null); doTranslate([preview.id]); }}
          onDelete={async () => {
            setPreview(null);
            if (await confirm("Xóa bản dịch của tập này?")) {
              await deleteDub.mutateAsync(preview.id);
              toast("Đã xóa bản dịch", "success");
            }
          }}
        />
      ) : null}
    </section>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button className={`sd-tab ${active ? "active" : ""}`} onClick={onClick}>{children}</button>;
}

/* ---------- source links bar ---------- */

function SourceBar({ series, scanning, onScan }: { series: Series; scanning: boolean; onScan: () => void }) {
  const [link, setLink] = useState("");
  const addSource = useAddSource(series.id);
  const deleteSource = useDeleteSource(series.id);

  async function add(event: FormEvent) {
    event.preventDefault();
    if (!link.trim()) return;
    await addSource.mutateAsync(link.trim());
    setLink("");
  }

  return (
    <div className="sd-sources">
      <span className="sd-sources-tag mono">NGUỒN</span>
      <div className="sd-source-chips">
        {series.sources.map((source) => (
          <span key={source.id} className="src-chip">
            <span className="src-platform">{source.platform}</span>
            <span className="src-url">{source.url.replace(/^https?:\/\/(www\.)?/, "").slice(0, 28)}…</span>
            <button className="src-x" title="Gỡ nguồn" onClick={() => deleteSource.mutate(source.id)}>×</button>
          </span>
        ))}
      </div>
      <form className="sd-add-source" onSubmit={add}>
        <input value={link} onChange={(event) => setLink(event.target.value)} placeholder="Dán link kênh…" aria-label="Link kênh" />
        <button className="ghost tiny" disabled={addSource.isPending || !link.trim()}>Thêm</button>
      </form>
      <button className="primary" disabled={scanning || !series.sources.length} onClick={onScan}>
        {scanning ? "Đang quét…" : "Quét tập mới"}
      </button>
    </div>
  );
}

/* ---------- tab: source episodes -> translate ---------- */

function SourceTab({ episodes, pending, onTranslate }: { episodes: Episode[]; pending: boolean; onTranslate: (ids: string[]) => void }) {
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
        <button className="primary" disabled={!selected.size || pending} onClick={() => onTranslate([...selected])}>
          {pending ? "Đang tạo…" : `Dịch ${selected.size || ""} tập`}
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

/* ---------- tab: dubbed -> compose export (drag to order) ---------- */

function DubbedTab({ series, dubbed, pending, onPreview, onExport }: {
  series: Series;
  dubbed: Episode[];
  pending: boolean;
  onPreview: (episode: Episode) => void;
  onExport: (body: { title: string; description: string; hashtags: string[]; episode_job_ids: string[] }) => void;
}) {
  const [order, setOrder] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [hashtags, setHashtags] = useState("#trungsinh #quantruong #thuyetminh");
  const [dragIndex, setDragIndex] = useState<number | null>(null);

  const toggle = (id: string) => setOrder((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  const chosen = order.map((id) => dubbed.find((e) => e.id === id)).filter(Boolean) as Episode[];
  const jobIds = chosen.map((e) => e.dub_job_id!).filter(Boolean);
  const totalDur = chosen.reduce((sum, e) => sum + (e.duration || 0), 0);

  function move(from: number, to: number) {
    setOrder((prev) => {
      const next = [...prev];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      return next;
    });
  }

  if (!dubbed.length) return <p className="muted-note">Chưa có tập nào đã dịch xong. Dịch ở tab “Tập nguồn” trước.</p>;

  return (
    <>
      <p className="sd-hint">Chọn các tập để ghép thành một video dài, kéo để sắp thứ tự, rồi đặt tiêu đề YouTube.</p>
      <div className="ep-grid">
        {dubbed.map((ep) => {
          const idx = order.indexOf(ep.id);
          return (
            <EpisodeCard key={ep.id} ep={ep} selected={idx >= 0} order={idx >= 0 ? idx + 1 : undefined}
              onToggle={() => toggle(ep.id)} onOpen={() => onPreview(ep)} />
          );
        })}
      </div>

      {chosen.length ? (
        <div className="export-compose panel">
          <div className="ec-row"><span className="ec-tag mono">{chosen.length} tập · ~{formatDuration(totalDur)}</span></div>
          <ol className="order-list">
            {chosen.map((ep, index) => (
              <li
                key={ep.id}
                className={`order-item ${dragIndex === index ? "dragging" : ""}`}
                draggable
                onDragStart={() => setDragIndex(index)}
                onDragOver={(event) => { event.preventDefault(); if (dragIndex !== null && dragIndex !== index) { move(dragIndex, index); setDragIndex(index); } }}
                onDragEnd={() => setDragIndex(null)}
              >
                <span className="order-grip" aria-hidden>⠿</span>
                <span className="order-num">{index + 1}</span>
                <span className="order-title">{ep.title}</span>
              </li>
            ))}
          </ol>
          <input className="lib-input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={`Tiêu đề YouTube (vd: ${series.title} — Full)`} />
          <textarea className="lib-input ec-desc" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Mô tả video…" rows={3} />
          <input className="lib-input" value={hashtags} onChange={(e) => setHashtags(e.target.value)} placeholder="#hashtag …" />
          <button className="primary" disabled={!jobIds.length || !title.trim() || pending}
            onClick={() => onExport({ title: title.trim(), description, hashtags: hashtags.split(/\s+/).filter((t) => t.startsWith("#")), episode_job_ids: jobIds })}>
            {pending ? "Đang dựng…" : `Ghép & Export ${chosen.length} tập ▶`}
          </button>
        </div>
      ) : null}
    </>
  );
}

/* ---------- tab: exports ---------- */

function ExportTab({ seriesId, exports }: { seriesId: string; exports: import("../types").Export[] }) {
  const render = useRenderExport(seriesId);
  const remove = useDeleteExport(seriesId);
  const { confirm } = useToast();

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
            {x.state === "done" && x.output_path ? <a className="ghost tiny" href={`/media/${x.output_path}`} download>Tải xuống</a> : null}
            {(x.state === "draft" || x.state === "failed") ? <button className="ghost tiny" disabled={render.isPending} onClick={() => render.mutate(x.id)}>Render</button> : null}
            {x.state === "rendering" ? <span className="mono tiny amber">đang ghép…</span> : null}
            <button className="ghost tiny danger" onClick={async () => { if (await confirm("Xóa bản ghép này?")) remove.mutate(x.id); }}>✕</button>
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
