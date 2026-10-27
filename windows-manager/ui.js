'use strict';
const tokenKey = 'xiaomi-tv-manager-' + location.host;
let token = location.hash.slice(1);
try { if (token) sessionStorage.setItem(tokenKey, token); else token = sessionStorage.getItem(tokenKey) || ''; } catch (e) {}
history.replaceState(null, '', '/');
const $ = id => document.getElementById(id);
const NAMES = ['Проектор', 'Приставка №1', 'Приставка №2'];
const labels = {freemytv:'Free My TV', smarttube:'SmartTube', vlc:'VLC', byedpi:'ByeByeDPI', vimu:'Vimu', mx:'MX Player', youtube:'YouTube', incy:'INCY', ui_profile:'Профиль Free My TV', incy_setup:'Настройка INCY', vpn_route:'VPN-маршрутизация', media:'Медиатека', home_diagnosis:'Диагностика Free My TV', launch_vlc:'Запуск VLC', launch_smarttube:'Запуск SmartTube', launch_youtube:'Запуск YouTube', launch_vimu:'Запуск Vimu', launch_mx:'Запуск MX Player'};
const states = {queued:'В очереди', running:'Выполняется', done:'Готово', error:'Ошибка', skipped:'Пропущено', manual:'Нужно действие на ТВ', warning:'Есть ручные шаги', ready:'Можно установить', update:'Будет обновлено', already:'Уже установлено', blocked:'Нельзя установить'};
const stages = {source:'Источник', download:'Скачивание', tools:'Инструменты', inspect:'Проверка APK', backup:'Резервная копия', install:'Установка', verify:'Проверка на ТВ'};
const kinds = {apk:'Полный APK', apks:'Комплект .apks (base + части)', xapk:'Комплект XAPK'};
let initialized = false, active = false, pendingSMB = false, lastEvent = 0, busy = false;
const openedDetails = new Set(), logLines = [], icons = {}, uploading = {};
let profileBase = null, profileStamp = '', uploadStamp = '', jobStamp = '', snapshot = null;

function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined && text !== null && text !== '') n.textContent = text; if (cls) n.className = cls; return n; }
function mb(n) { return n == null ? 'не определено' : (n / 1048576).toFixed(n >= 1048576 * 100 ? 0 : 1) + ' МБ'; }
function log(message) { logLines.push(new Date().toLocaleTimeString('ru') + '  ' + message); if (logLines.length > 600) logLines.shift(); $('log').textContent = logLines.join('\n'); $('log').scrollTop = $('log').scrollHeight; }
let sayUntil = 0, lastServerMessage = '';
function say(message, isError) { sayUntil = Date.now() + 8000; $('status').textContent = message; log(message); $('status').classList.toggle('error-text', !!isError); }
async function api(path, data) {
  const r = await fetch(path, {method: data ? 'POST' : 'GET', headers: {'X-Manager-Token': token, 'Content-Type': 'application/json'}, body: data ? JSON.stringify(data) : undefined});
  let b = {}; try { b = await r.json(); } catch (e) {}
  if (!r.ok) { const err = Error(b.error || 'Нет доступа. Закройте эту вкладку и снова запустите START.bat.'); err.details = b.details; err.code = b.code; throw err; }
  return b;
}
function ipOf(i) { return $('ip' + i).value.trim(); }
function addressOf(i) { const v = ipOf(i); return v ? (v.includes(':') ? v : v + ':5555') : ''; }
function slotOf(address) { for (const i of [0, 1, 2]) if (addressOf(i) === address) return i; return -1; }
function nameOf(address) { const i = slotOf(address); return i >= 0 ? NAMES[i] + ' · ' + address : address; }
function chosen() { return [0, 1, 2].map(i => $('use' + i).checked ? ipOf(i) : ''); }
function payload() { return {ips: chosen(), all_ips: [0, 1, 2].map(ipOf), use: [0, 1, 2].map(i => $('use' + i).checked), folder: $('folder').value.trim(), apps: Array.from(document.querySelectorAll('[name=app]:checked')).map(x => x.value), official_youtube: $('official').checked, source_ip: $('source-ip').value.trim(), sync_ui: $('sync-ui').checked, media_player: $('media-player').value}; }
function technical(parent, key, data) {
  if (!data || (!data.details && !data.code)) return;
  const details = node('details'); details.open = openedDetails.has(key);
  details.addEventListener('toggle', () => details.open ? openedDetails.add(key) : openedDetails.delete(key));
  details.append(node('summary', 'Технические подробности' + (data.code ? ' · ' + data.code : '')), node('pre', data.details || data.code)); parent.append(details);
}
function meter(value, max = 100) { const p = node('progress'); p.max = max; if (value !== null) p.value = Math.min(value, max); return p; }
function elapsed(started) { return started ? Math.max(0, Math.floor(Date.now() / 1000 - started)) + ' с' : ''; }
// Cards are rebuilt only when data changes; timers tick in place so buttons never vanish under the pointer.
function timer(prefix, started) { const n = node('div', prefix + elapsed(started), 'details'); n.dataset.prefix = prefix; n.dataset.started = started || ''; return n; }
setInterval(() => {
  for (const n of document.querySelectorAll('[data-started]')) n.textContent = n.dataset.prefix + elapsed(+n.dataset.started);
  for (const n of document.querySelectorAll('[data-expires]')) { const left = Math.max(0, Math.floor(+n.dataset.expires - Date.now() / 1000)); n.textContent = n.dataset.label + ' (действует ещё ' + left + ' с)'; if (!left) n.disabled = true; }
}, 1000);

