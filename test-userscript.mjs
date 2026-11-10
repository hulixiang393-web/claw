// test-userscript.mjs - inject ph-direct-player.user.js as a real userscript and verify playback
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PROXY = process.env.PROBE_PROXY || 'http://127.0.0.1:65532';
const PORT = Number(process.env.PROBE_PORT || 9420);
const videoUrl = process.argv[2];
const scriptFile = process.argv[3] || 'D:/code/claw/ph-direct-player.user.js';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';
const code = readFileSync(scriptFile, 'utf8');

const child = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${path.join(os.tmpdir(), 'us-verify-' + Date.now())}`,
  `--proxy-server=${PROXY}`, '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--autoplay-policy=no-user-gesture-required', '--window-size=1440,2000',
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
  if (r.exceptionDetails) return { __error: (r.exceptionDetails.exception?.description || r.exceptionDetails.text || '').slice(0, 400) };
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
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      if (m.error) p.reject(new Error(JSON.stringify(m.error))); else p.resolve(m.result);
    }
  });
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });

  await send('Page.addScriptToEvaluateOnNewDocument', { source: code });
  log('[inject] userscript armed, len=', code.length);

  await send('Page.navigate', { url: videoUrl });
  await sleep(9000);

  log('[step1 9s]', JSON.stringify(await ev(`(() => ({
    overlay: !!document.getElementById('ph-direct-player-overlay'),
    btn: !!document.getElementById('ph-direct-player-btn'),
    siteVideo: (() => { const v = document.querySelector('#player video'); return v ? { rs: v.readyState, src: (v.currentSrc||'').slice(0,60) } : null; })()
  }))()`)));

  await sleep(14000);
  const state = await ev(`(() => {
    const box = document.getElementById('ph-direct-player-overlay');
    const v = box && box.querySelector('video');
    return {
      overlay: !!box,
      msg: box ? (box.querySelector('span[style*="opacity"]')?.textContent || '') : '',
      video: v ? {
        rs: v.readyState, paused: v.paused,
        t: +(v.currentTime || 0).toFixed(2),
        dur: Number.isFinite(v.duration) ? +v.duration.toFixed(0) : String(v.duration),
        buffered: v.buffered.length ? +v.buffered.end(v.buffered.length - 1).toFixed(1) : 0,
        err: v.error ? { code: v.error.code, msg: v.error.message } : null,
        src: (v.currentSrc || '').slice(0, 70)
      } : null
    };
  })()`);
  log('[step2 23s]', JSON.stringify(state, null, 2));
} catch (e) {
  log('[fatal]', e.message);
} finally {
  try { ws?.close(); } catch { }
  try { child.kill(); } catch { }
  await sleep(300);
  process.exit(0);
}
