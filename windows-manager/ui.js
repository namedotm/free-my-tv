'use strict';
const tokenKey = 'xiaomi-tv-manager-' + location.host;
const token = location.hash.slice(1) || sessionStorage.getItem(tokenKey) || '';
if (token) sessionStorage.setItem(tokenKey, token);
history.replaceState(null, '', '/');
const $ = id => document.getElementById(id);
let initialized = false, active = false, pendingSMB = false;
const labels = {freemytv:'Free My TV', smarttube:'SmartTube', vlc:'VLC', byedpi:'ByeByeDPI', vimu:'Vimu', mx:'MX Player', youtube:'YouTube', incy:'INCY', ui_profile:'Профиль Free My TV', incy_setup:'Настройка INCY'};
const states = {queued:'В очереди', running:'Выполняется', done:'Готово', error:'Ошибка', skipped:'Пропущено', manual:'Действие на ТВ', warning:'Есть ручные шаги'};
const stages = {source:'Источник', download:'Скачивание', tools:'Инструменты', inspect:'Проверка APK', backup:'Резервная копия', install:'Установка', verify:'Проверка на ТВ'};
let lastEvent = 0;
const openedDetails = new Set();
const logLines = [];
let profileBase=null, profileStamp="";
function log(message) { logLines.push(new Date().toLocaleTimeString('ru') + '  ' + message); if(logLines.length>500)logLines.shift(); $('log').textContent=logLines.join('\n'); $('log').scrollTop=$('log').scrollHeight; }
function payload() { return { ips: [0,1,2].map(i => $('ip'+i).value.trim()), folder: $('folder').value.trim(), apps: Array.from(document.querySelectorAll('[name=app]:checked')).map(x => x.value), official_youtube: $('official').checked, source_ip:$('source-ip').value.trim(), sync_ui:$('sync-ui').checked, media_player:$('media-player').value }; }
async function api(path, data) {
  const r = await fetch(path, {method: data ? 'POST' : 'GET', headers: {'X-Manager-Token': token, 'Content-Type':'application/json'}, body: data ? JSON.stringify(data) : undefined});
  const b = await r.json(); if (!r.ok) throw Error(b.error || 'Нет доступа. Перезапустите START.bat и используйте открывшееся окно.'); return b;
}
async function action(name) {
  try {
    const p = payload(); p.action = name;
    if (name === 'measure' && !confirm('VLC откроется на экранах выбранных устройств. Продолжить замер?')) return;
    if (name === 'install' && !p.apps.length) throw Error('Выберите приложения.');
    if(name==='restore_ui' && !confirm('Вернуть настройки интерфейса приставок к состоянию до переноса? Системный Home и приложения не меняются.'))return;
    const result = await api('/action', p); log(result.message);
    pendingSMB = name === 'install' && $('share').checked; active = true;
  } catch(e) { log(e.message); }
}
async function smb(restore) {
  if (restore && !confirm('Удалить только созданные менеджером SMB-ресурсы? Фильмы и приложения останутся. Windows запросит UAC.')) return;
  try { log((await api(restore ? '/restore' : '/smb', payload())).message); } catch(e) {log(e.message);}
}
for (const name of ['check','measure','retry','install','profile','read_source','sync_ui','restore_ui','open_incy','diagnose_home','network_profile','existing_setup','open_media']) $(name).addEventListener('click', () => action(name));
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
    if (!initialized) { (s.config.ips || []).forEach((ip,i) => $('ip'+i).value=ip); $('folder').value=s.config.folder || 'W:\\.intake'; $('source-ip').value=s.config.source_ip || (s.config.ips || [])[0] || '192.168.28.60'; $('sync-ui').checked=!!s.config.sync_ui; $('official').checked=!!s.config.official_youtube; for (const x of document.querySelectorAll('[name=app]'))x.checked=(s.config.apps || []).includes(x.value); initialized=true; }
    $('status').textContent=s.message; $('source-status').textContent=s.source_status || '';
    const stamp=JSON.stringify(s.source_profile);
    if(s.source_profile && stamp!==profileStamp){ profileStamp=stamp;profileBase=JSON.parse(stamp);const p=profileBase.settings; $('profile-language').value=p.language || 'ru';$('profile-theme').value=p.theme || 'auto';$('profile-temperature').value=p.temperature || 'C';$('profile-shelf').value=(p.shelf || []).join('\n'); }
    const packages=s.source_packages || [];
    if($('source-apps').dataset.packages!==packages.join(',')){ $('source-apps').replaceChildren(); for(const pkg of packages){const opt=node('option',pkg);opt.value=pkg;$('source-apps').append(opt);} $('source-apps').dataset.packages=packages.join(','); }
    for (const id of ['check','measure','retry','install','profile','smb','restore','browse','read_source','sync_ui','restore_ui','open_incy','save-profile','media-info','diagnose_home','network_profile','existing_setup','open_media','apps-scan','apps-plan','apps-undo']) $(id).disabled=s.busy;
    const area = $('devices'); area.replaceChildren();
    let done=0, errors=0, manual=0, skipped=0, processed=0, total=0;
    for (const [ip,d] of Object.entries(s.rows)) {
      const card=node('article',null,'device '+d.status);
      const title=node('div',null,'device-title');title.append(node('strong',ip+(d.model?' · '+d.model:'')),node('span',states[d.status] || 'Проверка','state '+d.status));card.append(title);
      if(d.android)card.append(node('div',`Android ${d.android} · API ${d.api} · ${(d.abis || []).join(', ')} · свободно в хранилище /data ${d.free_mb} МБ`,'details'));
      const count=d.total_apps || 0;
      if(count) {
        card.append(node('p',`Обработано ${d.processed || 0} из ${count} шагов. Полоса показывает обработку, а не число успешных установок.`,'progress-label'));
        card.append(meter(d.processed || 0,count)); processed+=d.processed || 0;total+=count;
      } else if(s.busy && !['done','error','warning'].includes(d.status)) card.append(meter(null));
      card.append(node('p',d.message));
      for(const [key,r] of Object.entries(d.apps || {})) {
        card.append(taskCard(key,r,ip));
        if(r.status==='done')done++;else if(r.status==='error')errors++;else if(r.status==='manual')manual++;else if(r.status==='skipped')skipped++;
      }
      if(d.status==='error' && !Object.values(d.apps || {}).some(task=>task.status==='error'))errors++;
      if(d.launcher_diagnosis){const diag=d.launcher_diagnosis; const detail=node('details'); detail.open=openedDetails.has('home:'+ip);detail.addEventListener('toggle',()=>detail.open?openedDetails.add('home:'+ip):openedDetails.delete('home:'+ip)); detail.append(node('summary','Диагностика Free My TV: Wi-Fi и профиль'),node('p',diag.explanation),node('p','Владельцы сохранённых сетей: '+(diag.wifi_owner_packages.join(', ') || 'не обнаружены / неизвестно')));if(diag.owned_network_ids?.length)detail.append(node('p','ID записей блокировщика (сети не удалялись): '+diag.owned_network_ids.map(n=>n.network_id+' · '+n.owner).join('; ')));for(const step of diag.steps)detail.append(node('p',step));card.append(detail);}
      if(d.media_result){const m=d.media_result;card.append(node('p',m.pc_tcp.message),node('p',m.tv_tcp.message),node('p','Папка: '+m.media.uri+' · пользователь: '+m.media.username),node('p',m.player.message));}
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

$('append-apps').addEventListener('click',()=>{
 const items=$('profile-shelf').value.split(/[\s,]+/).filter(Boolean);
 for(const opt of $('source-apps').selectedOptions)if(!items.includes(opt.value))items.push(opt.value);
 $('profile-shelf').value=items.join('\n');
});
$('save-profile').addEventListener('click',async()=>{
 const profile=profileBase?JSON.parse(JSON.stringify(profileBase)):{schema:1,kind:'free-my-tv-ui',settings:{}}; Object.assign(profile.settings,{language:$('profile-language').value.trim(),theme:$('profile-theme').value,temperature:$('profile-temperature').value,shelf:[...new Set($('profile-shelf').value.split(/[\s,]+/).filter(Boolean))],topButtons:['live','inputs','network','settings']});
 try{log((await api('/manual-profile',{profile})).message);}catch(e){log(e.message);}
});
$('media-info').addEventListener('click',async()=>{
 const box=$('media-result');box.replaceChildren(node('p','Проверка типа диска и параметров SMB…'));
 try{
  const {media:m}=await api('/media-info',payload());box.replaceChildren(node('p',m.note));
  if(m.ready){
   if(m.server_options?.length>1)box.append(node('p','IP компьютера: '+m.server_options.join(', ')+'. Первым показан адрес по маршруту к первому ТВ; при необходимости выберите другой доступный адрес.'));
   for(const [label,value] of [['Сервер',m.server],['Порт',m.port],['Общий ресурс',m.share],['Подпапка',m.subfolder || '(пусто)'],['Пользователь',m.username],['Адрес без пароля',m.uri]]){
    const line=node('label',label);const field=node('input');field.readOnly=true;field.value=value;line.append(field);box.append(line);
   }
   box.append(node('p',m.password_hint));for(const key of ['vlc','vimu','mx'])box.append(node('p',m[key]));
  }
 }catch(e){box.replaceChildren(node('p',e.message,'warning'));}
});

let appRows=[], appAddress='', appSelected=new Set();
function renderApps(){
 const box=$('apps-list');box.replaceChildren();const search=$('apps-search').value.toLowerCase(), filter=$('apps-filter').value;
 for(const row of appRows){
  if(!(row.label+' '+row.package).toLowerCase().includes(search))continue;
  if(filter==='third' && row.protected || filter==='system' && !row.protected || filter==='disabled' && !row.disabled || filter==='removed' && row.installed || filter==='candidates' && !row.candidate)continue;
  const card=node('div',null,'app-row');const label=node('label');const checkbox=node('input');checkbox.type='checkbox';checkbox.disabled=row.protected;checkbox.checked=appSelected.has(row.package);checkbox.addEventListener('change',()=>checkbox.checked?appSelected.add(row.package):appSelected.delete(row.package));label.append(checkbox,node('strong',row.label));card.append(label,node('p',row.package),node('p',(row.installed?(row.disabled?'Отключено':'Установлено'):'Удалённая запись')+' · '+(row.system?'Системное':'Стороннее')+' · '+row.reason));
  if(row.installed){const button=node('button','Настройки на ТВ');button.addEventListener('click',async()=>{try{log((await api('/apps',{mode:'settings',address:appAddress,package:row.package})).message);}catch(e){log(e.message);}});card.append(button);}
  box.append(card);
 }
}
$('apps-search').addEventListener('input',renderApps);$('apps-filter').addEventListener('change',renderApps);
$('apps-device').addEventListener('change',()=>{appRows=[];appSelected.clear();appAddress='';renderApps();$('apps-status').textContent='Прочитайте список выбранного устройства заново.';});
async function appsRequest(data){$('apps-status').textContent='Выполняется, дождитесь ответа…';try{const result=await api('/apps',data);$('apps-status').textContent=result.message || 'Операция завершена';if(result.rows){appRows=result.rows;appAddress=result.address;appSelected.clear();renderApps();}return result;}catch(e){$('apps-status').textContent=e.message;throw e;}}
$('apps-scan').addEventListener('click',async()=>{try{await appsRequest({mode:'scan',address:$('ip'+$('apps-device').value).value.trim()});}catch(e){log(e.message);}});
$('apps-plan').addEventListener('click',async()=>{
 try{if(!appAddress)throw Error('Сначала прочитайте список приложений.');const operation=$('apps-operation').value;const r=await appsRequest({mode:'plan',address:appAddress,operation,packages:[...appSelected]});
  if(!confirm(r.message+'\n\nДействие: '+$('apps-operation').selectedOptions[0].textContent+'\nУстройство: '+appAddress+'\n\n'+r.packages.join('\n')+'\n\nВыполнить только этот список?'))return;
  const result=await appsRequest({mode:'apply',address:appAddress,plan:r.plan});showAppResult(result);
 }catch(e){log(e.message);}
});
$('apps-undo').addEventListener('click',async()=>{try{const address=$('ip'+$('apps-device').value).value.trim();if(!confirm('Отменить последнюю операцию приложений этого менеджера на '+address+'? Версии, подписи и устройство проверяются; полная копия данных не создавалась.'))return;showAppResult(await appsRequest({mode:'undo',address}));}catch(e){log(e.message);}});
function showAppResult(r){const box=$('apps-result');box.replaceChildren();if(r.metrics){const m=r.metrics,mb=v=>v==null?'не определено':(v/1048576).toFixed(1)+' МБ';box.append(node('p','Хранилище /data: '+mb(m.before_free_bytes)+' → '+mb(m.after_free_bytes)+'; разница: '+mb(m.storage_delta_bytes)),node('p','Доступная ОЗУ: '+mb(m.before_available_ram)+' → '+mb(m.after_available_ram)),node('p',m.note));}for(const e of r.changes?.entries || [])box.append(node('p',e.package+' · '+({disable:'Отключение',enable:'Включение',uninstall_keep_data:'Удаление APK с сохранением данных',restore:'Возврат записи'}[e.action] || e.action)+' · '+(e.restored?'Восстановлено':({done:'Подтверждено',error:'Ошибка',pending:'Нужно проверить',planned:'Не выполнялось'}[e.status] || e.status))+' · '+(e.message || '')));}
