// cdp-probe5.mjs - verify media chain by feeding flashvars m3u8 into hls.js inside the page
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PROXY = process.env.PROBE_PROXY || 'http://127.0.0.1:65532';
const PORT = Number(process.env.PROBE_PORT || 9337);
const videoUrl = process.argv[2];
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const child = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${path.join(os.tmpdir(), 'cdpprof5-' + Date.now())}`,
  `--proxy-server=${PROXY}`, '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--autoplay-policy=no-user-gesture-required', '--window-size=1440,2200',
  `--user-agent=${UA}`, '--lang=en-US', 'about:blank',
], { stdio: 'ignore' });

const log = (...a) => console.log(...a);
let ws, id = 0;
const pending = new Map();
function send(method, params = {}, timeout = 60000) {
  const mid = ++id;
  return new Promise((resolve, reject) => {
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); reject(new Error('timeout ' + method)); } }, timeout);
  });
}
async function ev(expression, awaitPromise = false) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
  if (r.exceptionDetails) return { __error: (r.exceptionDetails.exception?.description || r.exceptionDetails.text || '').slice(0, 500) };
  return r.result?.value;
}

const EXPR = `(async () => {
  const out = {};
  const scripts = [...document.scripts].map(s => s.textContent || '').filter(t => t.includes('flashvars_'));
  const m = scripts[0] && scripts[0].match(/"videoUrl"\\s*:\\s*"([^"]{20,900})/);
  const m3u8 = m ? m[1].replace(/\\u002F/g, '/').replace(/\\\\\\//g, '/').replace(/\\\\/g, '') : null;
  out.m3u8 = m3u8 ? m3u8.slice(0, 200) : 'NOT-FOUND';
  if (!m3u8) return out;
  try {
    const r = await fetch(m3u8, { credentials: 'omit' });
    const t = await r.text();
    out.m3u8Fetch = { status: r.status, len: t.length, head: t.slice(0, 80) };
  } catch (e) { out.m3u8Fetch = { err: String(e).slice(0, 120) }; }
  try {
    const mod = await import('https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js');
    const Hls = mod.default || window.Hls;
    const v = document.createElement('video');
    v.muted = true; v.playsInline = true;
    v.style.cssText = 'position:fixed;left:0;top:0;width:320px;height:180px;z-index:999999;background:#000';
    document.body.appendChild(v);
    const errors = [];
    const hls = new Hls({ maxBufferLength: 20 });
    window.__probe = { hls, v };
    hls.on(Hls.Events.ERROR, (e, d) => errors.push({ type: d.type, details: d.details, fatal: !!d.fatal, reason: String(d.reason || '').slice(0, 90) }));
    hls.on(Hls.Events.MANIFEST_PARSED, () => { try { v.play(); } catch (e) {} });
    hls.loadSource(m3u8);
    hls.attachMedia(v);
    await new Promise(r => setTimeout(r, 30000));
    const segs = performance.getEntriesByType('resource').filter(e => e.name.indexOf('.m4s') > -1 || e.name.indexOf('.ts') > -1);
    out.playback = {
      readyState: v.readyState, networkState: v.networkState,
      currentTime: +v.currentTime.toFixed(2),
      duration: Number.isFinite(v.duration) ? +v.duration.toFixed(1) : String(v.duration),
      buffered: v.buffered.length ? +v.buffered.end(v.buffered.length - 1).toFixed(2) : 0,
      err: v.error ? { code: v.error.code, msg: v.error.message } : null,
      hlsErrors: errors.slice(0, 6),
      level: hls.currentLevel, levels: hls.levels ? hls.levels.length : 0,
      segmentReqs: segs.length,
      lastSegBytes: segs.length ? Math.round(segs[segs.length - 1].transferSize) : 0
    };
  } catch (e) { out.hlsTest = 'FAILED: ' + String(e).slice(0, 250); }
  return out;
})()`;

try {
  let page;
  for (let i = 0; i < 80 && !page; i++) {
    try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === 'page'); } catch { }
    if (!page) await sleep(250);
  }
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      if (m.error) p.reject(new Error(JSON.stringify(m.error))); else p.resolve(m.result);
    }
  });
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });

  log('[nav]', videoUrl);
  await send('Page.navigate', { url: videoUrl });
  await sleep(8000);
  const res = await ev(EXPR, true);
  log('[hls test]', JSON.stringify(res, null, 2));
} catch (e) {
  log('[fatal]', e.message);
} finally {
  try { ws?.close(); } catch { }
  try { child.kill(); } catch { }
  await sleep(300);
  process.exit(0);
}
