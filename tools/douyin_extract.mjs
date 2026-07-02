// Headless-Chromium Douyin stream extractor (zero npm deps, Node >= 22 global WebSocket).
//
// Launches the installed Edge/Chromium headless, injects the saved cookies, loads the
// video page so Douyin's own JS signs the request, and reports the real CDN stream URL
// plus title/duration as JSON on stdout. Python then downloads it with the same cookies.
//
// Usage: node douyin_extract.mjs --url <videoUrl> --cookies <cookies.json> --browser <path> [--timeout 30000]

import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : fallback;
}

const url = arg("url");
const cookiesPath = arg("cookies");
const browser = arg("browser", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge");
const timeout = Number(arg("timeout", "35000"));
const debugPort = Number(arg("port", "0")) || 9300 + (process.pid % 800);

if (!url) {
  console.error("missing --url");
  process.exit(2);
}

const cookies = cookiesPath ? JSON.parse(readFileSync(cookiesPath, "utf8")) : [];
const userDir = mkdtempSync(path.join(os.tmpdir(), "autoreup-dy-"));

const proc = spawn(browser, [
  "--headless=new",
  `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${userDir}`,
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-gpu",
  "--mute-audio",
  "--autoplay-policy=no-user-gesture-required",
  "--disable-background-networking",
  "--window-size=1280,900",
  `--user-agent=${UA}`,
  "about:blank",
]);
proc.on("error", (e) => fail(`browser spawn failed: ${e.message}`));

function cleanup() {
  try { proc.kill("SIGKILL"); } catch {}
  try { rmSync(userDir, { recursive: true, force: true }); } catch {}
}
function fail(msg) {
  cleanup();
  console.error(msg);
  process.exit(1);
}
function done(payload) {
  cleanup();
  process.stdout.write(JSON.stringify(payload));
  process.exit(0);
}

// Hard watchdog: a CDP send can hang (e.g. Douyin serves a CAPTCHA interstitial and the
// page never settles), stalling the main loop past its own deadline. This guarantees the
// process exits — and fails FAST with a clear reason — instead of hanging until the
// caller's kill. Budget = devtools wait (15s) + loop timeout + a small buffer.
const watchdog = setTimeout(
  () => fail("Douyin không phản hồi (có thể đang chặn / yêu cầu xác minh CAPTCHA). Thử lại sau hoặc cập nhật cookie."),
  15000 + timeout + 8000,
);
watchdog.unref?.();

async function getJson(pathname) {
  const res = await fetch(`http://127.0.0.1:${debugPort}${pathname}`);
  return res.json();
}

async function waitForDevtools() {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {
      const v = await getJson("/json/version");
      if (v.webSocketDebuggerUrl) return v.webSocketDebuggerUrl;
    } catch {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("devtools endpoint did not come up");
}

// Minimal CDP client over the browser-level websocket using flat sessions.
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
      } else if (msg.method) {
        for (const fn of this.listeners) fn(msg);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }
  on(fn) { this.listeners.push(fn); }
}

// The real short-video stream is served from the douyinvod / douyincdn CDNs,
// usually as SEPARATE adaptive tracks (media-video-avc1 / media-audio-*). We
// capture both and mux them later. Static UI assets must be ignored.
const STREAM_RE = /douyinvod\.com|douyincdn\.com|\/video\/tos\//i;
const STATIC_RE = /douyinstatic\.com|douyin-pc-web|\.js(\?|$)|\.css(\?|$)/i;
const AUDIO_RE = /media-audio|mime_type=audio|-audio-/i;

function isStream(u) {
  return !!u && STREAM_RE.test(u) && !STATIC_RE.test(u);
}
function isAudio(u) {
  return AUDIO_RE.test(u);
}

const DEBUG = !!arg("debug", "") || !!process.env.DY_DEBUG;
function absUrl(u) {
  if (!u) return u;
  if (u.startsWith("//")) return "https:" + u;
  return u;
}

// Douyin's web detail API carries the FULL gear list (video.bit_rate[]) for the
// target aweme; each gear's play_addr.url_list is a muxed progressive MP4. We read
// that and pick the highest-resolution gear, instead of the low gear the player
// happens to stream. wantId scopes the pick to the target video (the same response
// machinery also serves related/favorite videos we must ignore).
const DETAIL_RE = /\/aweme\/v1\/web\/aweme\/detail\//;
const wantId = (url.match(/video\/(\d+)/) || [])[1] || null;

function pickBestGear(bitRate) {
  // Same resolution, different codecs: H.265 carries ~1.7x the quality per bit of
  // H.264, and Douyin's H.265 gear often LOOKS better than the higher-numbered
  // H.264 one (measured: h264 981kbps vs h265 767kbps ≈ 1.3Mbps h264-equivalent).
  // Compare by codec-adjusted effective bitrate, not the raw number.
  let best = null;
  for (const g of bitRate || []) {
    const pa = g.play_addr || g.playAddr || {};
    const urls = pa.url_list || pa.urlList || [];
    if (!urls.length) continue;
    const w = pa.width || 0, h = pa.height || 0, br = g.bit_rate || g.bitRate || 0;
    const h265 = (g.is_h265 ?? g.isH265 ?? 0) === 1;
    const eff = br * (h265 ? 1.7 : 1.0);
    if (DEBUG) console.error(`gear: ${w}x${h} @${br} eff=${Math.round(eff)} ${g.gear_name || g.gearName || ""} ${g.format || ""} h265=${h265 ? 1 : 0}`);
    const area = w * h, barea = best ? best.w * best.h : -1;
    if (!best || area > barea || (area === barea && eff > best.eff)) {
      best = { w, h, br, eff, gear: g.gear_name || g.gearName || "", url: urls[0] };
    }
  }
  return best;
}

