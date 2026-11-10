// cdp-probe4.mjs — 1) 页面内精确复现播放器请求(get_media?s=) 2) 搜索页封面坏图审计
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PROXY = process.env.PROBE_PROXY || 'http://127.0.0.1:65532';
const PORT = Number(process.env.PROBE_PORT || 9336);
const videoUrl = process.argv[2];
const searchUrl = process.argv[3] || 'https://www.pornhub.com/video/search?search=asian';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const child = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${path.join(os.tmpdir(),'cdpprof4-'+Date.now())}`,
  `--proxy-server=${PROXY}`, '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--autoplay-policy=no-user-gesture-required', '--window-size=1440,2200',
  `--user-agent=${UA}`, '--lang=en-US', 'about:blank',
], { stdio: 'ignore' });

const log = (...a) => console.log(...a);
let ws, id = 0;
const pending = new Map();
const respHeaders = {};

function send(method, params = {}, timeout = 30000) {
  const mid = ++id;
  return new Promise((resolve, reject) => {
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); reject(new Error('timeout ' + method)); } }, timeout);
  });
}
async function ev(expression, awaitPromise = false) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
  if (r.exceptionDetails) return { __error: (r.exceptionDetails.exception?.description || r.exceptionDetails.text || '').slice(0, 400) };
  return r.result?.value;
}

try {
  let page;
  for (let i = 0; i < 60 && !page; i++) {
    try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === 'page'); } catch {}
    if (!page) await sleep(250);
  }
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
      return;
    }
    if (m.method === 'Network.responseReceived' && /get_media/.test(m.params.response.url))
      respHeaders[m.params.response.url.slice(0, 120)] = m.params.response.headers;
  });

  await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });

  // ---------- A. 视频页：复现播放器真实请求 ----------
  log('[nav video]', videoUrl);
  await send('Page.navigate', { url: videoUrl });
  await sleep(8000);

  const gm = await ev(`(async () => {
    const html = document.documentElement.innerHTML;
    const urls = [...html.matchAll(/get_media\\?s=[^"'\\\\ ]{20,400}/g)].map(m => m[0].replace(/&amp;/g, '&'));
    const out = { foundS: urls.length, samples: urls.slice(0,2).map(u => u.slice(0,90)) };
    if (urls.length) {
      try {
        const r = await fetch('/' + urls[0], { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'include' });
        const t = await r.text();
        out.withS = { status: r.status, len: t.length, body: t.slice(0, 400) };
      } catch (e) { out.withS = { err: String(e) }; }
    }
    // 页面内直接验证 flashvars 里的 m3u8 是否可用
    const scripts = [...document.scripts].map(s => s.textContent || '').filter(t => t.includes('flashvars_'));
    const m = scripts[0] && scripts[0].match(/"videoUrl"\\s*:\\s*"([^"]{20,400})/);
    if (m) {
      const u = m[1].replace(/\\\\\\//g, '/');
      try {
        const r = await fetch(u, { headers: { 'Referer': location.href } });
        const t = await r.text();
        out.flashvarsM3u8 = { status: r.status, len: t.length, head: t.slice(0, 120) };
      } catch (e) { out.flashvarsM3u8 = { err: String(e) }; }
    }
    return out;
  })()`, true);
  log('[video-page api]', JSON.stringify(gm, null, 2));
  log('[get_media resp headers]', JSON.stringify(respHeaders, null, 2).slice(0, 1500));

  // ---------- B. 搜索页：封面坏图审计 ----------
  log('[nav search]', searchUrl);
  await send('Page.navigate', { url: searchUrl });
  await sleep(9000);
  for (let i = 0; i < 3; i++) {
    await ev('window.scrollBy(0, 1400); 1');
    await sleep(2500);
  }
  const audit = await ev(`(async () => {
    const imgs = [...document.images];
    const broken = imgs.filter(i => i.complete && i.naturalWidth === 0);
    const pending = imgs.filter(i => !i.complete);
    const hostOf = u => { try { return new URL(u, location.href).host; } catch { return '?' } };
    const byHost = {};
    for (const i of broken) { const h = hostOf(i.currentSrc || i.src); byHost[h] = (byHost[h]||0)+1; }
    // 直接重取前 5 个坏图，看是网络失败还是 HTTP 拒绝
    const probe = [];
    for (const i of broken.slice(0, 5)) {
      const u = i.currentSrc || i.src;
      try {
        const r = await fetch(u, { mode: 'cors', credentials: 'omit' });
        probe.push({ u: u.slice(0, 110), status: r.status, type: r.headers.get('content-type') });
      } catch (e) { probe.push({ u: u.slice(0, 110), err: String(e).slice(0, 90) }); }
    }
    return {
      total: imgs.length, broken: broken.length, pending: pending.length, byHost,
      sampleBroken: broken.slice(0, 5).map(i => (i.currentSrc || i.src).slice(0, 120)),
      probe
    };
  })()`, true);
  log('[image audit]', JSON.stringify(audit, null, 2));
} catch (e) {
  log('[fatal]', e.message);
} finally {
  try { ws?.close(); } catch {}
  try { child.kill(); } catch {}
  await sleep(300);
  process.exit(0);
}
