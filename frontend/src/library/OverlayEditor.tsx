import { useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api";
import type { CoverRegion, OverlayConfig } from "../api";
import { useToast } from "../toast";

const MODES: { id: string; label: string }[] = [
  { id: "blur", label: "Làm mờ" },
  { id: "darken", label: "Tối đi" },
  { id: "solid", label: "Đen đặc" },
];

type Handle = "move" | "se" | null;

/** WYSIWYG editor for the package overlay: drag boxes over the paused frame to hide
 *  the original subtitle band / a channel logo, and place the Vietnamese caption.
 *  Everything is stored as fractions of the frame, so it survives any resolution. */
export function OverlayEditor({
  jobId,
  videoPath,
  cues,
  onClose,
  onRepack,
}: {
  jobId: string;
  videoPath: string;
  cues: { start: number; end: number; text: string }[];
  onClose: () => void;
  onRepack: () => void;
}) {
  const { toast } = useToast();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [config, setConfig] = useState<OverlayConfig | null>(null);
  const [selected, setSelected] = useState(-1);
  const [t, setT] = useState(1);
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState("");
  const drag = useRef<{ handle: Handle; startX: number; startY: number; box: CoverRegion } | null>(null);

  useEffect(() => {
    api
      .fetchOverlay(jobId)
      .then((cfg) => {
        setConfig(cfg);
        setSelected(cfg.cover.length ? 0 : -1);
      })
      .catch(() => setConfig({ caption_mode: "band", caption_y: null, caption_scale: 1, cover: [] }));
  }, [jobId]);

  const captionText = useMemo(() => {
    const cue = cues.find((item) => item.start <= t && t < item.end);
    return cue?.text ?? "";
  }, [cues, t]);

  function patch(next: Partial<OverlayConfig>) {
    setConfig((current) => (current ? { ...current, ...next } : current));
    setPreview("");
  }

  function patchRegion(index: number, next: Partial<CoverRegion>) {
    setConfig((current) => {
      if (!current) return current;
      const cover = current.cover.map((region, i) => (i === index ? { ...region, ...next } : region));
      return { ...current, cover };
    });
    setPreview("");
  }

  function addRegion(label: string, mode: string) {
    setConfig((current) => {
      if (!current) return current;
      const region: CoverRegion = { x: 0.05, y: 0.05, w: 0.25, h: 0.1, mode, strength: 1, label };
      setSelected(current.cover.length);
      return { ...current, cover: [...current.cover, region] };
    });
    setPreview("");
  }

  function removeRegion(index: number) {
    setConfig((current) => {
      if (!current) return current;
      const cover = current.cover.filter((_region, i) => i !== index);
      setSelected((sel) => (sel === index ? -1 : sel > index ? sel - 1 : sel));
      return { ...current, cover };
    });
    setPreview("");
  }

  function onPointerDown(event: React.PointerEvent, index: number, handle: Handle) {
    if (!config) return;
    event.stopPropagation();
    event.preventDefault();
    setSelected(index);
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    drag.current = { handle, startX: event.clientX, startY: event.clientY, box: { ...config.cover[index]! } };
    (event.target as HTMLElement).setPointerCapture(event.pointerId);
  }

  function onPointerMove(event: React.PointerEvent) {
    const state = drag.current;
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!state || !rect || !config) return;
    const dx = (event.clientX - state.startX) / rect.width;
    const dy = (event.clientY - state.startY) / rect.height;
    const box = state.box;
    if (state.handle === "move") {
      const x = Math.min(Math.max(box.x + dx, 0), 1 - box.w);
      const y = Math.min(Math.max(box.y + dy, 0), 1 - box.h);
      patchRegion(selected, { x: round4(x), y: round4(y) });
    } else {
      const w = Math.min(Math.max(box.w + dx, 0.02), 1 - box.x);
      const h = Math.min(Math.max(box.h + dy, 0.02), 1 - box.y);
      patchRegion(selected, { w: round4(w), h: round4(h) });
    }
  }

  const active = selected >= 0 && config ? config.cover[selected] : undefined;

  async function previewFrame() {
    if (!config) return;
    setBusy("preview");
    try {
      const url = await api.previewOverlayFrame(jobId, config, t, captionText);
      setPreview((old) => {
        if (old) URL.revokeObjectURL(old);
        return url;
      });
    } catch (error) {
      toast(String(error), "error");
    } finally {
      setBusy("");
    }
  }

  async function save() {
    if (!config) return;
    setBusy("save");
    try {
      const saved = await api.saveOverlay(jobId, config);
      setConfig(saved);
      toast("Đã lưu khung che", "success");
    } catch (error) {
      toast(String(error), "error");
    } finally {
      setBusy("");
    }
  }

  async function repack() {
    if (!config) return;
    setBusy("repack");
    try {
      await save();
      await api.repackJob(jobId);
      toast("Đang render lại video…", "success");
      onRepack();
      onClose();
    } catch (error) {
      toast(String(error), "error");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="ov-scrim" onClick={onClose}>
      <div className="ov-panel" onClick={(event) => event.stopPropagation()}>
        <div className="ov-head">
          <p>
            Khung che & vị trí phụ đề <span className="mono tiny">job {jobId}</span>
          </p>
          <button className="link" onClick={onClose}>
            Đóng
          </button>
        </div>

        <div className="ov-body">
          <div className="ov-canvas" ref={canvasRef} onPointerMove={onPointerMove} onPointerUp={() => (drag.current = null)}>
            <video
              ref={videoRef}
              src={`${videoPath}#t=${t.toFixed(2)}`}
              muted
              playsInline
              preload="metadata"
              onLoadedMetadata={() => {
                const video = videoRef.current;
                if (video) video.currentTime = t;
              }}
            />
            {config?.cover.map((region, index) => (
              <div
                key={index}
                className={`ov-box ${index === selected ? "on" : ""} ${region.mode}`}
                style={{ left: `${region.x * 100}%`, top: `${region.y * 100}%`, width: `${region.w * 100}%`, height: `${region.h * 100}%` }}
                onPointerDown={(event) => onPointerDown(event, index, "move")}
                title={region.label || region.mode}
              >
                {index === selected ? (
                  <span className="ov-handle" onPointerDown={(event) => onPointerDown(event, index, "se")} />
                ) : null}
              </div>
            ))}
            <div
              className="ov-caption-line"
              style={{ top: `${((config?.caption_y ?? 0.88) * 100).toFixed(2)}%`, opacity: config?.caption_mode === "manual" ? 1 : 0.45 }}
              title="Vị trí phụ đề Việt"
            />
            {captionText ? <p className="ov-caption-hint">{captionText}</p> : null}
          </div>

          <aside className="ov-side">
            <label className="ov-field">
              <span>Thời điểm xem (giây)</span>
              <input
                type="number"
                min={0}
                step={0.5}
                value={t}
                onChange={(event) => {
                  const next = Math.max(0, Number(event.target.value) || 0);
                  setT(next);
                  const video = videoRef.current;
                  if (video) video.currentTime = next;
                  setPreview("");
                }}
              />
            </label>

            <div className="ov-group">
              <p className="mono tiny">Hộp che ({config?.cover.length ?? 0})</p>
              {config?.cover.map((region, index) => (
                <button
                  key={index}
                  className={`ov-region ${index === selected ? "on" : ""}`}
                  onClick={() => setSelected(index)}
                  onDoubleClick={() => removeRegion(index)}
                  title="Nháy đúp để xoá"
                >
                  <span className={`dot ${region.mode}`} />
                  {region.label || region.mode}
                  <span className="mono tiny">
                    {Math.round(region.x * 100)}%,{Math.round(region.y * 100)}% · {Math.round(region.w * 100)}×{Math.round(region.h * 100)}
                  </span>
                </button>
              ))}
              <div className="ov-row">
                <button className="ghost" onClick={() => addRegion("logo", "blur")}>
                  + Logo
                </button>
                <button className="ghost" onClick={() => addRegion("text", "blur")}>
                  + Vùng chữ
                </button>
                <button className="ghost" onClick={() => addRegion("band", "blur")}>
                  + Band phụ đề
                </button>
              </div>
            </div>

            {active ? (
              <div className="ov-group">
                <p className="mono tiny">Hộp đang chọn</p>
                <label className="ov-field">
                  <span>Cách che</span>
                  <select value={active.mode} onChange={(event) => patchRegion(selected, { mode: event.target.value })}>
                    {MODES.map((mode) => (
                      <option key={mode.id} value={mode.id}>
                        {mode.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="ov-field">
                  <span>Cường độ {active.strength.toFixed(2)}</span>
                  <input
                    type="range"
                    min={0.2}
                    max={1}
                    step={0.05}
                    value={active.strength}
                    onChange={(event) => patchRegion(selected, { strength: Number(event.target.value) })}
                  />
                </label>
                <label className="ov-field">
                  <span>Nhãn</span>
                  <input value={active.label} onChange={(event) => patchRegion(selected, { label: event.target.value })} />
                </label>
                <button className="ghost danger" onClick={() => removeRegion(selected)}>
                  Xoá hộp này
                </button>
              </div>
            ) : null}

            <div className="ov-group">
              <p className="mono tiny">Phụ đề Việt</p>
              <div className="seg-toggle">
                <button className={config?.caption_mode === "band" ? "on" : ""} onClick={() => patch({ caption_mode: "band" })}>
                  Theo band gốc
                </button>
                <button className={config?.caption_mode === "manual" ? "on" : ""} onClick={() => patch({ caption_mode: "manual" })}>
                  Tự đặt
                </button>
              </div>
              <label className="ov-field">
                <span>Vị trí dọc {Math.round((config?.caption_y ?? 0.88) * 100)}%</span>
                <input
                  type="range"
                  min={0.3}
                  max={0.98}
                  step={0.01}
                  value={config?.caption_y ?? 0.88}
                  onChange={(event) => patch({ caption_y: Number(event.target.value), caption_mode: "manual" })}
                />
              </label>
              <label className="ov-field">
                <span>Cỡ chữ ×{(config?.caption_scale ?? 1).toFixed(2)}</span>
                <input
                  type="range"
                  min={0.6}
                  max={1.8}
                  step={0.05}
                  value={config?.caption_scale ?? 1}
                  onChange={(event) => patch({ caption_scale: Number(event.target.value) })}
                />
              </label>
            </div>
          </aside>
        </div>

        {preview ? (
          <div className="ov-preview">
            <img src={preview} alt="Xem thử" />
            <span className="mono tiny">Xem thử tại {t.toFixed(1)}s — đúng như bản render</span>
          </div>
        ) : null}

        <div className="ov-actions">
          <button className="ghost" onClick={previewFrame} disabled={!!busy}>
            {busy === "preview" ? "Đang dựng…" : "Xem thử khung hình"}
          </button>
          <div className="pv-right">
            <button className="ghost" onClick={save} disabled={!!busy}>
              {busy === "save" ? "Đang lưu…" : "Lưu"}
            </button>
            <button className="primary" onClick={repack} disabled={!!busy}>
              {busy === "repack" ? "Đang gửi…" : "Render lại video"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function round4(value: number): number {
  return Math.round(value * 10000) / 10000;
}