async function main() {
  const browserWs = await waitForDevtools();
  const ws = new WebSocket(browserWs);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res);
    ws.addEventListener("error", () => rej(new Error("browser ws error")));
  });
  const cdp = new CDP(ws);

  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });

  let video = null;
  let audio = null;
  const detailReqs = [];
  cdp.on((msg) => {
    if (msg.method === "Network.responseReceived" && msg.params?.response) {
      const u = msg.params.response.url;
      if (DETAIL_RE.test(u)) { detailReqs.push({ id: msg.params.requestId, session: msg.sessionId }); return; }
      if (!isStream(u)) return;
      if (isAudio(u)) { if (!audio) audio = u; }
      else if (!video) video = u;
    }
  });

  await cdp.send("Network.enable", {}, sessionId);
  await cdp.send("Page.enable", {}, sessionId);
  if (cookies.length) {
    await cdp.send("Network.setCookies", { cookies }, sessionId);
  }
  await cdp.send("Page.navigate", { url }, sessionId);

  const deadline = Date.now() + timeout;
  let title = null;
  let duration = null;
  let videoReadySince = 0;
  let bestGear = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 700));
    try {
      const ev = await cdp.send(
        "Runtime.evaluate",
        {
          expression: `(() => {
            const v = document.querySelector('video');
            if (v) { v.muted = true; v.play && v.play().catch(() => {}); }
            return JSON.stringify({ src: v && (v.currentSrc || v.src) || '', dur: v ? v.duration : null, title: document.title });
          })()`,
          returnByValue: true,
        },
        sessionId,
      );
      const data = JSON.parse(ev.result.value || "{}");
      // Douyin's verification interstitial — bail immediately with a clear reason
      // rather than spinning until the deadline.
      if (/验证|verify|captcha|安全验证/i.test(data.title || "")) {
        fail("Douyin yêu cầu xác minh (CAPTCHA). Cần cập nhật cookie Douyin.");
      }
      title = (data.title || "").replace(/ - 抖音$/, "").trim() || title;
      if (data.dur && isFinite(data.dur)) duration = data.dur;
      if (isStream(data.src) && !isAudio(data.src) && !video) video = data.src;
    } catch {}
    // Parse the detail-API gear list and keep the highest-resolution gear for the
    // target aweme. Once found it wins over the player's sniffed default.
    if (!bestGear) {
      for (const req of detailReqs) {
        if (req.done) continue;
        try {
          const b = await cdp.send("Network.getResponseBody", { requestId: req.id }, req.session);
          req.done = true; // body was ready; don't re-fetch
          const j = JSON.parse(b.body || "{}");
          const aw = j.aweme_detail || j.awemeDetail || (Array.isArray(j.aweme_list) ? j.aweme_list[0] : null);
          if (aw && (!wantId || String(aw.aweme_id) === String(wantId))) {
            const v = aw.video || {};
            const cand = pickBestGear(v.bit_rate || v.bitRateList);
            if (cand && cand.url) {
              bestGear = cand;
              if (aw.desc) title = String(aw.desc).trim();
              if (v.duration) duration = v.duration / 1000; // ms -> s
              if (DEBUG) console.error(`best gear: ${cand.w}x${cand.h} @${cand.br} ${cand.gear}`);
            }
          }
        } catch { /* body not ready yet — retry next loop */ }
      }
    }
    // TRUSTED: gear from the detail API, scoped to the target aweme_id — this is the
    // only source we can prove belongs to the requested video.
    if (bestGear) done({ video: absUrl(bestGear.url), muxed: true, width: bestGear.w, height: bestGear.h, title, duration });
    // Sniffed adaptive tracks carry no id, so a stream we grab off the wire could be an
    // autoplay RECOMMENDATION, not the target — using it would dub the wrong video. Only
    // trust the sniff when the URL had no id to match against (best-effort last resort).
    if (!wantId) {
      if (video && audio) done({ video, audio, title, duration });
      if (video && !videoReadySince) videoReadySince = Date.now();
      if (video && Date.now() - videoReadySince > 6000) done({ video, audio, title, duration });
    }
  }
  if (bestGear) done({ video: absUrl(bestGear.url), muxed: true, width: bestGear.w, height: bestGear.h, title, duration });
  if (!wantId && video) done({ video, audio, title, duration });
  fail(wantId
    ? "Không lấy được đúng video mục tiêu (detail-API của Douyin không phản hồi — có thể bị rate-limit). Thử lại sau."
    : "timed out waiting for video stream URL");
}

main().catch((e) => fail(e.message));