// ---- tabs ------------------------------------------------------------------------------------------------
function showTab(name) {
  for (const b of document.querySelectorAll('[data-tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  for (const p of document.querySelectorAll('.panel')) p.hidden = p.id !== 'tab-' + name;
  try { localStorage.setItem('tvm-tab', name); } catch (e) {}
}
for (const b of document.querySelectorAll('[data-tab]')) b.addEventListener('click', () => showTab(b.dataset.tab));
try { const t = localStorage.getItem('tvm-tab'); if (t && $('tab-' + t)) showTab(t); } catch (e) {}

// ---- device actions --------------------------------------------------------------------------------------
async function action(name) {
  try {
    const p = payload(); p.action = name;
    if (!p.ips.some(Boolean) && name !== 'read_source') throw Error('Отметьте галочкой хотя бы один ТВ.');
    if (name === 'measure' && !confirm('Выбранные в «Каталоге» плееры (или VLC) по очереди откроются на экранах три раза, затем ТВ вернётся на главный экран. Продолжить?')) return;
    if (name === 'install' && !p.apps.length) throw Error('Выберите приложения в «Каталоге».');
    if (name === 'restore_ui' && !confirm('Вернуть настройки интерфейса приставок к состоянию до переноса? Home и приложения не меняются.')) return;
    if (name === 'play_test' && !confirm('На выбранных ТВ откроется ' + $('media-player').selectedOptions[0].textContent + ' с тестовым видео из папки. Если плеер спросит логин и пароль SMB — введите их на ТВ. Продолжить?')) return;
    const result = await api('/action', p); say(result.message);
    pendingSMB = name === 'install' && $('share').checked; active = true;
  } catch (e) { say(e.message, true); }
}
for (const name of ['check', 'measure', 'retry', 'install', 'profile', 'read_source', 'sync_ui', 'restore_ui', 'open_incy', 'diagnose_home', 'network_profile', 'existing_setup', 'open_media', 'vpn_check', 'play_test']) $(name).addEventListener('click', () => action(name));
$('vpn_check2').addEventListener('click', () => action('vpn_check'));
async function smb(restore) {
  if (restore && !confirm('Удалить только созданные менеджером общий ресурс, правило брандмауэра и учётную запись? Фильмы и приложения останутся. Windows запросит UAC.')) return;
  try { say((await api(restore ? '/restore' : '/smb', payload())).message); } catch (e) { say(e.message, true); }
}
$('browse').addEventListener('click', async () => { try { const r = await api('/folder', {}); if (r.message) $('folder').value = r.message; } catch (e) { say(e.message, true); } });
$('smb').addEventListener('click', () => smb(false)); $('restore').addEventListener('click', () => smb(true));
for (const b of document.querySelectorAll('[data-connect]')) b.addEventListener('click', async () => {
  const i = +b.dataset.connect; if (!ipOf(i)) return say('Введите IP устройства.', true);
  try { const r = await api('/remote', {mode: 'connect', address: ipOf(i)}); showAdb(i, r); say(NAMES[i] + ': ' + r.message, r.kind === 'error'); } catch (e) { say(e.message, true); }
});
for (const b of document.querySelectorAll('[data-disconnect]')) b.addEventListener('click', async () => {
  const i = +b.dataset.disconnect; if (!ipOf(i)) return;
  try { const r = await api('/remote', {mode: 'disconnect', address: ipOf(i)}); showAdb(i, r); say(NAMES[i] + ': ' + r.message); } catch (e) { say(e.message, true); }
});
function showAdb(i, st) { const pill = $('adb' + i); pill.textContent = {ready: 'ADB подключён', action: 'Нужно действие на ТВ', error: 'Нет связи', idle: 'Отключено'}[st.kind] || '—'; pill.className = 'pill ' + (st.kind || ''); pill.title = st.message || ''; }
async function pollAdb() {
  try {
    const ips = [0, 1, 2].map(ipOf);
    if (ips.some(Boolean)) { const r = await api('/remote', {mode: 'states', ips}); for (const st of r.states) { const i = slotOf(st.address); if (i >= 0) showAdb(i, st); } }
  } catch (e) {}
  setTimeout(pollAdb, 8000);
}

// ---- status polling --------------------------------------------------------------------------------------
function taskState(task) {
  if (task.status !== 'done') return states[task.status] || task.status;
  if (task.already_installed) return 'Уже установлено';
  if (task.new_install === true) return 'Установлено';
  if (task.new_install === false) return 'Обновлено';
  return 'Готово';
}
function taskCard(key, task, ip) {
  const card = node('div', null, 'task ' + task.status);
  const title = node('div', null, 'task-title'); title.append(node('strong', labels[key] || key), node('span', taskState(task), 'state ' + task.status)); card.append(title);
  const current = task.stage;
  if (current && current !== 'done' && stages[current]) {
    const steps = node('div', null, 'steps');
    for (const [id, label] of Object.entries(stages)) steps.append(node('span', label, id === current ? (task.status === 'error' ? 'failed' : 'current') : (task.completed_stages || []).includes(id) ? 'passed' : ''));
    card.append(steps);
  }
  card.append(node('div', task.message, 'task-message'));
  if (task.status === 'running') {
    const info = task.stage_info || {}, received = Number(info.received || 0), total = Number(info.total || 0);
    if ((current === 'download' || current === 'tools') && !info.waiting) {
      const percent = total ? Math.min(100, Math.floor(100 * received / total)) : null;
      card.append(meter(percent), node('div', mb(received) + (total ? ' / ' + mb(total) + ' · ' + percent + '%' : ' · размер пока неизвестен') + (info.cached ? ' · из проверенного кэша' : ''), 'details'));
    } else card.append(meter(null), timer((stages[current] || 'Обработка') + (info.waiting ? ' · ожидает очереди' : '') + ' · ', task.stage_started));
  }
  technical(card, ip + ':' + key, task); return card;
}
function facts(d) {
  const parts = [];
  if (d.model) parts.push(d.model + (d.source ? ' · источник' : ''));
  if (d.android) parts.push('Android ' + d.android + ' · API ' + d.api + ' · ' + (d.abis || []).join(', '));
  if (d.free_mb != null) parts.push('Хранилище /data: свободно ' + d.free_mb + ' МБ');
  if (d.ram_mb) parts.push('ОЗУ: доступно ' + d.ram_mb + (d.ram_total_mb ? ' из ' + d.ram_total_mb : '') + ' МБ');
  if (d.home) parts.push('Главный экран: ' + d.home.split('/')[0]);
  const net = Object.entries(d.interfaces || {}).filter(([ip]) => !ip.startsWith('127.')).map(([ip, n]) => n + ' ' + ip);
  if (net.length) parts.push('Сеть: ' + net.join(', '));
  return parts;
}
let rowsStamp = '';
function renderRows(s) {
  const stamp = JSON.stringify([s.rows, s.busy, [0, 1, 2].map(addressOf)]);
  if (stamp === rowsStamp) return; rowsStamp = stamp;
  const area = $('devices'); area.replaceChildren();
  let done = 0, already = 0, errors = 0, manual = 0, skipped = 0, processed = 0, total = 0;
  for (const [ip, d] of Object.entries(s.rows)) {
    const slot = slotOf(ip);
    if (slot >= 0 && d.model) { const f = $('facts' + slot); f.replaceChildren(...facts(d).map(t => node('div', t))); }
    const card = node('article', null, 'device ' + d.status);
    const title = node('div', null, 'device-title'); title.append(node('strong', nameOf(ip) + (d.model ? ' · ' + d.model : '')), node('span', states[d.status] || 'Проверка', 'state ' + d.status)); card.append(title);
    for (const line of facts(d).slice(1)) card.append(node('div', line, 'details'));
    if (d.vpn) card.append(node('div', 'VPN: ' + d.vpn, d.vpn_ok === false ? 'warning' : 'details'));
    for (const w of d.warnings || []) card.append(node('div', w, 'warning'));
    const count = d.total_apps || 0;
    if (count) { card.append(node('p', `Обработано шагов: ${d.processed || 0} из ${count}`, 'progress-label'), meter(d.processed || 0, count)); processed += d.processed || 0; total += count; }
    else if (s.busy && !['done', 'error', 'warning', 'skipped'].includes(d.status)) card.append(meter(null));
    card.append(node('p', d.message));
    for (const [key, r] of Object.entries(d.apps || {})) {
      card.append(taskCard(key, r, ip));
      if (r.status === 'done' && r.already_installed) already++; else if (r.status === 'done') done++; else if (r.status === 'error') errors++; else if (r.status === 'manual') manual++; else if (r.status === 'skipped') skipped++;
    }
    if (d.status === 'error' && !Object.values(d.apps || {}).some(t => t.status === 'error')) errors++;
    if (d.benchmark) card.append(benchmarkView(d.benchmark));
    if (d.launcher_diagnosis) card.append(diagnosisView(ip, d.launcher_diagnosis));
    if (d.media_result) { const m = d.media_result; card.append(node('p', m.pc_tcp.message), node('p', m.tv_tcp.message), node('p', 'Адрес: ' + m.media.uri + ' · пользователь: ' + m.media.username), node('p', m.player.message, m.player.playback_verified ? 'ok-text' : '')); }
    technical(card, ip, d); area.append(card);
  }
  $('summary').replaceChildren();
  if (Object.keys(s.rows).length) for (const [text, cls] of [[`Шагов: ${processed}/${total}`, ''], [`Выполнено: ${done}`, 'done'], [`Уже было: ${already}`, 'done'], [`Ошибки: ${errors}`, 'error'], [`Пропущено: ${skipped}`, 'skipped'], [`Действие на ТВ: ${manual}`, 'manual']]) $('summary').append(node('span', text, 'summary-count ' + cls));
}
function benchmarkView(b) {
  const box = node('div', null, 'bench'); box.append(node('strong', 'Замер ' + b.time));
  box.append(node('div', 'Доступная ОЗУ на главном экране: ' + mb(b.ram_available_bytes) + ' · процессов: ' + b.process_count, 'details'));
  const c = b.comparison || {};
  if (c.available) {
    box.append(node('div', 'Сравнение с замером ' + c.previous_time + ': ОЗУ ' + (c.ram_delta_bytes >= 0 ? '+' : '') + mb(c.ram_delta_bytes) + ', процессов ' + (c.process_delta >= 0 ? '+' : '') + c.process_delta, 'details'));
    for (const r of c.apps) box.append(node('div', (labels['launch_' + r.app] || r.app) + ': ' + r.before_ms + ' → ' + r.after_ms + ' мс' + (r.comparable ? '' : ' (разный тип запуска — не сравнимо)'), 'details'));
  }
  box.append(node('div', c.message || '', 'hint')); return box;
}
function diagnosisView(ip, diag) {
  const detail = node('details'); detail.open = openedDetails.has('home:' + ip) || !!diag.wifi_owner_blockers.length;
  detail.addEventListener('toggle', () => detail.open ? openedDetails.add('home:' + ip) : openedDetails.delete('home:' + ip));
  detail.append(node('summary', 'Диагностика Free My TV: ' + (diag.wifi_owner_blockers.length ? 'есть блокировка Wi-Fi' : 'блокировок не найдено')), node('p', diag.explanation));
  detail.append(node('p', 'Главный экран сейчас: ' + (diag.resolved_home || 'не определён') + '. ADB ' + (diag.adb_over_wifi ? 'идёт через Wi-Fi.' : 'идёт не через Wi-Fi.')));
  if (diag.owned_network_ids?.length) detail.append(node('p', 'Записи первоначальной настройки: ' + diag.owned_network_ids.map(n => 'ID ' + n.network_id + (n.current ? ' (текущая)' : '')).join(', ')));
  const ol = node('ol'); for (const step of diag.steps) ol.append(node('li', step)); detail.append(ol);
  return detail;
}
async function poll() {
  try {
    const s = await api('/status'); snapshot = s; busy = s.busy;
    if (!initialized) {
      (s.config.ips || []).forEach((ip, i) => { if (i < 3) $('ip' + i).value = ip || ''; });
      (s.config.use || []).forEach((u, i) => { if (i < 3) $('use' + i).checked = u !== false; });
      $('folder').value = s.config.folder || 'W:\\.intake'; $('source-ip').value = s.config.source_ip || (s.config.ips || [])[0] || '192.168.28.60';
      $('sync-ui').checked = !!s.config.sync_ui; $('official').checked = !!s.config.official_youtube;
      for (const x of document.querySelectorAll('[name=app]')) x.checked = (s.config.apps || []).includes(x.value);
      initialized = true; pollAdb();
    }
    // A fresh server message wins; otherwise the user's own last result stays readable for a few seconds.
    if (s.message !== lastServerMessage || Date.now() > sayUntil) { if (s.message !== lastServerMessage) $('status').classList.remove('error-text'); $('status').textContent = s.message; lastServerMessage = s.message; }
    $('busy').className = 'dot' + (s.busy ? ' on' : ''); $('source-status').textContent = s.source_status || '';
    const stamp = JSON.stringify(s.source_profile);
    if (s.source_profile && stamp !== profileStamp) { profileStamp = stamp; profileBase = JSON.parse(stamp); const p = profileBase.settings; $('profile-language').value = p.language || 'ru'; $('profile-theme').value = p.theme || 'auto'; $('profile-temperature').value = p.temperature || 'C'; $('profile-shelf').value = (p.shelf || []).join('\n'); }
    const packages = s.source_packages || [];
    if ($('source-apps').dataset.packages !== packages.join(',')) { $('source-apps').replaceChildren(); for (const pkg of packages) { const opt = node('option', pkg); opt.value = pkg; $('source-apps').append(opt); } $('source-apps').dataset.packages = packages.join(','); }
    for (const id of ['check', 'measure', 'vpn_check', 'vpn_check2', 'retry', 'install', 'profile', 'smb', 'restore', 'browse', 'read_source', 'sync_ui', 'restore_ui', 'open_incy', 'save-profile', 'media-info', 'diagnose_home', 'network_profile', 'existing_setup', 'open_media', 'play_test', 'apps-scan', 'apps-plan', 'apps-undo', 'cache-plan', 'wifi-plan']) $(id).disabled = s.busy;
    renderRows(s); renderUploads(s.uploads || {items: [], jobs: {}});
    for (const entry of s.events || []) if (entry.id > lastEvent) { log(nameOf(entry.address) + ' · ' + entry.message); lastEvent = entry.id; }
    if (active && !s.busy) { active = false; if (pendingSMB) { pendingSMB = false; await smb(false); } }
  } catch (e) { $('status').textContent = e.message; }
  setTimeout(poll, 900);
}

// ---- your own APKs ---------------------------------------------------------------------------------------
function upload(file) {
  const key = 'local-' + Math.random().toString(36).slice(2);
  const xhr = new XMLHttpRequest(); uploading[key] = {name: file.name, loaded: 0, total: file.size, xhr};
  xhr.open('POST', '/upload'); xhr.setRequestHeader('X-Manager-Token', token); xhr.setRequestHeader('Content-Type', 'application/octet-stream'); xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
  xhr.upload.onprogress = e => { uploading[key].loaded = e.loaded; uploadStamp = ''; };
  xhr.onloadend = () => { let b = {}; try { b = JSON.parse(xhr.responseText); } catch (e) {} delete uploading[key]; uploadStamp = ''; if (xhr.status === 200) log('Файл «' + file.name + '» получен; идёт проверка.'); else say(b.error || (xhr.status ? 'Файл не принят.' : 'Загрузка прервана.'), true); };
  xhr.send(file); uploadStamp = '';
}
function files(list) { for (const f of list) { if (!/\.(apk|apks|xapk|apkm)$/i.test(f.name)) { say('«' + f.name + '»: поддерживаются только .apk, .apks и .xapk.', true); continue; } upload(f); } }
$('pick').addEventListener('click', e => { e.stopPropagation(); $('file').click(); });
$('drop').addEventListener('click', () => $('file').click());
$('drop').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); } });
$('file').addEventListener('change', () => { files($('file').files); $('file').value = ''; });
for (const ev of ['dragenter', 'dragover']) $('drop').addEventListener(ev, e => { e.preventDefault(); $('drop').classList.add('over'); });
for (const ev of ['dragleave', 'drop']) $('drop').addEventListener(ev, e => { e.preventDefault(); $('drop').classList.remove('over'); });
$('drop').addEventListener('drop', e => files(e.dataTransfer.files));
window.addEventListener('dragover', e => e.preventDefault()); window.addEventListener('drop', e => e.preventDefault());
$('apk-download').addEventListener('click', async () => { try { const url = $('apk-url').value.trim(); if (!url) throw Error('Вставьте HTTPS-ссылку.'); say((await api('/apk', {mode: 'url', url})).message); $('apk-url').value = ''; } catch (e) { say(e.message, true); } });
async function apkCall(data, confirmText) { if (confirmText && !confirm(confirmText)) return; try { const r = await api('/apk', data); say(r.message); } catch (e) { say(e.message, true); } }
function kv(table, key, value, mono) { const tr = node('tr'); tr.append(node('th', key)); const td = node('td', value == null || value === '' ? '—' : String(value)); if (mono) td.className = 'mono'; tr.append(td); table.append(tr); }
function uploadCard(item, jobs) {
  const a = item.analysis, card = node('article', null, 'upload ' + item.status);
  const head = node('div', null, 'upload-head');
  const img = node('img', null, 'app-icon'); img.alt = ''; img.hidden = true;
  if (a && a.has_icon) { if (icons[item.id]) { img.src = icons[item.id]; img.hidden = false; } else if (icons[item.id] === undefined) { icons[item.id] = null; api('/apk', {mode: 'icon', id: item.id}).then(r => { icons[item.id] = r.icon || ''; uploadStamp = ''; }).catch(() => {}); } }
  const title = node('div'); title.append(node('strong', (a && a.label) || item.name), node('div', item.name + ' · ' + mb(item.size || item.total || item.received), 'details'));
  const st = {receiving: 'Получение файла', downloading: 'Скачивание', analyzing: 'Проверка', ready: 'Проверен', error: 'Ошибка', cancelled: 'Отменено'}[item.status];
  head.append(img, title, node('span', st, 'state ' + ({ready: 'done', error: 'error', cancelled: 'skipped'}[item.status] || 'running'))); card.append(head);
  if (item.status === 'receiving' || item.status === 'downloading') {
    const total = item.total, pct = total ? Math.floor(100 * item.received / total) : null;
    card.append(meter(pct), node('div', mb(item.received) + (total ? ' / ' + mb(total) + ' · ' + pct + '%' : ' · размер неизвестен') + ' · ' + new Date(item.time * 1000).toLocaleTimeString('ru'), 'details'));
    const b = node('button', 'Отменить загрузку', 'small ghost'); b.addEventListener('click', () => apkCall({mode: 'cancel', id: item.id})); card.append(b);
  }
  if (item.status === 'analyzing') card.append(meter(null), node('div', item.stage || 'Проверка', 'details'));
  if (item.status === 'error' || item.status === 'cancelled') { card.append(node('p', item.error, 'warning')); technical(card, 'up:' + item.id, item); }
  if (a) {
    const prov = a.provenance || {}; card.append(node('div', prov.message, 'prov ' + prov.kind));
    const t = node('table', null, 'facts');
    kv(t, 'Формат', kinds[a.kind] || a.kind); kv(t, 'Пакет', a.package, true); kv(t, 'Версия', (a.version_name || '?') + ' (versionCode ' + a.version + ')');
    kv(t, 'Android', 'минимум API ' + a.min_api + (a.target_api ? ' · target API ' + a.target_api : '')); kv(t, 'Архитектура', a.abis.length ? a.abis.join(', ') : 'без нативного кода (любая)');
    kv(t, 'SHA-256 файла', a.sha256, true); kv(t, 'SHA-256 сертификата', a.certificates.join(', '), true); kv(t, 'Схемы подписи', (a.schemes || []).join(', ') || 'v1');
    if (a.parts.length > 1) kv(t, 'Части', a.parts.map(p => (p.split_name || 'base') + ' · ' + mb(p.size)).join('; '));
    card.append(t);
    for (const w of a.warnings || []) card.append(node('div', w, 'warning'));
    const bar = node('div', null, 'buttons');
    const plan = node('button', 'Проверить на выбранных ТВ', 'primary'); plan.disabled = busy;
    plan.addEventListener('click', () => { const ips = chosen(); if (!ips.some(Boolean)) return say('Отметьте ТВ галочками вверху.', true); apkCall({mode: 'plan', id: item.id, ips}); });
    bar.append(plan); card.append(bar);
  }
  if (jobs.item === item.id) card.append(jobView(item, jobs));
  if (!['receiving', 'downloading', 'analyzing'].includes(item.status)) { const del = node('button', 'Удалить файл из менеджера', 'small ghost'); del.disabled = !!jobs.running && jobs.item === item.id; del.addEventListener('click', () => apkCall({mode: 'delete', id: item.id}, 'Удалить загруженный файл? Установленные приложения на ТВ не затрагиваются.')); card.append(del); }
  return card;
}
function jobView(item, jobs) {
  const box = node('div', null, 'job'); box.append(node('h3', jobs.mode === 'plan' ? 'План по устройствам' : 'Установка по устройствам'));
  for (const [address, r] of Object.entries(jobs.rows || {})) {
    const row = node('div', null, 'task ' + (r.status || 'queued'));
    const title = node('div', null, 'task-title'); title.append(node('strong', nameOf(address) + (r.model ? ' · ' + r.model + ' · Android ' + r.android : '')), node('span', r.status === 'done' ? (r.new_install ? 'Установлено и проверено' : 'Обновлено и проверено') : states[r.status] || r.status, 'state ' + ({ready: 'done', update: 'running', already: 'done', blocked: 'error'}[r.status] || r.status)));
    row.append(title);
    if (r.status === 'running' || r.status === 'queued') row.append(meter(null), timer((r.stage || 'Ожидание') + ' · ', r.stage_started));
    if (r.free_bytes != null) row.append(node('div', 'Свободно ' + mb(r.free_bytes) + (r.old ? ' · установлена версия ' + r.old.version : ' · не установлено'), 'details'));
    for (const x of r.reasons || []) row.append(node('div', x, r.status === 'blocked' ? 'warning' : 'details'));
    for (const x of r.warnings || []) row.append(node('div', x, 'hint'));
    if (r.message) row.append(node('div', r.message, 'task-message'));
    technical(row, 'job:' + address, r); box.append(row);
  }
  if (jobs.plan && !jobs.running && jobs.expires * 1000 > Date.now()) {
    const ready = Object.entries(jobs.rows).filter(([, r]) => ['ready', 'update'].includes(r.status));
    const left = Math.max(0, Math.floor(jobs.expires - Date.now() / 1000));
    const b = node('button', `Установить по плану на ${ready.length} ТВ (действует ещё ${left} с)`, 'primary');
    b.dataset.expires = jobs.expires; b.dataset.label = `Установить по плану на ${ready.length} ТВ`;
    b.addEventListener('click', () => apkCall({mode: 'install', plan: jobs.plan}, 'Установить ' + (item.analysis.label || item.analysis.package) + ' ' + item.analysis.version_name + ' на:\n' + ready.map(([a, r]) => '• ' + nameOf(a) + ' — ' + (r.status === 'update' ? 'обновление' : 'новая установка')).join('\n') + '\n\nДанные приложений не удаляются, понижение версии не выполняется. Продолжить?'));
    box.append(b);
  }
  return box;
}
function renderUploads(u) {
  const local = Object.values(uploading).map(x => [x.name, x.loaded]);
  const stamp = JSON.stringify([u, local, busy, Object.values(icons).filter(Boolean).length, [0, 1, 2].map(addressOf)]);
  if (stamp === uploadStamp) return; uploadStamp = stamp;
  const box = $('uploads'); box.replaceChildren();
  for (const x of Object.values(uploading)) {
    const card = node('article', null, 'upload receiving'); const pct = x.total ? Math.floor(100 * x.loaded / x.total) : null;
    card.append(node('strong', x.name), meter(pct), node('div', 'Передача в менеджер: ' + mb(x.loaded) + ' / ' + mb(x.total) + ' · ' + pct + '%', 'details'));
    const b = node('button', 'Отменить', 'small ghost'); b.addEventListener('click', () => x.xhr.abort()); card.append(b); box.append(card);
  }
  if (!u.items.length && !local.length) box.append(node('p', 'Загруженных файлов пока нет.', 'hint'));
  for (const item of u.items) box.append(uploadCard(item, u.jobs || {}));
}

