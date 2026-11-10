// offline check: same extraction logic as the userscript, against a saved page
import { readFileSync } from "node:fs";
const html = readFileSync(process.env.TEMP + "\\p_6564e5338d29b.html", "utf8");

function extractFlashvars(src) {
  // build a fake list of script contents
  const re = /<script[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(src))) {
    const t = m[1];
    if (t.indexOf("flashvars_") === -1) continue;
    const head = t.match(/var\s+flashvars_\d+\s*=\s*/);
    if (!head) continue;
    const start = t.indexOf("{", head.index + head[0].length);
    let depth = 0, inStr = false, esc = false, end = -1;
    for (let j = start; j < t.length; j++) {
      const c = t[j];
      if (inStr) { if (esc) esc = false; else if (c === "\\") esc = true; else if (c === '"') inStr = false; continue; }
      if (c === '"') { inStr = true; continue; }
      if (c === "{") depth++; else if (c === "}") { depth--; if (depth === 0) { end = j; break; } }
    }
    if (end < 0) continue;
    try { return JSON.parse(t.slice(start, end + 1)); } catch (e) { console.log("parse fail:", e.message); }
  }
  return null;
}

const fv = extractFlashvars(html);
if (!fv) { console.log("NO FLASHVARS"); process.exit(1); }
console.log("vkey:", fv.vkey, "| title:", (fv.video_title||"").slice(0,40));
console.log("geo:", fv.geo, "| isp:", fv.isp, "| loggedIn:", fv.loggedIn, "| isPremium:", fv.isPremium);
const list = (fv.mediaDefinitions||[])
  .filter(d => d && typeof d.videoUrl === "string" && d.videoUrl.indexOf(".m3u8") > -1)
  .map(d => ({ h: d.height, q: d.quality, host: new URL(d.videoUrl.replace(/\\\//g,"/")).host }))
  .sort((a,b)=>b.h-a.h);
console.log("streams:", JSON.stringify(list));
