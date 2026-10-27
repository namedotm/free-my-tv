"""Local-only Russian GUI; stdlib, no remote content or device actions at startup."""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import webbrowser
import profiles
import media

from engine import (ROOT, DATA, Tools, Device, Problem, targets, read_json, save_json,
                    install_app, vpn_profile, run)


class Manager:
    def __init__(self, data=DATA, tools=None):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.config = read_json(self.data / 'config.json', read_json(ROOT / 'devices.json'))
        self.tools = tools or Tools(self.data)
        self.catalog = read_json(ROOT / 'catalog.json')
        self.lock = threading.Lock()
        self.busy = False
        self.rows = {}
        self.message = 'Готово к проверке. Никаких действий на телевизорах при запуске.'
        self.last = read_json(self.data / 'last-run.json', {})
        self.events = []
        self.event_id = 0
        self.source_profile = read_json(self.data / 'source-profile.json', None)
        self.source_status = 'Профиль проектора ещё не прочитан.'
        self.source_packages = []
        if self.source_profile is not None:
            try:
                self.source_profile = profiles.validate(self.source_profile)
                self.source_status = 'Загружен сохранённый UI-профиль; свежая копия проектора в этом сеансе ещё не получена.'
            except Problem:
                self.source_profile = None
                self.source_status = 'Сохранённый UI-профиль повреждён или содержит запрещённые поля. Заполните его заново.'

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({'busy': self.busy, 'rows': self.rows, 'message': self.message,
                                  'config': self.config, 'catalog_date': self.catalog['reviewed'],
                                  'events': self.events, 'source_profile': self.source_profile,
                                  'source_status': self.source_status, 'source_packages': self.source_packages})

    def update(self, address, **values):
        with self.lock:
            self.rows.setdefault(address, {}).update(values)

    def event(self, address, message, **fields):
        self.update(address, message=message)
        record = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'message': message, 'address': address, **fields}
        with self.lock:
            self.event_id += 1
            self.events.append({'id': self.event_id, **record})
            self.events = self.events[-300:]
        log = self.data / 'logs' / (address.replace(':', '_') + '.jsonl')
        log.parent.mkdir(exist_ok=True)
        with log.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def progress(self, address, key, stage, message, **metadata):
        now = time.time()
        with self.lock:
            row = self.rows.setdefault(address, {})
            task = row.setdefault('apps', {}).setdefault(key, {})
            changed = task.get('stage') != stage
            visited = task.setdefault('completed_stages', [])
            old = task.get('stage')
            if changed and old and old not in visited:
                visited.append(old)
            task.update(status='running', stage=stage, message=message, stage_info=metadata)
            if changed:
                task['stage_started'] = now
            row['current_app'] = key
        if changed:
            self.event(address, self.catalog['apps'].get(key, {'label': key})['label'] + ': ' + message)

    def result(self, address, key, value):
        with self.lock:
            current = self.rows.get(address, {}).get('apps', {}).get(key, {})
            result = {**copy.deepcopy(current), **value}
            if result['status'] != 'error':
                result['stage'] = 'done'
            self.rows.setdefault(address, {}).setdefault('apps', {})[key] = result
        return result

    def start(self, payload):
        action = payload.get('action')
        addresses = targets([payload.get('source_ip') or '192.168.28.60']) if action == 'read_source' else targets(payload.get('ips', []))
        selected = payload.get('apps', [])
        if not isinstance(selected, list) or any(k not in self.catalog['apps'] and k not in ('mx', 'youtube') for k in selected):
            raise Problem('Неизвестное приложение.')
        action = payload.get('action')
        if action not in ('check', 'install', 'retry', 'measure', 'profile', 'read_source', 'sync_ui', 'restore_ui', 'open_incy'):
            raise Problem('Неизвестное действие.')
        source_address = targets([payload.get('source_ip') or '192.168.28.60'])[0]
        if action == 'read_source':
            addresses = [source_address]
        if action in ('sync_ui', 'restore_ui', 'open_incy'):
            addresses = [a for a in addresses if a.split(':')[0] != source_address.split(':')[0]]
            if not addresses:
                raise Problem('Для переноса выберите хотя бы одну приставку. Проектор-источник исключён из изменений.')
        options = {'source_ip': source_address, 'sync_ui': bool(payload.get('sync_ui')),
                   'protect_source': True}
        if action == 'install' and not selected:
            raise Problem('Выберите хотя бы одно приложение для установки.')
        if action == 'retry':
            failed = {a for a, r in self.last.items() if r.get('status') == 'error' or any(x.get('status') == 'error' for x in r.get('apps', {}).values())}
            addresses = [a for a in addresses if a in failed]
            if not addresses:
                raise Problem('Нет неудачных шагов. Для новых устройств используйте общую кнопку установки.')
        with self.lock:
            if self.busy:
                raise Problem('Дождитесь окончания текущей операции.')
            self.config = {'ips': payload.get('ips'), 'apps': selected,
                           'folder': payload.get('folder', ''), 'official_youtube': bool(payload.get('official_youtube')),
                           **options}
            save_json(self.data / 'config.json', self.config)
            self.busy = True
            self.rows = {a: {'status': 'queued', 'message': 'Ожидание подключения', 'progress': 0,
                            'processed': 0, 'total_apps': len(selected) if action in ('install', 'retry') else 0,
                            'apps': {key: {'status': 'queued', 'message': 'В очереди'} for key in selected}
                            if action in ('install', 'retry') else {}} for a in addresses}
            self.message = 'Операция выполняется. Подтвердите USB-отладку на экране ТВ, если появится запрос.'
        threading.Thread(target=self.work, args=(addresses, selected, action, bool(payload.get('official_youtube')), options), daemon=True).start()

    def work(self, addresses, selected, action, official, options=None):
        options = options or {}
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda a: self.device_work(a, selected, action, official, options), addresses))
            with self.lock:
                if action in ('install', 'retry'):
                    self.last.update(copy.deepcopy(self.rows))
                    save_json(self.data / 'last-run.json', self.last)
                errors = sum(r.get('status') == 'error' for r in self.rows.values())
                warnings = sum(r.get('status') == 'warning' for r in self.rows.values())
                self.message = f'Обработка завершена. Устройств с ошибками: {errors}; с пропусками или ручными шагами: {warnings}. Подробности — в карточках ниже.'
        except Exception:
            with self.lock:
                self.message = 'Ошибка сохранения результатов. Проверьте свободное место и права на папку data; устройства не удалялись и Home не менялся.'
        finally:
            with self.lock:
                self.busy = False

    def device_work(self, address, selected, action, official, options=None):
        options = options or {}
        folder = self.data / 'reports' / address.replace(':', '_') / str(time.time_ns())
        folder.mkdir(parents=True)
        apps = {}
        try:
            d = Device(address, self.tools)
            self.update(address, status='running')
            self.event(address, 'Подключение и сбор диагностики без изменения настроек')
            report = d.diagnose()
            save_json(folder / 'before.json', report)
            self.update(address, model=report['model'], android=report['android'], api=report['api'],
                        abis=report['abis'], identity=report['identity'], free_mb=report['free_bytes'] // 1048576, status='running', progress=0,
                        report=str(folder.relative_to(self.data)), processed=0,
                        total_apps=len(selected) if action in ('install', 'retry') else 0)
            self.event(address, 'ADB подключён; диагностический отчёт сохранён')
            if action == 'read_source':
                activities = d.shell('cmd', 'package', 'query-activities', '--brief', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LEANBACK_LAUNCHER', check=False)
                candidates = sorted(set(re.findall(r'^([A-Za-z][A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+$', activities, re.M)))
                with self.lock:
                    self.source_packages = [p for p in candidates if p in report['packages']]
                try:
                    profile = profiles.export(d)
                    save_json(self.data / 'source-profile.json', profile)
                    with self.lock:
                        self.source_profile = profile
                        self.source_status = 'Прочитан UI-профиль проектора; ключи, VPN и системные параметры исключены.'
                    self.event(address, self.source_status)
                except Problem as exc:
                    with self.lock:
                        self.source_status = str(exc)
                        self.source_profile = None
                        old = self.data / 'source-profile.json'
                        if old.exists():
                            old.replace(self.data / ('source-profile.previous-' + str(time.time_ns()) + '.json'))
                    raise
            elif action == 'sync_ui':
                _, missing = self.transfer_ui(d, folder)
                apps['ui_profile'] = {'status': 'manual' if missing else 'done', 'missing_packages': missing,
                                      'message': 'UI-профиль перенесён и проверен. Home не менялся.' + (' Не установлены приложения из избранного: ' + ', '.join(missing) if missing else '')}
                self.update(address, total_apps=1, processed=1)
            elif action == 'restore_ui':
                actual = profiles.restore(d)
                save_json(folder / 'ui-restored.json', actual)
                apps['ui_profile'] = {'status': 'done', 'message': 'Исходный UI-профиль восстановлен. Системные параметры не менялись.'}
                self.update(address, total_apps=1, processed=1)
                self.event(address, 'Исходные настройки интерфейса восстановлены. Home, разрешения и VPN не менялись.')
            elif action == 'open_incy':
                if 'llc.itdev.incy' not in d.packages():
                    raise Problem('Сначала установите INCY на это устройство.')
                output = d.shell('am', 'start', '-n', 'llc.itdev.incy/.MainActivity')
                if re.search(r'Error:|Exception', output):
                    raise Problem('Android не подтвердил открытие INCY.', details=output)
                apps['incy_setup'] = {'status': 'manual', 'message': 'INCY открыт. Перенос подписки и проверку списка приложений нужно завершить в клиенте; VPN менеджером не включался.'}
                self.update(address, total_apps=1, processed=1)
                self.event(address, 'INCY открыт. Перенесите подписку через штатный Send to TV/QR и настройте белый список перед включением VPN.')
            elif action == 'measure':
                if 'org.videolan.vlc' not in report['packages']:
                    raise Problem('VLC не установлен; замер запуска пропущен.')
                resolved = d.shell('cmd', 'package', 'resolve-activity', '--brief', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LEANBACK_LAUNCHER', '-p', 'org.videolan.vlc')
                components = re.findall(r'^org\.videolan\.vlc/[A-Za-z0-9_.$]+$', resolved, re.M)
                if not components:
                    raise Problem('Не удалось определить TV Activity VLC; запуск не выполнялся.')
                timing = d.shell('am', 'start', '-W', '-n', components[-1], timeout=90)
                if not re.search(r'^Status: ok', timing, re.M):
                    raise Problem('Android не подтвердил запуск VLC; время запуска не считается измеренным.')
                save_json(folder / 'launch-vlc.json', {'output': timing, 'cold_start': False, 'note': 'Без force-stop и очистки кэша. Состояние процесса неизвестно; это не доказательство ускорения.'})
                self.event(address, 'Замер запуска VLC сохранён в отчёт. Приложение открыто на ТВ; настройки Home не менялись.')
            elif action == 'profile':
                self.make_profile(d, report, folder, official)
            elif action in ('install', 'retry'):
                previous_row = self.last.get(address, {})
                previous = previous_row.get('apps', {}) if previous_row.get('identity') == report['identity'] else {}
                for index, key in enumerate(selected):
                    # Retry only failed steps from the previous install run.
                    if action == 'retry' and previous.get(key, {}).get('status') not in (None, 'error'):
                        apps[key] = self.result(address, key, previous[key])
                        self.update(address, processed=index + 1, progress=round(100 * (index + 1) / len(selected)))
                        continue
                    try:
                        self.progress(address, key, 'source', 'Начало проверки приложения')
                        if key == 'freemytv' and options.get('protect_source') and address.split(':')[0] == options.get('source_ip', '').split(':')[0]:
                            apps[key] = {'status': 'skipped', 'message': 'Проектор-источник защищён: работающий лаунчер не обновлялся. Другие выбранные приложения обрабатываются независимо.'}
                        elif key == 'vimu' and 'net.gtvbox.videoplayer' in d.packages():
                            apps[key] = {'status': 'done', 'already_installed': True, 'message': 'Vimu Player уже обнаружен на ТВ. Installer не требуется; настройки и лицензия не менялись.'}
                        elif key in ('mx', 'youtube'):
                            pkg = 'com.mxtech.videoplayer.ad' if key == 'mx' else 'com.google.android.youtube.tv'
                            if pkg in d.packages():
                                apps[key] = {'status': 'done', 'already_installed': True, 'message': 'Пакет уже обнаружен; версия не заменялась и магазин не открывался.'}
                            else:
                                opened = d.shell('am', 'start', '-a', 'android.intent.action.VIEW', '-d', 'market://details?id=' + pkg)
                                if re.search(r'Error:|Exception|unable to resolve|No Activity', opened, re.I):
                                    raise Problem('На ТВ нет доступного официального магазина; автоматическая установка этого приложения пропущена.')
                                apps[key] = {'status': 'manual', 'message': 'Открыта официальная страница приложения на ТВ. Установка ещё не подтверждена: завершите её в Google Play.'}
                        else:
                            apps[key] = install_app(d, report, key, self.catalog['apps'][key], self.tools, folder,
                                                    lambda msg: self.event(address, msg),
                                                    progress=lambda stage, msg, **meta: self.progress(address, key, stage, msg, **meta))
                        if key == 'vimu' and apps[key].get('package') == 'net.gtvbox.installer' and apps[key]['status'] == 'done':
                            apps[key].update(status='manual', message='Официальный Vimu Installer установлен и проверен. Откройте его на ТВ, установите плеер и при необходимости активируйте лицензию. Сам плеер пока не считается установленным.')
                        if key == 'incy' and apps[key]['status'] == 'done':
                            allowed = [p for p in ('org.smarttube.stable', 'com.google.android.youtube.tv') if p in d.packages() and (official or p != 'com.google.android.youtube.tv')]
                            headers = {'per-app-proxy-enable': '1', 'per-app-proxy-mode': 'proxy', 'per-app-proxy-list': ','.join(allowed)} if allowed else {}
                            if headers:
                                (folder / 'INCY-YOUTUBE-HEADERS.txt').write_text('\n'.join(k+': '+v for k,v in headers.items())+'\n', encoding='utf-8')
                            save_json(folder / 'incy-checklist.json', {'kind': 'manual-checklist-not-import-config', 'allowed_apps': allowed, 'subscription_headers': headers,
                                      'vpn_started_by_manager': False, 'routing_verified': False,
                                      'transfer': 'Use INCY Send to TV/QR; no private credentials extracted.'})
                            apps[key].update(status='manual', installation_verified=True,
                                message='INCY установлен, подпись проверена. Перенесите сервер через штатный Send to TV/QR и включите режим только выбранных приложений: SmartTube/YouTube. VPN менеджером не включался; маршрутизация требует проверки на ТВ.')
                        apps[key] = self.result(address, key, apps[key])
                        self.event(address, self.catalog['apps'].get(key, {'label': key})['label'] + ': ' + apps[key]['message'])
                    except Problem as e:
                        apps[key] = self.result(address, key, {'status': 'error', 'message': str(e), 'code': e.code, 'details': e.details})
                        self.event(address, key + ': ' + str(e), code=e.code, details=e.details)
                    except Exception:
                        apps[key] = self.result(address, key, {'status': 'error', 'message': 'Непредвиденная ошибка шага. Сохранённые приложения не удалялись.'})
                        self.event(address, key + ': непредвиденная ошибка шага')
                    self.update(address, processed=index + 1, progress=round(100 * (index + 1) / max(1, len(selected))))
                if 'byedpi' in selected and apps.get('byedpi', {}).get('status') == 'done':
                    try:
                        self.make_profile(d, report, folder, official)
                    except Problem as e:
                        self.event(address, 'Профиль VPN не создан: ' + str(e))
                save_json(folder / 'install.json', apps)
                if options.get('sync_ui') and address.split(':')[0] != options.get('source_ip', '').split(':')[0]:
                    try:
                        _, missing = self.transfer_ui(d, folder)
                    except Problem as exc:
                        apps['ui_profile'] = self.result(address, 'ui_profile', {'status': 'error', 'message': str(exc), 'code': exc.code, 'details': exc.details})
                        self.event(address, str(exc))
                    else:
                        apps['ui_profile'] = self.result(address, 'ui_profile', {'status': 'manual' if missing else 'done', 'missing_packages': missing,
                            'message': 'UI-профиль перенесён, избранное объединено и проверено. Home не менялся.' + (' Не установлены пакеты: ' + ', '.join(missing) if missing else '')})
                    self.update(address, total_apps=len(selected) + 1, processed=len(selected) + 1)
                    save_json(folder / 'install.json', apps)
            after = d.measures()
            save_json(folder / 'after.json', after)
            # Observed deltas are not a benchmark: devices/apps may be doing different work.
            counts = {s: sum(x.get('status') == s for x in apps.values()) for s in ('done', 'error', 'skipped', 'manual')}
            state = 'error' if counts['error'] else 'warning' if counts['skipped'] or counts['manual'] else 'done'
            self.update(address, status=state, progress=100, apps=copy.deepcopy(apps))
            summary = (f"Проверено/установлено: {counts['done']}; ошибки: {counts['error']}; "
                       f"пропущено: {counts['skipped']}; требуется действие на ТВ: {counts['manual']}.") if apps else 'Проверка завершена; отчёт сохранён.'
            self.event(address, ('Завершено с ошибками. ' if state == 'error' else 'Завершено. ') + summary)
        except Problem as e:
            self.update(address, status='error', code=e.code, details=e.details)
            self.event(address, str(e), code=e.code, details=e.details)
        except Exception:
            self.update(address, status='error')
            self.event(address, 'Непредвиденная ошибка. Проверьте доступ к каталогу данных и повторите проверку.')

    def transfer_ui(self, device, folder):
        with self.lock:
            source = copy.deepcopy(self.source_profile)
        if source is None:
            raise Problem('Нет профиля источника. Сначала прочитайте профиль проектора или сохраните ручной профиль; заводские значения не выдаются за скопированные.', code='PROFILE_SOURCE_REQUIRED')
        before = profiles.export(device)
        save_json(folder / 'ui-before.json', before)
        merged, missing = profiles.merge(source, before, device.packages())
        save_json(folder / 'ui-planned.json', merged)
        actual = profiles.apply(device, merged)
        save_json(folder / 'ui-after.json', actual)
        if missing:
            self.event(device.address, 'В избранное не добавлены отсутствующие пакеты: ' + ', '.join(missing))
        return actual, missing

    def save_manual_profile(self, value):
        profile = profiles.validate(value)
        with self.lock:
            if self.busy:
                raise Problem('Дождитесь завершения текущего шага.')
            save_json(self.data / 'source-profile.json', profile)
            self.source_profile = profile
            self.source_status = 'Сохранён ручной UI-профиль пользователя; это не автоматическая копия проектора.'
        return self.source_status

    def media_info(self, payload):
        remote_ip = next((v for v in payload.get('ips', []) if v.strip()), None)
        info = media.inspect(payload.get('folder') or r'W:\.intake', remote_ip)
        return media.plan(info, read_json(self.data / 'smb-state.json', None))

    def make_profile(self, d, report, folder, official):
        installed = d.installed('io.github.romanvht.byedpi', folder / 'profile-target', report['api'])
        expected = self.catalog['apps']['byedpi']['certificate']
        if not installed or installed['version'] != 1820 or installed['certificates'] != [expected]:
            raise Problem('Профиль рассчитан только на проверенную ByeByeDPI 1.8.2 с официальной подписью. Сначала установите её; другую версию автоматически не перенастраиваем.')
        profile = vpn_profile(d.packages(), official)
        local = folder / 'XiaomiTV-YouTube-only.json'
        save_json(local, profile)
        # A unique public Download file; no overwrite of existing user files or app-private data.
        remote = '/sdcard/Download/XiaomiTV-YouTube-only-' + folder.name + '.json'
        d.adb('push', str(local), remote)
        self.event(d.address, 'Подготовлен ' + remote + '. На ТВ: ByeByeDPI → Настройки → экспорт исходных настроек → импорт этого файла (Настройки и Приложения). Проверьте белый список ДО разрешения VPN; автозапуск выключен.')
        save_json(folder / 'vpn-profile.json', {'remote_file': remote, 'profile': profile,
                  'verified': False, 'reason': 'Импорт и согласие VPN выполняются на экране ТВ, не через ADB.'})

    def pick_folder(self):
        if os.name != 'nt':
            raise Problem('Выбор папки доступен на Windows.')
        return run(['powershell.exe', '-NoProfile', '-STA', '-Command',
                    "Add-Type -AssemblyName System.Windows.Forms; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Выберите папку с фильмами'; if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.SelectedPath)}"], timeout=300)

    def smb(self, payload, restore=False):
        if os.name != 'nt':
            raise Problem('Настройка SMB доступна только на Windows 10/11 x64.')
        with self.lock:
            if self.busy:
                raise Problem('Дождитесь завершения работы с устройствами.')
        path = payload.get('folder', '')
        if not restore:
            info = media.inspect(path)
            if info['kind'] == 'network':
                raise Problem('Это уже сетевой диск. Нажмите «Как подключить в плеерах»: используйте исходный NAS/SMB-сервер вместо повторной публикации W:.', code='SMB_ALREADY_NETWORK')
        if not restore and (not path or not Path(path).is_dir()):
            raise Problem('Сначала выберите существующую локальную папку Windows.')
        ips = [] if restore else [x.split(':')[0] for x in targets(payload.get('ips', []))]
        request = {'folder': path, 'ips': ips, 'data': str(self.data.resolve()), 'restore': restore}
        request_path = self.data / 'smb-request.json'
        save_json(request_path, request)  # No password. Password is entered only in the elevated native dialog.
        script = str(ROOT / 'SMB.ps1').replace("'", "''")
        req = str(request_path.resolve()).replace("'", "''")
        inner = f"& '{script}' -Request '{req}'"
        encoded = base64.b64encode(inner.encode('utf-16-le')).decode()
        outer = "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -STA -ExecutionPolicy RemoteSigned -EncodedCommand " + encoded + "'"
        run(['powershell.exe', '-NoProfile', '-Command', outer], timeout=180)
        return 'Открывается отдельное окно Windows с запросом UAC. Результат — в этом окне и data/smb-result.json; пароль в журнал не записывается.'


def make_handler(manager, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never record token, request data, or local paths in HTTP logs.

        def authorized(self):
            expected_host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != expected_host:
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + expected_host:
                return False
            return secrets.compare_digest(self.headers.get('X-Manager-Token', ''), token)

        def send(self, status, body, content='application/json; charset=utf-8'):
            b = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content)
            self.send_header('Content-Length', str(len(b)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path in ('/', '/ui.js', '/style.css'):
                # HTML is public only to the exact loopback Host. Token arrives in URL fragment, not HTTP.
                if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                    return self.send(403, '{}')
                name = {'/': 'index.html', '/ui.js': 'ui.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html', '/ui.js': 'application/javascript', '/style.css': 'text/css'}[path]
                return self.send(200, (ROOT / name).read_text(encoding='utf-8'), mime + '; charset=utf-8')
            if not self.authorized():
                return self.send(403, '{}')
            if path == '/status':
                return self.send(200, json.dumps(manager.snapshot(), ensure_ascii=False))
            self.send(404, '{}')

        def do_POST(self):
            if not self.authorized() or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(403, '{}')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    return self.send(413, '{}')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise Problem('Ожидается объект запроса.')
                if self.path == '/action':
                    manager.start(payload)
                    message = 'Операция запущена.'
                elif self.path == '/folder':
                    message = manager.pick_folder()
                elif self.path in ('/smb', '/restore'):
                    message = manager.smb(payload, self.path == '/restore')
                elif self.path == '/manual-profile':
                    message = manager.save_manual_profile(payload.get('profile'))
                elif self.path == '/media-info':
                    return self.send(200, json.dumps({'media': manager.media_info(payload)}, ensure_ascii=False))
                else:
                    return self.send(404, '{}')
                self.send(200, json.dumps({'message': message}, ensure_ascii=False))
            except Problem as e:
                self.send(400, json.dumps({'error': str(e)}, ensure_ascii=False))
            except Exception:
                self.send(400, json.dumps({'error': 'Не удалось обработать запрос; проверьте поля и доступ к файлам.'}, ensure_ascii=False))
    return Handler


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    lock_file = (DATA / 'manager.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock_file.seek(0)
            if lock_file.read(1) == b'':
                lock_file.write(b'1'); lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('Менеджер уже запущен из этой папки. Используйте открытое окно браузера.')
        lock_file.close()
        return
    manager = Manager()
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(manager, token))
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_port}/#{token}'
    print('Xiaomi TV Manager запущен. Закройте это окно для завершения. Журналы: windows-manager/data.')
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        lock_file.close()


if __name__ == '__main__':
    main()