// ---- applications ----------------------------------------------------------------------------------------
let appRows = [], appAddress = '', appSelected = new Set();
const order = {candidate: 0, review: 1, homemode: 2, keep: 3, system: 4, critical: 5};
function renderApps() {
  const box = $('apps-list'); box.replaceChildren(); const search = $('apps-search').value.toLowerCase(), filter = $('apps-filter').value, sort = $('apps-sort').value;
  const rows = appRows.filter(row => (row.label + ' ' + row.package).toLowerCase().includes(search) && !(
    filter === 'third' && row.system || filter === 'system' && !row.protected || filter === 'disabled' && !row.disabled || filter === 'removed' && row.installed ||
    filter === 'advice' && !['candidate', 'review'].includes(row.kind) || filter === 'homemode' && row.kind !== 'homemode'));
  rows.sort((a, b) => sort === 'size' ? (b.app_bytes || 0) + (b.data_bytes || 0) - (a.app_bytes || 0) - (a.data_bytes || 0) : sort === 'name' ? a.label.localeCompare(b.label, 'ru') : order[a.kind] - order[b.kind] || (b.app_bytes || 0) - (a.app_bytes || 0));
  $('apps-status').dataset.count = rows.length;
  for (const row of rows.slice(0, 400)) {
    const card = node('div', null, 'app-row k-' + row.kind); const label = node('label'); const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.disabled = row.protected; checkbox.checked = appSelected.has(row.package);
    checkbox.addEventListener('change', () => checkbox.checked ? appSelected.add(row.package) : appSelected.delete(row.package));
    label.append(checkbox, node('strong', row.label), node('span', row.kind_text, 'tag k-' + row.kind)); card.append(label);
    card.append(node('p', row.package, 'mono'));
    const size = row.app_bytes != null ? ' · APK ' + mb(row.app_bytes) + ' · данные ' + mb(row.data_bytes) + ' · кэш ' + mb(row.cache_bytes) : '';
    card.append(node('p', (row.installed ? (row.disabled ? 'Отключено' : 'Включено') : 'Удалённая запись') + ' · ' + (row.system ? 'системное' : 'стороннее') + (row.protected ? ' · защищено' : '') + size));
    card.append(node('p', row.advice + (row.homemode_state ? ' Сейчас: ' + row.homemode_state + '.' : ''), 'advice'));
    if (row.installed) { const b = node('button', 'Карточка на ТВ', 'small ghost'); b.addEventListener('click', async () => { try { say((await api('/apps', {mode: 'settings', address: appAddress, package: row.package})).message); } catch (e) { say(e.message, true); } }); card.append(b); }
    box.append(card);
  }
  if (rows.length > 400) box.append(node('p', 'Показаны первые 400; уточните поиск.', 'hint'));
}
function renderSummary(s) {
  const box = $('apps-summary'); box.replaceChildren(); if (!s) return;
  box.append(node('div', 'Свободно в /data: ' + mb(s.free_bytes) + (s.cache_bytes != null ? ' · кэш приложений: ' + mb(s.cache_bytes) : '') + ' · кандидатов: ' + s.candidates.length + ' · проверить: ' + s.review.length, 'details'));
  for (const n of s.notes || []) box.append(node('div', n, 'hint'));
}
function renderHistory(h) {
  const box = $('apps-history'); box.replaceChildren();
  if (!h || !h.length) { box.append(node('p', 'Операций менеджера на этом устройстве ещё не было.', 'hint')); return; }
  for (const op of h) { const d = node('div', null, 'history'); d.append(node('strong', op.report.split(/[\\/]/).pop() + (op.undo_available ? ' · можно отменить' : ''))); for (const e of op.entries) d.append(node('div', e.package + ' · ' + actionName(e.action) + ' · ' + (e.restored ? 'восстановлено' : statusName(e.status)), 'details')); if (op.storage_delta_bytes != null) d.append(node('div', 'Изменение свободного места: ' + mb(op.storage_delta_bytes), 'details')); box.append(d); }
}
function actionName(a) { return {disable: 'Отключение', enable: 'Включение', uninstall_keep_data: 'Удаление APK с сохранением данных', restore: 'Возврат записи'}[a] || a; }
function statusName(s) { return {done: 'подтверждено на ТВ', error: 'ошибка', pending: 'нужно проверить', planned: 'не выполнялось'}[s] || s; }
$('apps-search').addEventListener('input', renderApps); $('apps-filter').addEventListener('change', renderApps); $('apps-sort').addEventListener('change', renderApps);
$('apps-device').addEventListener('change', () => { appRows = []; appSelected.clear(); appAddress = ''; renderApps(); renderSummary(null); $('apps-status').textContent = 'Прочитайте список выбранного устройства.'; });
async function appsRequest(data) {
  $('apps-status').textContent = 'Выполняется, дождитесь ответа…';
  try { const r = await api('/apps', data); $('apps-status').textContent = r.message || 'Готово'; if (r.rows) { appRows = r.rows; appAddress = r.address; appSelected.clear(); renderApps(); renderSummary(r.summary); renderHistory(r.history); } return r; }
  catch (e) { $('apps-status').textContent = e.message; throw e; }
}
$('apps-scan').addEventListener('click', async () => { try { const ip = ipOf($('apps-device').value); if (!ip) throw Error('Введите IP устройства вверху.'); await appsRequest({mode: 'scan', address: ip}); } catch (e) { log(e.message); } });
$('apps-plan').addEventListener('click', async () => {
  try {
    if (!appAddress) throw Error('Сначала прочитайте список приложений.'); if (!appSelected.size) throw Error('Отметьте приложения.');
    const operation = $('apps-operation').value; const r = await appsRequest({mode: 'plan', address: appAddress, operation, packages: [...appSelected]});
    if (!confirm(r.message + '\n\nДействие: ' + $('apps-operation').selectedOptions[0].textContent + '\nУстройство: ' + nameOf(appAddress) + '\n\n' + r.packages.join('\n') + '\n\nВыполнить только этот список?')) return;
    showAppResult(await appsRequest({mode: 'apply', address: appAddress, plan: r.plan}));
  } catch (e) { log(e.message); }
});
$('apps-undo').addEventListener('click', async () => { try { const address = ipOf($('apps-device').value); if (!confirm('Отменить последнюю операцию менеджера на ' + address + '? Устройство, версии и подписи проверяются заново; полной копии данных нет.')) return; showAppResult(await appsRequest({mode: 'undo', address})); } catch (e) { log(e.message); } });
function showAppResult(r) {
  const box = $('apps-result'); box.replaceChildren(); const report = node('div', null, 'report'); report.append(node('h3', 'Отчёт об операции'));
  for (const e of r.changes?.entries || []) report.append(node('div', e.package + ' — ' + actionName(e.action) + ': ' + (e.restored ? 'восстановлено' : statusName(e.status)) + (e.message ? '. ' + e.message : ''), e.status === 'error' ? 'warning' : 'details'));
  if (r.metrics) { const m = r.metrics; report.append(node('div', 'Хранилище /data: ' + mb(m.before_free_bytes) + ' → ' + mb(m.after_free_bytes) + ' (' + (m.storage_delta_bytes >= 0 ? '+' : '') + mb(m.storage_delta_bytes) + ')'), node('div', 'Доступная ОЗУ: ' + mb(m.before_available_ram) + ' → ' + mb(m.after_available_ram) + ' · процессов: ' + m.before_processes + ' → ' + m.after_processes), node('p', m.note, 'hint')); }
  box.append(report);
}
$('cache-plan').addEventListener('click', async () => {
  try {
    const address = ipOf($('apps-device').value); if (!address) throw Error('Введите IP устройства.');
    const p = await api('/apps', {mode: 'cache_plan', address});
    if (!confirm(p.message + '\n\nСейчас кэш: ' + mb(p.cache_bytes) + ', свободно: ' + mb(p.free_bytes) + '.\nОчистить кэш на ' + nameOf(address.includes(':') ? address : address + ':5555') + '?')) return;
    const r = await api('/apps', {mode: 'cache_apply', address, plan: p.plan}); const c = r.cache;
    $('cache-result').replaceChildren(node('div', 'Свободно было ' + mb(c.before_free_bytes) + ', стало ' + mb(c.after_free_bytes) + ' (освобождено ' + mb(Math.max(0, c.freed_bytes)) + '). Кэш по данным Android: ' + mb(c.cache_before) + ' → ' + mb(c.cache_after) + '. Доступная ОЗУ сейчас: ' + mb(c.measures_after.ram_available_bytes) + '.', 'report'), node('p', c.note, 'hint'));
    say(r.message);
  } catch (e) { say(e.message, true); }
});

