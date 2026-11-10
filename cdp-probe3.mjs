// cdp-probe3.mjs — 在页面上下文里直接打 get_media，并抓播放器初始化失败原因
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PROXY = process.env.PROBE_PROXY || 'http://127.0.0.1:65532';
const PORT = Number(process.env.PROBE_PORT || 9335);
const videoUrl = process.argv[2];
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const udd = path.join(os.tmpdir(), 'cdpprof3-' + Date.now());
const child = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`,
  `--proxy-server=${PROXY}`, '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--autoplay-policy=no-user-gesture-required', '--window-size=1440,2000',
  `--user-agent=${UA}`, '--lang=en-US', 'about:blank',
], { stdio: 'ignore' });

const log = (...a) => console.log(...a);
let ws, id = 0;
const pending = new Map();
const consoleLog = [];
const exceptions = [];

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
  if (r.exceptionDetails) return { __error: (r.exceptionDetails.exception?.description || r.exceptionDetails.text || '').slice(0, 300) };
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
    if (m.method === 'Runtime.consoleAPICalled')
      consoleLog.push({ t: m.params.type, s: (m.params.args || []).map(a => a.value ?? a.description ?? a.type).join(' ').slice(0, 240) });
    if (m.method === 'Runtime.exceptionThrown')
      exceptions.push(String(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text || '').slice(0, 400));
  });

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Network.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });

  log('[nav]', videoUrl);
  await send('Page.navigate', { url: videoUrl });
  await sleep(9000);

  // 1) 页面上下文里直接请求 get_media（与播放器同 cookie / 同出口）
  const gm = await ev(`(async () => {
    const vk = new URL(location.href).searchParams.get('viewkey');
    const out = {};
    const tryOne = async (label, url, opts) => {
      try {
        const r = await fetch(url, opts);
        const txt = await r.text();
        out[label] = { status: r.status, len: txt.length, body: txt.slice(0, 300) };
      } catch (e) { out[label] = { err: String(e) }; }
    };
    const h = { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json, text/javascript, */*; q=0.01' };
    await tryOne('viewkey_get', '/video/get_media?viewkey=' + vk, { headers: h, credentials: 'include' });
    await tryOne('viewkey_get_pc', '/video/get_media?viewkey=' + vk + '&platform=pc', { headers: h, credentials: 'include' });
    await tryOne('viewkey_post', '/video/get_media', { method: 'POST', headers: { ...h, 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'viewkey=' + vk + '&platform=pc', credentials: 'include' });
    const fv = document.querySelector('#player script, script[type="text/javascript"]');
    const scripts = [...document.scripts].map(s => s.textContent || '').filter(t => t.includes('flashvars_'));
    if (scripts[0]) {
      const m = scripts[0].match(/"videoUrl"\\s*:\\s*"([^"]{20,300})/);
      out.flashvarsHasVideoUrl = !!m;
      out.firstVideoUrl = m ? m[1].slice(0, 160).replace(/\\\\\\//g, '/') : null;
    }
    out.cookieLen = document.cookie.length;
    return out;
  })()`, true);
  log('[get_media in page]', JSON.stringify(gm, null, 2));

  // 2) 播放器是否被墙在初始化
  log('[player state]', JSON.stringify(await ev(`(() => {
    const p = document.querySelector('#player');
    const v = document.querySelector('#player video') || document.querySelectorAll('video')[5];
    return {
      playerExists: !!p,
      playerChildren: p ? p.children.length : 0,
      playerClasses: p ? p.className : null,
      videoCount: document.querySelectorAll('video').length,
      mainVideo: v ? { rs: v.readyState, ns: v.networkState, src: v.currentSrc, poster: (v.poster||'').slice(0,80) } : null,
      playerText: p ? p.innerText.slice(0, 200) : null,
      resourceCount: performance.getEntriesByType('resource').length,
      phncdnHits: performance.getEntriesByType('resource').filter(e => /phncdn|get_media|m3u8|m4s/.test(e.name)).length
    };
  })()`), null, 2));

  // 3) 点#player 中心
  const box = await ev(`(() => { const p = document.querySelector('#player'); if(!p) return null; const r = p.getBoundingClientRect(); return { x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height }; })()`);
  log('[player box]', JSON.stringify(box));
  if (box && box.w > 100) {
    for (const type of ['mousePressed', 'mouseReleased']) {
      await send('Input.dispatchMouseEvent', { type, x: box.x, y: box.y, button: 'left', clickCount: 1 });
    }
    log('[click] dispatched at player center');
  }
  await sleep(12000);

  log('[post-click]', JSON.stringify(await ev(`(() => {
    const v = [...document.querySelectorAll('video')].sort((a,b)=> b.clientWidth-a.clientWidth)[0];
    return v ? { rs: v.readyState, ns: v.networkState, paused: v.paused, t: +(v.currentTime||0).toFixed(2),
      dur: Number.isFinite(v.duration)?+v.duration.toFixed(1):String(v.duration),
      buffered: v.buffered.length ? +v.buffered.end(v.buffered.length-1).toFixed(2) : 0,
      err: v.error ? { code: v.error.code, msg: v.error.message } : null,
      src: (v.currentSrc||'').slice(0,200) } : 'none';
  })()`), null, 2));
  log('[hls resources]', JSON.stringify(await ev(`performance.getEntriesByType('resource').map(e=>e.name).filter(n=>/m3u8|m4s|get_media/.test(n)).slice(0,10)`), null, 2));

  log('[console]', JSON.stringify(consoleLog.filter(c => /error|warn/i.test(c.t)).slice(0, 15), null, 2));
  log('[exceptions]', JSON.stringify(exceptions.slice(0, 8), null, 2));
} catch (e) {
  log('[fatal]', e.message);
} finally {
  try { ws?.close(); } catch {}
  try { child.kill(); } catch {}
  await sleep(300);
  process.exit(0);
}
