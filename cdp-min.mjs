import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import os from 'node:os'; import path from 'node:path';
const PORT=9411;
const udd = path.join(os.tmpdir(),'cdpmin-'+Date.now());
const c = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
 '--headless=new',`--remote-debugging-port=${PORT}`,`--user-data-dir=${udd}`,
 '--no-first-run','--no-default-browser-check','--disable-gpu','about:blank'], {stdio:'ignore'});
let page;
for (let i=0;i<80 && !page;i++){
  try { const j = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = j.find(t=>t.type==='page'); if(!page) console.log('targets:', j.map(t=>t.type).join(',')); } catch(e){}
  if(!page) await sleep(250);
}
console.log('page:', page && page.url, page && page.webSocketDebuggerUrl);
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r,j)=>{ws.addEventListener('open',r);ws.addEventListener('error',j)});
console.log('ws open');
ws.send(JSON.stringify({id:1,method:'Runtime.enable',params:{}}));
const got = await new Promise((res)=>{ const t=setTimeout(()=>res('TIMEOUT'),8000); ws.addEventListener('message',e=>{ if(JSON.parse(e.data).id===1){clearTimeout(t);res('OK')} }); });
console.log('Runtime.enable ->', got);
try{c.kill()}catch{}
process.exit(0);
