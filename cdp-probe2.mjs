// cdp-probe2.mjs — 真 Chrome 走代理，定位"播放器没起播"到底卡在哪一步
// 用法: node cdp-probe2.mjs <videoPageUrl> [searchUrl]
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PROXY = process.env.PROBE_PROXY || 'http://127.0.0.1:65532';
const PORT = Number(process.env.PROBE_PORT || 9334);
const videoUrl = process.argv[2];
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const udd = path.join(os.tmpdir(), 'cdpprof2-' + Date.now());
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
const events = [];       // {kind, url, status, error, mime}
const reqMap = new Map();
const bodies = {};

function send(method, params = {}, timeout = 20000) {
  const mid = ++id;
  return new Promise((resolve, reject) => {
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); reject(new Error('timeout ' + method)); } }, timeout);
  });
}
async function ev(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true });
  if (r.exceptionDetails) return { __error: r.exceptionDetails.text + ' ' + (r.exceptionDetails.exception?.description || '') };
  return r.result?.value;
}

const WANT = /phncdn\.com|pspcdn\.com|pornhub\.(com|net)|get_media|\.m3u8|\.m4s|\.ts(\?|$)/;

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
    if (m.method === 'Network.requestWillBeSent') reqMap.set(m.params.requestId, m.params.request.url);
    if (m.method === 'Network.responseReceived') {
      const u = m.params.response.url;
      if (WANT.test(u)) {
        events.push({ kind: 'resp', url: u.slice(0, 200), status: m.params.response.status, mime: m.params.response.mimeType });
        bodies[u] = m.params.requestId;
      }
    }
    if (m.method === 'Network.loadingFailed') {
      const u = reqMap.get(m.params.requestId) || '';
      if (WANT.test(u)) events.push({ kind: 'fail', url: u.slice(0, 200), error: m.params.errorText, blocked: m.params.blockedReason || null });
    }
  });

  await send('Network.enable', { maxResourceBufferSize: 60 * 1024 * 1024, maxTotalBufferSize: 300 * 1024 * 1024 });
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });

  log(`[nav] ${videoUrl}`);
  await send('Page.navigate', { url: videoUrl });
  await sleep(10000);

  log('[title]', await ev('document.title'));
  log('[player-dom]', JSON.stringify(await ev(`(() => {
    const vs = [...document.querySelectorAll('video')];
    const cand = [...document.querySelectorAll('div,button,a')]
      .filter(e => typeof e.className === 'string' && /play/i.test(e.className) && e.className.length < 80)
      .slice(0, 8).map(e => e.tagName + '.' + e.className);
    return {
      videos: vs.map(v => ({ rs: v.readyState, ns: v.networkState, src: (v.currentSrc||'').slice(0,120), attr: v.getAttribute('src'), poster: (v.poster||'').slice(0,100), w: v.clientWidth, h: v.clientHeight })),
      playCandidates: cand,
      hasMsgp: !!document.querySelector('[class*=mgp]'),
      playerHtmlHead: (document.querySelector('[class*=player i], #player, .video-wrapper')?.outerHTML || '').slice(0, 500)
    };
  })()`), null, 2));

  // 点播放
  const clicked = await ev(`(() => {
    const p = document.querySelector('.mgp_playButton, .mgp_videoPlayer__playButton, [class*=playIcon], [class*=play-button], video');
    if (!p) return 'no-target';
    p.click();
    const v = document.querySelector('video');
    if (v) { v.muted = true; try { v.play(); } catch(e) {} }
    return 'clicked: ' + p.tagName + '.' + (typeof p.className === 'string' ? p.className : '');
  })()`);
  log('[click]', clicked);

  await sleep(12000);

  log('[after-click video]', JSON.stringify(await ev(`(() => {
    const v = document.querySelector('video');
    const imgs = [...document.images];
    return v ? {
      rs: v.readyState, ns: v.networkState, paused: v.paused,
      t: +(v.currentTime||0).toFixed(2),
      dur: Number.isFinite(v.duration) ? +v.duration.toFixed(1) : String(v.duration),
      buffered: v.buffered.length ? +v.buffered.end(v.buffered.length-1).toFixed(2) : 0,
      err: v.error ? { code: v.error.code, msg: v.error.message } : null,
      src: (v.currentSrc||'').slice(0,160),
      imgsBroken: imgs.filter(i => i.complete && i.naturalWidth === 0).length,
      imgsTotal: imgs.length
    } : 'no-video';
  })()`), null, 2));

  for (const u of Object.keys(bodies).filter(u => /get_media/.test(u)).slice(0, 3)) {
    try {
      const b = await send('Network.getResponseBody', { requestId: bodies[u] });
      log('[get_media body]', u.slice(0, 110), '=>', (b.body || '').slice(0, 500));
    } catch (e) { log('[get_media body]', 'unavailable', e.message.slice(0, 60)); }
  }

  const summary = events.reduce((a, e) => { const k = `${e.kind}:${e.status ?? e.error}`; a[k] = (a[k] || 0) + 1; return a; }, {});
  log('[net-summary]', JSON.stringify(summary, null, 2));
  log('[net-detail]', JSON.stringify(events.filter(e => /m3u8|m4s|get_media/.test(e.url)).slice(0, 20), null, 2));
  log('[net-bad]', JSON.stringify(events.filter(e => e.kind === 'fail' || (e.status || 0) >= 400).slice(0, 15), null, 2));
} catch (e) {
  log('[fatal]', e.message);
} finally {
  try { ws?.close(); } catch {}
  try { child.kill(); } catch {}
  await sleep(300);
  process.exit(0);
}
