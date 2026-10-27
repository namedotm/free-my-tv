'use strict';
const tokenKey = 'xiaomi-tv-manager-' + location.host;
const token = location.hash.slice(1) || sessionStorage.getItem(tokenKey) || '';
if (token) sessionStorage.setItem(tokenKey, token);
history.replaceState(null, '', '/');
const $ = id => document.getElementById(id);
let initialized = false, active = false, pendingSMB = false;
const labels = {freemytv:'Free My TV', smarttube:'SmartTube', vlc:'VLC', byedpi:'ByeByeDPI', vimu:'Vimu', mx:'MX Player'};
const states = {queued:'В очереди', running:'Выполняется', done:'Готово', error:'Ошибка', skipped:'Пропущено', manual:'Действие на ТВ', warning:'Есть ручные шаги'};
const stages = {source:'Источник', download:'Скачивание', tools:'Инструменты', inspect:'Проверка APK', backup:'Резервная копия', install:'Установка', verify:'Проверка на ТВ'};
let lastEvent = 0;
const openedDetails = new Set();
const logLines = [];
function log(message) { logLines.push(new Date().toLocaleTimeString('ru') + '  ' + message); if(logLines.length>500)logLines.shift(); $('log').textContent=logLines.join('\n'); $('log').scrollTop=$('log').scrollHeight; }
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
function meter(value, max=100) {
  const p=node('progress'); p.max=max;
  if(value!==null)p.value=Math.min(value,max); return p;
}
function technical(parent, key, data) {
  if(!data.details && !data.code)return;
  const details=node('details'); details.dataset.detailKey=key; details.open=openedDetails.has(key);
  details.addEventListener('toggle',()=>details.open?openedDetails.add(key):openedDetails.delete(key));
  details.append(node('summary','Технические сведения'+(data.code?' · '+data.code:'')));
  details.append(node('pre',data.details || data.code)); parent.append(details);
}
function taskCard(key, task, ip) {
  const card=node('div',null,'task '+task.status);
  const title=node('div',null,'task-title');title.append(node('strong',labels[key] || key),node('span',states[task.status] || task.status,'state '+task.status));card.append(title);
  const current=task.stage;
  if(current && current!=='done') {
    const steps=node('div',null,'steps');
    for(const [id,label] of Object.entries(stages)) {
      const cls=id===current?(task.status==='error'?'failed':'current'):(task.completed_stages || []).includes(id)?'passed':'';
      steps.append(node('span',label,cls));
    }
    card.append(steps);
  }
  card.append(node('div',task.message,'task-message'));
  if(task.status==='running') {
    const info=task.stage_info || {};
    const received=Number(info.received || 0), total=Number(info.total || 0);
    if((current==='download' || current==='tools') && !info.waiting) {
      const percent=total?Math.min(100,Math.floor(100*received/total)):null;
      card.append(meter(percent));
      const mb=n=>(n/1048576).toFixed(1);
      card.append(node('div',`${mb(received)} МБ`+(total?` / ${mb(total)} МБ · ${percent}%`: ' · размер пока неизвестен')+(info.cached?' · из проверенного кэша':''),'details'));
    } else {
      card.append(meter(null));
      const elapsed=Math.max(0,Math.floor(Date.now()/1000-(task.stage_started || Date.now()/1000)));
      card.append(node('div',`${stages[current] || 'Обработка'} · ${elapsed} с`+(info.waiting?' · ожидает освобождения очереди':''),'details'));
    }
  }
  technical(card,ip+':'+key,task); return card;
}
async function poll() {
  try {
    const s = await api('/status');
    if (!initialized) { (s.config.ips || []).forEach((ip,i) => $('ip'+i).value=ip); $('folder').value=s.config.folder || ''; $('official').checked=!!s.config.official_youtube; for (const x of document.querySelectorAll('[name=app]'))x.checked=(s.config.apps || []).includes(x.value); initialized=true; }
    $('status').textContent=s.message;
    for (const id of ['check','measure','retry','install','profile','smb','restore','browse']) $(id).disabled=s.busy;
    const area = $('devices'); area.replaceChildren();
    let done=0, errors=0, manual=0, skipped=0, processed=0, total=0;
    for (const [ip,d] of Object.entries(s.rows)) {
      const card=node('article',null,'device '+d.status);
      const title=node('div',null,'device-title');title.append(node('strong',ip+(d.model?' · '+d.model:'')),node('span',states[d.status] || 'Проверка','state '+d.status));card.append(title);
      if(d.android)card.append(node('div',`Android ${d.android} · API ${d.api} · ${(d.abis || []).join(', ')} · свободно ${d.free_mb} МБ`,'details'));
      const count=d.total_apps || 0;
      if(count) {
        card.append(node('p',`Обработано ${d.processed || 0} из ${count} приложений. Полоса показывает обработку шагов, а не число успешных установок.`,'progress-label'));
        card.append(meter(d.processed || 0,count)); processed+=d.processed || 0;total+=count;
      } else if(s.busy && !['done','error','warning'].includes(d.status)) card.append(meter(null));
      card.append(node('p',d.message));
      for(const [key,r] of Object.entries(d.apps || {})) {
        card.append(taskCard(key,r,ip));
        if(r.status==='done')done++;else if(r.status==='error')errors++;else if(r.status==='manual')manual++;else if(r.status==='skipped')skipped++;
      }
      if(d.status==='error' && !Object.values(d.apps || {}).some(task=>task.status==='error'))errors++;
      technical(card,ip,d); area.append(card);
    }
    $('summary').replaceChildren();
    for(const [text,cls] of [[`Обработано ${processed}/${total}`,''],[`Готово: ${done}`,'done'],[`Ошибки: ${errors}`,'error'],[`Пропуски: ${skipped}`,'skipped'],[`Действие на ТВ: ${manual}`,'manual']])$('summary').append(node('span',text,'summary-count '+cls));
    for(const entry of s.events || [])if(entry.id>lastEvent){log(entry.address+' · '+entry.message);lastEvent=entry.id;}
    if(active && !s.busy){active=false;if(pendingSMB){pendingSMB=false;await smb(false);}}
  } catch(e) { $('status').textContent=e.message; }
  setTimeout(poll,650);
}
poll();
