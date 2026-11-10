// csp-test.mjs - check CSP and whether a <script src=jsdelivr> can load in page
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const PORT = Number(process.env.PROBE_PORT || 9440);
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';
const child = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${path.join(os.tmpdir(), 'csp-' + Date.now())}`,
  '--proxy-server=http://127.0.0.1:65532', '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu', `--user-agent=${UA}`, 'about:blank',
], { stdio: 'ignore' });

let ws, id = 0; const pending = new Map();
const send = (m, p = {}, t = 60000) => new Promise((res, rej) => {
  const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p }));
  setTimeout(() => { if (pending.has(i)) { pending.delete(i); rej(new Error('timeout ' + m)); } }, t);
});
const ev = async (e, a = false) => { const r = await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: a }); return r.exceptionDetails ? { __error: r.exceptionDetails.text } : r.result?.value; };

try {
  let page;
  for (let i = 0; i < 80 && !page; i++) {
    try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === 'page'); } catch { }
    if (!page) await sleep(250);
  }
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } });
  await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });
  await send('Page.navigate', { url: 'https://www.pornhub.com/view_video.php?viewkey=6ab2b1eac488a' });
  await sleep(6000);

  const r = await ev(`(async () => {
    const out = {};
    const tag = await new Promise(res => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js';
      s.onload = () => res('loaded');
      s.onerror = (e) => res('error: ' + (e.message || 'script error'));
      document.head.appendChild(s);
      setTimeout(() => res('timeout'), 15000);
    });
    out.scriptTag = tag;
    out.hlsAfterTag = typeof window.Hls;
    try { const m = await import('https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js'); out.importOk = !!(m.default || m); } catch (e) { out.importErr = String(e).slice(0, 120); }
    out.perf = performance.getEntriesByType('resource').filter(e => /jsdelivr/.test(e.name)).map(e => ({ n: e.name.slice(-40), size: e.transferSize, dur: Math.round(e.duration) }));
    return out;
  })()`, true);
  console.log('[csp/jsdelivr]', JSON.stringify(r, null, 2));
} catch (e) { console.log('[fatal]', e.message); }
finally { try { ws?.close(); } catch { } try { child.kill(); } catch { } await sleep(200); process.exit(0); }
