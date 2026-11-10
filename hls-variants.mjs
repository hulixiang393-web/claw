// hls-variants.mjs - test site Hls with different XHR/credential settings against flashvars m3u8
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os';
import path from 'node:path';

const PORT = Number(process.env.PROBE_PORT || 9460);
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';
const child = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${path.join(os.tmpdir(), 'hlsv2-' + Date.now())}`,
  '--proxy-server=http://127.0.0.1:65532', '--proxy-bypass-list=<-loopback>',
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--autoplay-policy=no-user-gesture-required', `--user-agent=${UA}`, 'about:blank',
], { stdio: 'ignore' });

let ws, id = 0; const pending = new Map();
const send = (m, p = {}, t = 90000) => new Promise((res, rej) => {
  const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p }));
  setTimeout(() => { if (pending.has(i)) { pending.delete(i); rej(new Error('timeout ' + m)); } }, t);
});
const ev = async (e, a = false) => {
  const r = await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: a });
  return r.exceptionDetails ? { __error: (r.exceptionDetails.exception?.description || r.exceptionDetails.text || '').slice(0, 300) } : r.result?.value;
};

const EXTRACT = `
function grab() {
  for (const s of document.scripts) {
    const t = s.textContent || '';
    if (t.indexOf('flashvars_') === -1) continue;
    const head = t.match(/var\\s+flashvars_\\d+\\s*=\\s*/);
    if (!head) continue;
    const start = t.indexOf('{', head.index + head[0].length);
    let d = 0, inStr = false, esc = false, end = -1;
    for (let i = start; i < t.length; i++) {
      const c = t[i];
      if (inStr) { if (esc) esc = false; else if (c === '\\\\') esc = true; else if (c === '"') inStr = false; continue; }
      if (c === '"') { inStr = true; continue; }
      if (c === '{') d++; else if (c === '}') { d--; if (d === 0) { end = i; break; } }
    }
    if (end < 0) continue;
    try { return JSON.parse(t.slice(start, end + 1)); } catch (e) { }
  }
  return null;
}`;

try {
  let page;
  for (let i = 0; i < 80 && !page; i++) {
    try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === 'page'); } catch { }
    if (!page) await sleep(250);
  }
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } });
  await send('Runtime.enable'); await send('Page.enable');
  await send('Emulation.setUserAgentOverride', { userAgent: UA });
  await send('Page.navigate', { url: process.argv[2] || 'https://www.pornhub.com/view_video.php?viewkey=6ab2b1eac488a' });

  for (let i = 0; i < 25; i++) {
    const ready = await ev(`(() => !!${''}document.querySelector('#player') && typeof window.Hls === 'function')()`);
    if (ready) break;
    await sleep(1000);
  }
  await sleep(3000);

  const out = await ev(`(async () => {
    ${EXTRACT}
    const fv = grab();
    const res = { hlsVersion: window.Hls && window.Hls.version, isSupported: window.Hls && window.Hls.isSupported() };
    if (!fv) return { ...res, err: 'no flashvars' };
    const d = (fv.mediaDefinitions || []).find(x => (x.format === 'hls' || /m3u8/.test(x.videoUrl || '')) && x.height >= 480) || (fv.mediaDefinitions || [])[0];
    const url = d.videoUrl.replace(/\\\\\\//g, '/');
    res.url = url.slice(0, 100);
    res.defs = (fv.mediaDefinitions || []).map(x => x.height + '/' + x.format);

    const probe = (label, cfg) => new Promise(done => {
      const v = document.createElement('video');
      v.muted = true; v.style.cssText = 'position:fixed;left:0;top:0;width:200px;height:120px;z-index:99999';
      document.body.appendChild(v);
      let h;
      try { h = new window.Hls(cfg || undefined); } catch (e) { done({ label, ctorErr: String(e).slice(0, 100) }); return; }
      const errs = [];
      h.on(window.Hls.Events.ERROR, (_e, x) => errs.push(x.details + (x.fatal ? '(FATAL)' : '')));
      h.on(window.Hls.Events.MANIFEST_PARSED, () => { try { v.play(); } catch (e) { } });
      h.loadSource(url); h.attachMedia(v);
      setTimeout(() => {
        done({ label, rs: v.readyState, t: +v.currentTime.toFixed(2), buf: v.buffered.length ? +v.buffered.end(0).toFixed(1) : 0, levels: h.levels ? h.levels.length : 0, errs: errs.slice(0, 3) });
        try { h.destroy(); v.remove(); } catch (e) { }
      }, 15000);
    });

    res.A_default = await probe('default');
    res.B_noCred = await probe('withCredentials=false', { xhrSetup: x => { x.withCredentials = false; } });
    return res;
  })()`, true);
  console.log(JSON.stringify(out, null, 2));
} catch (e) { console.log('[fatal]', e.message); }
finally { try { ws?.close(); } catch { } try { child.kill(); } catch { } await sleep(200); process.exit(0); }
