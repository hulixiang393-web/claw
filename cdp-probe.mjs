// cdp-probe.mjs — 用真实 Chrome 走本地代理跑一遍，抓网络/播放器真实状态
// 用法: node cdp-probe.mjs <url> [waitMs]
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PROXY  = process.env.PROBE_PROXY || 'http://127.0.0.1:65532';
const PORT   = Number(process.env.PROBE_PORT || 9333);
const target = process.argv[2] || 'https://www.pornhub.com/';
const waitMs = Number(process.argv[3] || 12000);
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const udd = path.join(os.tmpdir(), 'cdpprof-' + Date.now());
const child = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${udd}`,
  `--proxy-server=${PROXY}`,
  '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--autoplay-policy=no-user-gesture-required',
  '--window-size=1440,2600',
  `--user-agent=${UA}`,
  '--lang=en-US',
  'about:blank',
], { stdio: 'ignore' });

const log = (...a) => console.log(...a);

async function waitForDevtools() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const j = await r.json();
      const page = j.find(t => t.type === 'page');
      if (page) return page;
    } catch {}
    await sleep(250);
  }
  throw new Error('devtools not ready');
}

let ws, msgId = 0;
const pending = new Map();
const requests = new Map();
const responses = [];
const failures = [];
const consoleMsgs = [];
const bodies = {};

function send(method, params = {}, sessionless = true) {
  const id = ++msgId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error('timeout ' + method)); } }, 30000);
  });
}

function classify(url) {
  if (/\.m3u8/.test(url)) return 'hls-playlist';
  if (/\.(m4s|ts)(\?|$)/.test(url)) return 'hls-segment';
  if (/get_media/.test(url)) return 'get_media';
  if (/\.(jpg|jpeg|webp|png|gif)/.test(url)) return 'image';
  if (/\/view_video\.php/.test(url)) return 'video-page';
  if (/(\.js|\.css)(\?|$)/.test(url)) return 'asset';
  if (/\/video\/search|\/video\?/.test(url)) return 'search-page';
  return 'other';
}

const HOSTS = /phncdn\.com|pspcdn\.com|pornhub\.(com|net)/;

try {
  const page = await waitForDevtools();
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });

  ws.addEventListener('message', async (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) reject(new Error(JSON.stringify(m.error))); else resolve(m.result);
      return;
    }
    switch (m.method) {
      case 'Network.requestWillBeSent':
        requests.set(m.params.requestId, { url: m.params.request.url, type: m.params.type });
        break;
      case 'Network.responseReceived': {
        const { response, requestId } = m.params;
        if (HOSTS.test(response.url)) {
          responses.push({ url: response.url, status: response.status, mime: response.mimeType, cache: response.fromDiskCache || response.fromMemoryCache });
        }
        (globalThis.__respIds ||= {})[response.url] = requestId;
        break;
      }
      case 'Network.loadingFailed': {
        const req = requests.get(m.params.requestId);
        if (req && HOSTS.test(req.url)) {
          failures.push({ url: req.url, error: m.params.errorText, blocked: m.params.blockedReason || null, type: m.params.type });
        }
        break;
      }
      case 'Runtime.consoleAPICalled':
        consoleMsgs.push({ type: m.params.type, text: (m.params.args || []).map(a => a.value ?? a.description ?? a.type).join(' ').slice(0, 300) });
        break;
      case 'Runtime.exceptionThrown':
        consoleMsgs.push({ type: 'exception', text: (m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text || '').slice(0, 300) });
        break;
      case 'Log.entryAdded':
        consoleMsgs.push({ type: 'log:' + m.params.entry.level, text: String(m.params.entry.text).slice(0, 300) });
        break;
    }
  });

  await send('Network.enable', { maxResourceBufferSize: 50 * 1024 * 1024, maxTotalBufferSize: 200 * 1024 * 1024 });
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });

  log(`[nav] ${target}`);
  await send('Page.navigate', { url: target });
  await sleep(waitMs);

  // 先探活：主线程是否还能响应
  try {
    const t = await send('Runtime.evaluate', { expression: 'document.title + " | " + location.href', returnByValue: true });
    log('[alive]', JSON.stringify(t.result?.value));
  } catch (e) {
    log('[alive-fail]', e.message);
  }

  // 尝试起播 + 读播放器状态
  let evalRes = { result: { value: { note: 'evaluate failed' } } };
  try {
  evalRes = await send('Runtime.evaluate', {
    expression: `(async () => {
      const v = document.querySelector('video');
      let playErr = null;
      if (v) { try { v.muted = true; await v.play(); } catch (e) { playErr = String(e); } }
      await new Promise(r => setTimeout(r, 6000));
      const imgs = [...document.images];
      const errEls = [...document.querySelectorAll('[class*=error i],[class*=Error],[role=alert]')]
        .map(e => e.textContent.trim()).filter(t => t && t.length < 200).slice(0, 6);
      return {
        title: document.title,
        video: v ? {
          readyState: v.readyState, networkState: v.networkState,
          paused: v.paused, currentTime: +v.currentTime.toFixed(2),
          duration: Number.isFinite(v.duration) ? +v.duration.toFixed(1) : String(v.duration),
          buffered: v.buffered.length ? +v.buffered.end(v.buffered.length-1).toFixed(2) : 0,
          err: v.error ? { code: v.error.code, msg: v.error.message } : null,
          src: (v.currentSrc || '').slice(0, 180)
        } : null,
        playErr,
        images: { total: imgs.length, broken: imgs.filter(i => i.complete && i.naturalWidth === 0).length,
                  pending: imgs.filter(i => !i.complete).length },
        errEls,
        readyStateAfterPlay: v ? v.readyState : null
      };
    })()`,
    awaitPromise: true, returnByValue: true
  });
  } catch (e) { log('[dom-fail]', e.message); }
  log('[dom]', JSON.stringify(evalRes.result?.value, null, 2));

  // 抓 get_media 的响应体
  const gmUrls = Object.keys(globalThis.__respIds || {}).filter(u => /get_media/.test(u));
  for (const u of gmUrls.slice(0, 3)) {
    try {
      const b = await send('Network.getResponseBody', { requestId: globalThis.__respIds[u] });
      bodies[u.slice(0, 160)] = (b.body || '').slice(0, 400);
    } catch (e) { bodies[u.slice(0, 160)] = 'body-unavailable: ' + e.message.slice(0, 80); }
  }

  const summary = {
    host: new URL(target).host,
    counts: responses.reduce((acc, r) => { const k = classify(r.url) + ':' + (r.status >= 400 ? 'ERR' : 'OK'); acc[k] = (acc[k] || 0) + 1; return acc; }, {}),
    badResponses: responses.filter(r => r.status >= 400 || r.status === 0).slice(0, 25),
    failures: failures.slice(0, 25),
    getMediaBodies: bodies,
    console: consoleMsgs.filter(c => /error|warn|exception/i.test(c.type)).slice(0, 20),
    sampleHls: responses.filter(r => /m3u8|m4s/.test(r.url)).slice(0, 6)
  };
  log('[summary]', JSON.stringify(summary, null, 2));
} catch (e) {
  log('[fatal]', e.message);
} finally {
  try { ws?.close(); } catch {}
  try { child.kill(); } catch {}
  await sleep(300);
  process.exit(0);
}