// ---- Wi-Fi blocker ---------------------------------------------------------------------------------------
$('wifi-plan').addEventListener('click', async () => {
  const box = $('wifi-result'); box.replaceChildren(node('p', 'Чтение сохранённых сетей…'));
  try {
    const address = ipOf($('wifi-device').value); if (!address) throw Error('Введите IP приставки вверху.');
    const r = await api('/wifi', {mode: 'plan', address}); box.replaceChildren(node('p', r.message, 'hint'), node('p', r.adb_over_wifi ? 'ADB сейчас идёт через Wi-Fi: текущую сеть отсюда забыть нельзя.' : 'ADB идёт не через Wi-Fi.', 'details'));
    for (const n of r.networks) {
      const row = node('div', null, 'task ' + (n.allowed ? 'manual' : n.blocker ? 'error' : 'done'));
      row.append(node('strong', 'ID ' + n.network_id + ' · «' + n.ssid + '»' + (n.current ? ' · текущая' : '')), node('div', 'Владелец: ' + n.owner, 'details'), node('div', n.why, 'details'));
      if (n.allowed) { const b = node('button', 'Забыть эту сеть'); b.addEventListener('click', async () => { if (!confirm('Забыть сеть «' + n.ssid + '» (ID ' + n.network_id + ') на ' + nameOf(address.includes(':') ? address : address + ':5555') + '?\n\nЭто не текущее подключение, ADB не оборвётся. Если сеть понадобится, подключитесь к ней снова через Настройки ТВ.')) return; try { const res = await api('/wifi', {mode: 'forget', address, plan: r.plan, network_id: n.network_id}); say(res.message); box.append(node('p', res.message + ' Нажмите «Диагностика на выбранных ТВ», чтобы проверить, что блокировок не осталось.', 'ok-text')); b.disabled = true; } catch (e) { say(e.message, true); } }); row.append(b); }
      box.append(row);
    }
    if (!r.networks.length) box.append(node('p', 'Сохранённых сетей не найдено или формат прошивки не распознан.', 'hint'));
  } catch (e) { box.replaceChildren(node('p', e.message, 'warning')); }
});

