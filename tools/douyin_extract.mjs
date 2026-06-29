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
  cdp.on((msg) => {
    if (msg.method === "Network.responseReceived" && msg.params?.response) {
      const u = msg.params.response.url;
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
      title = (data.title || "").replace(/ - 抖音$/, "").trim() || title;
      if (data.dur && isFinite(data.dur)) duration = data.dur;
      if (isStream(data.src) && !isAudio(data.src) && !video) video = data.src;
    } catch {}
    // Once we have video, give audio a short grace window to arrive, then ship.
    if (video && audio) done({ video, audio, title, duration });
    if (video && !videoReadySince) videoReadySince = Date.now();
    if (video && Date.now() - videoReadySince > 6000) done({ video, audio, title, duration });
  }
  if (video) done({ video, audio, title, duration });
  fail("timed out waiting for video stream URL");
}

main().catch((e) => fail(e.message));
