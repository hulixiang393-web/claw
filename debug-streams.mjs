// debug-streams.mjs - list every mediaDefinition on the page and probe each URL
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PROXY = 'http://127.0.0.1:65532';
const PORT = Number(process.env.PROBE_PORT || 9430);
const videoUrl = process.argv[2];
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const child = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${path.join(os.tmpdir(), 'dbg-' + Date.now())}`,
  `--proxy-server=${PROXY}`, '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--autoplay-policy=no-user-gesture-required', '--window-size=1440,2000',
  `--user-agent=${UA}`, '--lang=en-US', 'about:blank',
], { stdio: 'ignore' });

const log = (...a) => console.log(...a);
let ws, id = 0; const pending = new Map();
function send(method, params = {}, timeout = 60000) {
  const mid = ++id;
  return new Promise((res, rej) => {
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); rej(new Error('timeout ' + method)); } }, timeout);
  });
}
async function ev(expr, awaitPromise = false) {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
  if (r.exceptionDetails) return { __error: (r.exceptionDetails.exception?.description || '').slice(0, 400) };
  return r.result?.value;
}

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
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); }
  });
  await send('Runtime.enable'); await send('Page.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });
  await send('Page.navigate', { url: videoUrl });
  await sleep(8000);

  const out = await ev(`(async () => {
    const s = [...document.scripts].map(x => x.textContent || '').filter(t => t.includes('flashvars_'))[0] || '';
    const head = s.match(/var\\s+flashvars_\\d+\\s*=\\s*/);
    let fv = null;
    if (head) {
      const start = s.indexOf('{', head.index + head[0].length);
      let d = 0, inStr = false, esc = false, end = -1;
      for (let i = start; i < s.length; i++) {
        const c = s[i];
        if (inStr) { if (esc) esc = false; else if (c === '\\\\') esc = true; else if (c === '"') inStr = false; continue; }
        if (c === '"') { inStr = true; continue; }
        if (c === '{') d++; else if (c === '}') { d--; if (d === 0) { end = i; break; } }
      }
      try { fv = JSON.parse(s.slice(start, end + 1)); } catch (e) { return { parseErr: String(e) }; }
    }
    if (!fv) return { noFlashvars: true, scriptLens: [...document.scripts].map(x => (x.textContent||'').length).filter(l => l > 1000) };
    const defs = (fv.mediaDefinitions || []).map(d => ({ q: d.quality, h: d.height, f: d.format, url: d.videoUrl }));
    const res = [];
    for (const d of defs) {
      try {
        const r = await fetch(d.url, { credentials: 'omit' });
        const t = await r.text();
        res.push({ q: d.q, h: d.h, status: r.status, len: t.length, head: t.slice(0, 40).replace(/\\n/g, '|'), host: new URL(d.url).host });
      } catch (e) { res.push({ q: d.q, h: d.h, err: String(e).slice(0, 80) }); }
    }
    return { vkey: fv.vkey, videoUrl: (fv.videoUrl || '').slice(0, 120), defs: defs.map(d => ({ q: d.q, h: d.h, host: (() => { try { return new URL(d.url).host; } catch { return '?'; } })() })), res };
  })()`, true);
  log('[streams]', JSON.stringify(out, null, 2));
} catch (e) { log('[fatal]', e.message); }
finally { try { ws?.close(); } catch { } try { child.kill(); } catch { } await sleep(300); process.exit(0); }