// ---- remote ----------------------------------------------------------------------------------------------
function remoteAddress() { const ip = ipOf($('remote-device').value); if (!ip) throw Error('Введите IP устройства вверху.'); return ip; }
for (const b of document.querySelectorAll('[data-key]')) b.addEventListener('click', async () => { try { await api('/remote', {mode: 'key', address: remoteAddress(), key: b.dataset.key}); } catch (e) { say(e.message, true); } });
$('shot').addEventListener('click', async () => { try { $('shot').disabled = true; const r = await api('/remote', {mode: 'screenshot', address: remoteAddress()}); $('shot-img').src = r.image; $('shot-img').hidden = false; say(r.message); } catch (e) { say(e.message, true); } finally { $('shot').disabled = false; } });
$('remote-apps').addEventListener('click', async () => {
  const box = $('remote-app-list'); box.replaceChildren(node('p', 'Чтение…'));
  try {
    const address = remoteAddress(); const r = await api('/remote', {mode: 'apps', address}); box.replaceChildren();
    for (const app of r.apps) { const row = node('div', null, 'launch-row'); const go = node('button', 'Открыть', 'small'); go.addEventListener('click', async () => { try { say((await api('/remote', {mode: 'launch', address, package: app.package})).message); } catch (e) { say(e.message, true); } }); const card = node('button', 'Карточка', 'small ghost'); card.addEventListener('click', async () => { try { say((await api('/apps', {mode: 'settings', address, package: app.package})).message); } catch (e) { say(e.message, true); } }); row.append(node('span', app.label + ' · ' + app.package), go, card); box.append(row); }
  } catch (e) { box.replaceChildren(node('p', e.message, 'warning')); }
});
$('remote-device').addEventListener('change', () => { $('shot-img').hidden = true; $('remote-app-list').replaceChildren(); });

