// Headless-Chromium Douyin USER post-list lister (zero npm deps, Node >= 22).
//
// Loads a creator's /user/<sec_uid> page so Douyin's own JS signs the request,
// scrolls to lazy-load more posts, captures the /aweme/v1/web/aweme/post/ JSON
// responses off the wire, and prints the deduped video list as JSON on stdout.
//
// Usage: node douyin_user_posts.mjs --url <userUrl> --cookies <cookies.json> --browser <path> [--limit 24] [--timeout 45000]

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
const limit = Number(arg("limit", "24"));
const timeout = Number(arg("timeout", "45000"));
const debugPort = Number(arg("port", "0")) || 9400 + (process.pid % 700);

if (!url) {
  console.error("missing --url");
  process.exit(2);
}

const cookies = cookiesPath ? JSON.parse(readFileSync(cookiesPath, "utf8")) : [];
const userDir = mkdtempSync(path.join(os.tmpdir(), "autoreup-dyu-"));

const proc = spawn(browser, [
  "--headless=new",
  `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${userDir}`,
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-gpu",
  "--mute-audio",
  "--disable-background-networking",
  "--window-size=1280,1600",
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

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.listeners = [];
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

const POST_RE = /\/aweme\/v1\/web\/aweme\/post\//;

function cover(a) {
  const c = (a.video && (a.video.cover || a.video.origin_cover || a.video.dynamic_cover)) || {};
  const list = c.url_list || c.urlList || [];
  return list[0] || null;
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

  const postReqs = [];
  cdp.on((msg) => {
    if (msg.method === "Network.responseReceived" && msg.params?.response) {
      if (POST_RE.test(msg.params.response.url)) postReqs.push({ id: msg.params.requestId, session: msg.sessionId });
    }
  });

  await cdp.send("Network.enable", {}, sessionId);
  await cdp.send("Page.enable", {}, sessionId);
  if (cookies.length) await cdp.send("Network.setCookies", { cookies }, sessionId);
  await cdp.send("Page.navigate", { url }, sessionId);

  const byId = new Map();
  let uploader = null;
  let hasMore = true;
  const deadline = Date.now() + timeout;

  async function drain() {
    for (const req of postReqs) {
      if (req.done) continue;
      try {
        const b = await cdp.send("Network.getResponseBody", { requestId: req.id }, req.session);
        req.done = true;
        const j = JSON.parse(b.body || "{}");
        if (typeof j.has_more !== "undefined") hasMore = !!j.has_more;
        for (const a of j.aweme_list || []) {
          if (!a.aweme_id || byId.has(a.aweme_id)) continue;
          if (!uploader && a.author) uploader = a.author.nickname || null;
          byId.set(a.aweme_id, {
            id: String(a.aweme_id),
            desc: (a.desc || "").trim(),
            duration: a.video && a.video.duration ? a.video.duration / 1000 : null,
            cover: cover(a),
          });
        }
      } catch { /* body not ready; retry next loop */ }
    }
  }

  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 800));
    await drain();
    if (byId.size >= limit) break;
    if (!hasMore && postReqs.some((r) => r.done)) break;
    // Scroll to the bottom to trigger the next page of posts.
    try {
      await cdp.send("Runtime.evaluate", { expression: "window.scrollTo(0, document.body.scrollHeight)" }, sessionId);
    } catch {}
  }
  await drain();

  const videos = [...byId.values()].slice(0, limit);
  if (!videos.length) fail("no posts captured (login/cookies or empty channel)");
  done({ videos, uploader });
}

main().catch((e) => fail(e.message));
