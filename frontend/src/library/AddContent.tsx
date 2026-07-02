import { useState } from "react";
import { useAddVideo, useSeriesList } from "../queries";
import { useToast } from "../toast";
import { ChannelImport } from "./ChannelImport";

/** One intake for everything. Paste a link → routed by type: a channel link fans out
 *  into series (ChannelImport); a single video is added as an episode (new or existing
 *  series). Replaces the old scattered "Quét kênh" / "+ Series" buttons. */
export function AddContent({ onOpenSeries }: { onOpenSeries: (seriesId: string) => void }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [stage, setStage] = useState<"input" | "channel" | "single">("input");

  function reset() {
    setStage("input");
    setUrl("");
    setOpen(false);
  }
  const isChannel = (u: string) => /\/user\//.test(u) || /space\.bilibili\.com/.test(u);
  // A Bilibili video link often IS a whole show (合集 collection or multi-part video
  // — the "videopod.episodes" player). Let the user pick scope explicitly.
  const isBiliVideo = (u: string) => /bilibili\.com\/video\//.test(u);

  if (!open) {
    return (
      <button className="primary add-btn" onClick={() => setOpen(true)}>+ Thêm nội dung</button>
    );
  }

  return (
    <div className="add-panel panel">
      <div className="add-head">
        <span className="ci-tag mono">THÊM NỘI DUNG</span>
        <button className="link" onClick={reset}>Đóng</button>
      </div>

      {stage === "input" ? (
        <>
          <div className="ci-scan">
            <input
              className="lib-input"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="Dán link KÊNH (tự tách bộ) hoặc 1 VIDEO lẻ…"
              autoFocus
            />
            {isBiliVideo(url.trim()) && !isChannel(url.trim()) ? (
              <>
                <button className="primary" onClick={() => setStage("channel")}>Nhập cả bộ</button>
                <button className="ghost" onClick={() => setStage("single")}>Chỉ video này</button>
              </>
            ) : (
              <button className="primary" disabled={!url.trim()} onClick={() => setStage(isChannel(url.trim()) ? "channel" : "single")}>
                Tiếp
              </button>
            )}
          </div>
          <p className="ci-hint">
            Link <b>/user/…</b> = cả kênh (tách thành các bộ). Link video Bilibili thuộc 合集/nhiều phần có thể nhập cả bộ. Link 1 video = thêm 1 tập.
          </p>
        </>
      ) : null}

      {stage === "channel" ? <ChannelImport url={url.trim()} onDone={reset} /> : null}
      {stage === "single" ? <SingleVideoAdd url={url.trim()} onOpenSeries={(id) => { reset(); onOpenSeries(id); }} /> : null}
    </div>
  );
}

function SingleVideoAdd({ url, onOpenSeries }: { url: string; onOpenSeries: (seriesId: string) => void }) {
  const { data } = useSeriesList("");
  const series = data?.items ?? [];
  const addVideo = useAddVideo();
  const { toast } = useToast();
  const [target, setTarget] = useState<"new" | string>("new");
  const [title, setTitle] = useState("");

  async function add() {
    try {
      const result = await addVideo.mutateAsync({
        url,
        seriesId: target === "new" ? undefined : target,
        title: target === "new" ? title.trim() || undefined : undefined,
      });
      toast("Đã thêm video", "success");
      onOpenSeries(result.series_id);
    } catch (error) {
      toast(error instanceof Error ? error.message : "Không thêm được video", "error");
    }
  }

  return (
    <div className="single-add">
      <p className="ci-hint">Video lẻ — thêm vào:</p>
      <label className="radio-row">
        <input type="radio" checked={target === "new"} onChange={() => setTarget("new")} /> Tạo bộ mới
      </label>
      {target === "new" ? (
        <input className="lib-input" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Tên bộ (để trống = lấy tên video)" />
      ) : null}
      <label className="radio-row">
        <input type="radio" checked={target !== "new"} disabled={!series.length} onChange={() => setTarget(series[0]?.id ?? "new")} />
        Thêm vào bộ có sẵn {series.length ? "" : "(chưa có bộ nào)"}
      </label>
      {target !== "new" ? (
        <select className="lib-input" value={target} onChange={(event) => setTarget(event.target.value)}>
          {series.map((s) => (
            <option key={s.id} value={s.id}>{s.title}</option>
          ))}
        </select>
      ) : null}
      <button className="primary" disabled={addVideo.isPending} onClick={add}>
        {addVideo.isPending ? "Đang thêm…" : "Thêm video"}
      </button>
    </div>
  );
}
