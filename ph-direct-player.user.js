// ==UserScript==
// @name         Pornhub 直连播放（绕过 get_media 返回空数组）
// @namespace    local.ph.directplayer
// @version      2.0
// @description  站点播放器拿不到播放流时，读取页面 flashvars 里的 m3u8，用页面自带的 Hls 或内置 MSE 播放器起播
// @match        https://*.pornhub.com/view_video.php*
// @match        https://*.pornhub.com/embed/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  var OVERLAY_ID = 'ph-direct-player-overlay';
  var BTN_ID = 'ph-direct-player-btn';

  // ---------- 1. 抠 flashvars ----------
  function extractFlashvars() {
    var scripts = document.scripts;
    for (var i = 0; i < scripts.length; i++) {
      var t = scripts[i].textContent || '';
      if (t.indexOf('flashvars_') === -1) continue;
      var head = t.match(/var\s+flashvars_\d+\s*=\s*/);
      if (!head) continue;
      var start = t.indexOf('{', head.index + head[0].length);
      if (start < 0) continue;
      var depth = 0, inStr = false, esc = false, end = -1;
      for (var j = start; j < t.length; j++) {
        var c = t.charAt(j);
        if (inStr) {
          if (esc) esc = false;
          else if (c === '\\') esc = true;
          else if (c === '"') inStr = false;
          continue;
        }
        if (c === '"') { inStr = true; continue; }
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) { end = j; break; } }
      }
      if (end < 0) continue;
      try { return JSON.parse(t.slice(start, end + 1)); } catch (e) { }
    }
    return null;
  }

  function pickStreams(fv) {
    var list = [];
    (fv.mediaDefinitions || []).forEach(function (d) {
      var u = d && d.videoUrl;
      if (!u || typeof u !== 'string') return;
      u = u.replace(/\\\//g, '/');
      if (u.indexOf('.m3u8') === -1) return;
      list.push({ url: u, height: Number(d.height) || 0, quality: String(d.quality || d.height || '?') });
    });
    if (!list.length && fv.videoUrl && /\.m3u8/.test(fv.videoUrl)) {
      list.push({ url: fv.videoUrl.replace(/\\\//g, '/'), height: 0, quality: '默认' });
    }
    list.sort(function (a, b) { return b.height - a.height; });
    return list;
  }

  // ---------- 2. 引擎 A：页面自带的 Hls（站点 CSP 只放行自家域名，别引外部 CDN） ----------
  function playWithPageHls(video, url, onState) {
    return new Promise(function (resolve, reject) {
      var Hls = window.Hls;
      if (!Hls || !Hls.isSupported()) return reject(new Error('页面没有可用的 Hls'));
      var hls;
      try { hls = new Hls({ maxBufferLength: 30, maxMaxBufferLength: 60 }); } catch (e) { return reject(e); }
      var settled = false;
      hls.on(Hls.Events.ERROR, function (_e, d) {
        if (!d || !d.fatal || settled) return;
        settled = true;
        try { hls.destroy(); } catch (e) { }
        reject(new Error('hls:' + d.details));
      });
      hls.on(Hls.Events.MANIFEST_PARSED, function () {
        onState('已就绪');
        video.play().catch(function () { });
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      setTimeout(function () { if (!settled) { settled = true; resolve(hls); } }, 8000);
    });
  }

  // ---------- 3. 引擎 B：内置最简 MSE 播放器（fMP4 分片，零依赖） ----------
  function playWithMse(video, masterUrl, onState) {
    onState('内置播放器加载中…');
    return fetch(masterUrl).then(function (r) {
      if (!r.ok) throw new Error('master HTTP ' + r.status);
      return r.text();
    }).then(function (master) {
      var lines = master.split(/\r?\n/);
      var codecs = null, variant = null;
      for (var i = 0; i < lines.length; i++) {
        if (lines[i].indexOf('#EXT-X-STREAM-INF') === 0) {
          var m = lines[i].match(/CODECS="([^"]+)"/);
          if (m) codecs = m[1];
          variant = lines[i + 1];
          break;
        }
      }
      var mediaUrl = variant ? new URL(variant, masterUrl).href : masterUrl;
      return fetch(mediaUrl).then(function (r2) { return r2.text(); }).then(function (media) {
        return { media: media, mediaUrl: mediaUrl, codecs: codecs };
      });
    }).then(function (ctx) {
      var media = ctx.media;
      var mapMatch = media.match(/#EXT-X-MAP:URI="([^"]+)"/);
      var segs = media.split(/\r?\n/).filter(function (l) { return l && l.charAt(0) !== '#'; })
        .map(function (l) { return new URL(l, ctx.mediaUrl).href; });
      if (!segs.length) throw new Error('播放列表里没有分片');
      var codec = ctx.codecs || 'avc1.64001f,mp4a.40.2';
      var ms = new MediaSource();
      video.src = URL.createObjectURL(ms);
      return new Promise(function (res, rej) {
        ms.addEventListener('sourceopen', res, { once: true });
        setTimeout(function () { rej(new Error('MSE 打开超时')); }, 8000);
      }).then(function () {
        var sb;
        try { sb = ms.addSourceBuffer('video/mp4; codecs="' + codec + '"'); }
        catch (e) { sb = ms.addSourceBuffer('video/mp4; codecs="avc1.64001f"'); }
        function appendBuf(buf) {
          return new Promise(function (res, rej) {
            sb.addEventListener('updateend', res, { once: true });
            sb.addEventListener('error', rej, { once: true });
            sb.appendBuffer(buf);
          });
        }
        function fetchBuf(u) { return fetch(u).then(function (r) { return r.arrayBuffer(); }); }
        var chain = Promise.resolve();
        if (mapMatch) chain = chain.then(function () { return fetchBuf(new URL(mapMatch[1], ctx.mediaUrl).href); }).then(appendBuf);
        segs.slice(0, 24).forEach(function (u) {
          chain = chain.then(function () { return fetchBuf(u); }).then(appendBuf);
        });
        return chain.then(function () {
          onState('内置播放器已缓冲 ' + Math.min(24, segs.length) + ' 段');
          video.play().catch(function () { });
        });
      });
    });
  }

  // ---------- 4. UI ----------
  function buildOverlay(streams) {
    var old = document.getElementById(OVERLAY_ID);
    if (old) old.remove();

    var host = document.querySelector('#player') || document.querySelector('.mainPlayerDiv') || document.body;
    var rect = host.getBoundingClientRect();

    var box = document.createElement('div');
    box.id = OVERLAY_ID;
    box.style.cssText = [
      'position:fixed', 'z-index:2147483000', 'background:#000',
      'left:' + Math.max(0, rect.left) + 'px', 'top:' + Math.max(0, rect.top) + 'px',
      'width:' + (rect.width > 200 ? rect.width : 960) + 'px',
      'height:' + (rect.height > 150 ? rect.height : 540) + 'px',
      'display:flex', 'flex-direction:column'
    ].join(';');

    var bar = document.createElement('div');
    bar.style.cssText = 'display:flex;gap:8px;align-items:center;padding:6px 8px;background:#141414;color:#ddd;font:12px/1.4 system-ui;flex-wrap:wrap';

    var label = document.createElement('span');
    label.textContent = '直连播放';
    label.style.cssText = 'color:#f90;font-weight:600';

    var sel = document.createElement('select');
    sel.style.cssText = 'background:#222;color:#eee;border:1px solid #444;padding:2px 4px;border-radius:4px';
    streams.forEach(function (s, i) {
      var o = document.createElement('option');
      o.value = String(i);
      o.textContent = s.height ? s.height + 'p' : s.quality;
      sel.appendChild(o);
    });

    var msg = document.createElement('span');
    msg.style.cssText = 'opacity:.7';

    var reload = document.createElement('button');
    reload.textContent = '重取地址';
    reload.style.cssText = 'background:#333;color:#eee;border:1px solid #555;border-radius:4px;padding:2px 8px;cursor:pointer';
    reload.onclick = function () { location.reload(); };

    var close = document.createElement('button');
    close.textContent = '关闭';
    close.style.cssText = 'margin-left:auto;background:#333;color:#eee;border:1px solid #555;border-radius:4px;padding:2px 8px;cursor:pointer';
    close.onclick = function () { box.remove(); };

    bar.appendChild(label);
    bar.appendChild(sel);
    bar.appendChild(msg);
    bar.appendChild(reload);
    bar.appendChild(close);

    var video = document.createElement('video');
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.style.cssText = 'flex:1;width:100%;background:#000';

    box.appendChild(bar);
    box.appendChild(video);
    document.body.appendChild(box);

    function start(idx) {
      var s = streams[idx];
      if (!s) return;
      msg.textContent = '连接中…';
      playWithPageHls(video, s.url, function (t) { msg.textContent = t; })
        .catch(function (e1) {
          console.warn('[直连] Hls 失败，切内置播放器:', e1.message);
          return playWithMse(video, s.url, function (t) { msg.textContent = t; });
        })
        .catch(function (e2) {
          msg.textContent = '失败：' + e2.message;
        });
    }

    sel.onchange = function () { start(Number(sel.value)); };
    start(0);
  }

  // ---------- 5. 触发 ----------
  function sitePlaying() {
    var vids = document.querySelectorAll('#player video, .mainPlayerDiv video');
    for (var i = 0; i < vids.length; i++) {
      var v = vids[i];
      var src = v.currentSrc || '';
      if (src && !/^data:/.test(src) && (v.readyState >= 1 || v.currentTime > 0)) return true;
    }
    return false;
  }

  function run(force) {
    var fv = extractFlashvars();
    if (!fv) return;
    var streams = pickStreams(fv);
    if (!streams.length) return;
    if (!force && sitePlaying()) return;
    buildOverlay(streams);
  }

  function addButton() {
    if (document.getElementById(BTN_ID)) return;
    var b = document.createElement('button');
    b.id = BTN_ID;
    b.textContent = '直连播放';
    b.title = '用页面里的 m3u8 直接起播（绕过 get_media）';
    b.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483000;background:#f90;color:#111;border:0;border-radius:18px;padding:8px 14px;font:600 13px system-ui;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.45)';
    b.onclick = function () { run(true); };
    document.body.appendChild(b);
  }

  function boot() {
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      var fv = extractFlashvars();
      if (fv) {
        clearInterval(timer);
        addButton();
        setTimeout(function () { run(false); }, 6000);
        setTimeout(function () { if (!sitePlaying()) run(true); }, 12000);
      } else if (tries > 40) {
        clearInterval(timer);
      }
    }, 500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
