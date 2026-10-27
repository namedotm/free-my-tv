'use strict';
const token = location.hash.slice(1);
history.replaceState(null, '', '/');
const $ = id => document.getElementById(id);
let initialized = false, active = false, pendingSMB = false;
const seen = new Map();
function log(message) { $('log').textContent += new Date().toLocaleTimeString('ru') + '  ' + message + '\n'; $('log').scrollTop = $('log').scrollHeight; }
function payload() { return { ips: [0,1,2].map(i => $('ip'+i).value.trim()), folder: $('folder').value.trim(), apps: Array.from(document.querySelectorAll('[name=app]:checked')).map(x => x.value), official_youtube: $('official').checked }; }
async function api(path, data) {
  const r = await fetch(path, {method: data ? 'POST' : 'GET', headers: {'X-Manager-Token': token, 'Content-Type':'application/json'}, body: data ? JSON.stringify(data) : undefined});
  const b = await r.json(); if (!r.ok) throw Error(b.error || 'Нет доступа. Перезапустите START.bat и используйте открывшееся окно.'); return b;
}
async function action(name) {
  try {
    const p = payload(); p.action = name;
    if (name === 'measure' && !confirm('VLC откроется на экранах выбранных устройств. Продолжить замер?')) return;
    if (name === 'install' && !p.apps.length) throw Error('Выберите приложения.');
    const result = await api('/action', p); log(result.message);
    pendingSMB = name === 'install' && $('share').checked; active = true;
  } catch(e) { log(e.message); }
}
async function smb(restore) {
  if (restore && !confirm('Удалить только созданные менеджером SMB-ресурсы? Фильмы и приложения останутся. Windows запросит UAC.')) return;
  try { log((await api(restore ? '/restore' : '/smb', payload())).message); } catch(e) {log(e.message);}
}
for (const name of ['check','measure','retry','install','profile']) $(name).addEventListener('click', () => action(name));
$('browse').addEventListener('click', async () => { try { const r = await api('/folder',{}); if(r.message) $('folder').value = r.message; }catch(e){log(e.message);} });
$('smb').addEventListener('click', () => smb(false)); $('restore').addEventListener('click', () => smb(true));
function node(tag, text, cls) { const n = document.createElement(tag); if(text) n.textContent=text; if(cls)n.className=cls; return n; }
async function poll() {
  try {
    const s = await api('/status');
    if (!initialized) { (s.config.ips || []).forEach((ip,i) => $('ip'+i).value=ip); $('folder').value=s.config.folder || ''; $('official').checked=!!s.config.official_youtube; for (const x of document.querySelectorAll('[name=app]'))x.checked=(s.config.apps || []).includes(x.value); initialized=true; }
    $('status').textContent=s.message;
    for (const id of ['check','measure','retry','install','profile','smb','restore']) $(id).disabled=s.busy;
    const area = $('devices'); area.replaceChildren();
    for (const [ip,d] of Object.entries(s.rows)) {
      const card=node('article',null,'device '+d.status); card.append(node('strong',ip+(d.model?' · '+d.model:'')));
      const bar=node('progress'); bar.max=100; bar.value=d.progress || 0; card.append(bar);
      if(d.android)card.append(node('div',`Android ${d.android} · API ${d.api} · ${(d.abis || []).join(', ')} · свободно ${d.free_mb} МБ`,'details'));
      card.append(node('p',d.message));
      for(const [name,r] of Object.entries(d.apps || {})) card.append(node('div',name+' · '+r.message,'task'));
      area.append(card);
      if(seen.get(ip)!==d.message){log(ip+' · '+d.message);seen.set(ip,d.message);}
    }
    if(active && !s.busy){active=false;if(pendingSMB){pendingSMB=false;await smb(false);}}
  } catch(e) { $('status').textContent=e.message; }
  setTimeout(poll,1200);
}
poll();