// ---- profile & media -------------------------------------------------------------------------------------
$('append-apps').addEventListener('click', () => { const items = $('profile-shelf').value.split(/[\s,]+/).filter(Boolean); for (const opt of $('source-apps').selectedOptions) if (!items.includes(opt.value)) items.push(opt.value); $('profile-shelf').value = items.join('\n'); });
$('save-profile').addEventListener('click', async () => {
  const profile = profileBase ? JSON.parse(JSON.stringify(profileBase)) : {schema: 1, kind: 'free-my-tv-ui', settings: {}};
  Object.assign(profile.settings, {language: $('profile-language').value.trim(), theme: $('profile-theme').value, temperature: $('profile-temperature').value, shelf: [...new Set($('profile-shelf').value.split(/[\s,]+/).filter(Boolean))], topButtons: ['live', 'inputs', 'network', 'settings']});
  try { say((await api('/manual-profile', {profile})).message); } catch (e) { say(e.message, true); }
});
$('media-info').addEventListener('click', async () => {
  const box = $('media-result'); box.replaceChildren(node('p', 'Проверка типа диска и параметров SMB…'));
  try {
    const {media: m} = await api('/media-info', payload()); box.replaceChildren(node('p', m.note));
    if (m.ready) {
      if (m.server_options?.length > 1) box.append(node('p', 'IP компьютера: ' + m.server_options.join(', ') + '. Первым показан адрес по маршруту к первому ТВ.'));
      for (const [label, value] of [['Сервер', m.server], ['Порт', m.port], ['Общий ресурс', m.share], ['Подпапка', m.subfolder || '(пусто)'], ['Пользователь', m.username], ['Адрес без пароля', m.uri]]) { const line = node('label', label); const field = node('input'); field.readOnly = true; field.value = value; line.append(field); box.append(line); }
      box.append(node('p', m.password_hint)); for (const key of ['vlc', 'vimu', 'mx']) box.append(node('p', m[key]));
    }
  } catch (e) { box.replaceChildren(node('p', e.message, 'warning')); }
});
poll();
