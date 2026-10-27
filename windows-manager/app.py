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
import applications
import repair
import reports
import apk_intake
import remote
import optimize

from engine import (ROOT, DATA, Tools, Device, Problem, targets, read_json, save_json,
                    install_app, vpn_profile, run, device_key)

LAUNCH = {'vlc': 'org.videolan.vlc', 'smarttube': 'org.smarttube.stable', 'youtube': 'com.google.android.youtube.tv',
          'vimu': 'net.gtvbox.videoplayer', 'mx': 'com.mxtech.videoplayer.ad'}


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
        self.app_plans = {}
        self.app_last = read_json(self.data / 'last-app-changes.json', {})
        # The source projector is remembered by serial+model, so a new IP cannot make it a target.
        self.source_device = read_json(self.data / 'source-device.json', None)
        self.claims = {}
        self.intake = apk_intake.Intake(self.data, self.tools, self.catalog)
        self.wifi_plans = {}
        self.cache_plans = {}
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
                                  'source_status': self.source_status, 'source_packages': self.source_packages,
                                  'source_device': {k: v for k, v in (self.source_device or {}).items() if k != 'device_key'},
                                  'uploads': self.intake.snapshot()})

    def is_source(self, address, report, options):
        """IP match, remembered serial match, or an unprovable identity all count as the source."""
        if address.split(':')[0] == options.get('source_ip', '').split(':')[0]:
            return True
        known = (self.source_device or {}).get('device_key')
        return bool(known) and report.get('device_key') in (known, '', None)

    def claim(self, address, report):
        """One physical TV per run, even if it answers on Ethernet and Wi-Fi addresses."""
        key = report.get('device_key')
        if not key:
            return None
        with self.lock:
            owner = self.claims.setdefault(key, address)
        return owner if owner != address else None

    def source_hint(self):
        """Read-only: which connected ADB devices are the remembered projector or a projector model."""
        listing = run([self.tools.adb, 'devices'], check=False)
        hints = []
        for address in re.findall(r'^(\d+\.\d+\.\d+\.\d+:\d+)\s+device$', listing, re.M):
            try:
                d = Device(address, self.tools)
                props = {k: d.prop(k) for k in ('ro.serialno', 'ro.product.model')}
            except Problem:
                continue
            if self.source_device and device_key(props) == self.source_device.get('device_key'):
                return f' Этот же проектор сейчас отвечает по адресу {address}. Укажите его в поле проектора и повторите.'
            hints.append(f"{address} — {props['ro.product.model']}")
        return (' Подключены: ' + '; '.join(hints) + '. Укажите адрес проектора в первом поле.') if hints else ''

    def protected_source(self, address, report):
        """Remembered projector (any IP) or the configured source address."""
        with self.lock:
            options = {'source_ip': self.config.get('source_ip', '192.168.28.60:5555')}
        return self.is_source(address, report, options)

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
        if action not in ('check', 'install', 'retry', 'measure', 'profile', 'read_source', 'sync_ui', 'restore_ui', 'open_incy', 'diagnose_home', 'network_profile', 'existing_setup', 'open_media', 'vpn_check', 'play_test'):
            raise Problem('Неизвестное действие.')
        source_address = targets([payload.get('source_ip') or '192.168.28.60'])[0]
        if action == 'read_source':
            addresses = [source_address]
        if action == 'read_source':
            addresses = [source_address]
        if action in ('sync_ui', 'restore_ui', 'open_incy', 'network_profile'):
            addresses = [a for a in addresses if a.split(':')[0] != source_address.split(':')[0]]
            if not addresses:
                raise Problem('Для переноса выберите хотя бы одну приставку. Проектор-источник исключён из изменений.')
        options = {'source_ip': source_address, 'sync_ui': bool(payload.get('sync_ui')),
                   'protect_source': True}
        if action in ('existing_setup', 'open_media', 'play_test'):
            if payload.get('media_player', 'vlc') not in repair.PLAYERS:
                raise Problem('Выберите плеер.')
            options.update(folder=payload.get('folder'), ips=payload.get('ips'), media_player=payload.get('media_player', 'vlc'))
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
            # Every address is kept; the check boxes only pick this action's targets.
            all_ips = payload.get('all_ips') if isinstance(payload.get('all_ips'), list) else payload.get('ips')
            self.config = {'ips': [v if isinstance(v, str) else '' for v in all_ips][:3],
                           'use': [bool(v) for v in payload.get('use', [True, True, True])][:3], 'apps': selected,
                           'folder': payload.get('folder', ''), 'official_youtube': bool(payload.get('official_youtube')),
                           **options}
            save_json(self.data / 'config.json', self.config)
            self.busy = True
            self.claims = {}
            self.rows = {a: {'status': 'queued', 'message': 'Ожидание подключения', 'progress': 0,
                            'processed': 0, 'total_apps': len(selected) if action in ('install', 'retry') else 0,
                            'apps': {key: {'status': 'queued', 'message': 'В очереди'} for key in selected}
                            if action in ('install', 'retry') else {}} for a in addresses}
            self.message = 'Операция выполняется. Подтвердите USB-отладку на экране ТВ, если появится запрос.'
        threading.Thread(target=self.work, args=(addresses, selected, action, bool(payload.get('official_youtube')), options), daemon=True).start()

    def work(self, addresses, selected, action, official, options=None):
        options = options or {}
        try:
            if action in ('existing_setup', 'open_media', 'play_test'):
                info = self.media_info(options)
                if not info['ready'] and action == 'existing_setup':
                    self.smb(options, allow_busy=True)
                    deadline = time.monotonic() + 300
                    with self.lock:
                        self.message = 'Подтвердите UAC и задайте SMB-пароль в отдельном окне Windows. Установка приложений не выполняется.'
                    while time.monotonic() < deadline:
                        result = read_json(self.data / 'smb-result.json', {})
                        if result.get('request_id') == self.smb_request_id:
                            if not result.get('ok'):
                                raise Problem(result.get('message', 'Создание SMB отменено.'), code='SMB_SETUP')
                            break
                        time.sleep(.5)
                    else:
                        raise Problem('Нет подтверждения создания SMB за 5 минут. Проверьте отдельное окно Windows; шаги на ТВ не выполнялись.', code='SMB_WAIT')
                    info = self.media_info(options)
                if not info['ready']:
                    raise Problem('Сначала создайте SMB-папку Windows.', code='SMB_NOT_READY')
                options['media'] = info
                options['pc_tcp'] = repair.pc_port(info['server'])
                if action == 'play_test':
                    options['test_uri'] = media.test_uri(info, options.get('folder') or r'W:\.intake')
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda a: self.device_work(a, selected, action, official, options), addresses))
            with self.lock:
                if action in ('install', 'retry'):
                    self.last.update(copy.deepcopy(self.rows))
                    save_json(self.data / 'last-run.json', self.last)
                errors = sum(r.get('status') == 'error' for r in self.rows.values())
                warnings = sum(r.get('status') == 'warning' for r in self.rows.values())
                self.message = f'Обработка завершена. Устройств с ошибками: {errors}; с пропусками или ручными шагами: {warnings}. Подробности — в карточках ниже.'
        except Problem as exc:
            for address in addresses:
                self.update(address, status='error', code=exc.code, details=exc.details)
                self.event(address, str(exc))
            with self.lock:
                self.message = str(exc)
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
            try:
                report = d.diagnose()
            except Problem as exc:
                if action == 'read_source':
                    raise Problem(str(exc) + self.source_hint(), code=exc.code, details=exc.details) from None
                raise
            save_json(folder / 'before.json', report)
            source = action == 'read_source' or self.is_source(address, report, options)
            vpn = report.get('vpn') or {}
            ram = report.get('ram_available_kb')
            self.update(address, model=report['model'], android=report['android'], api=report['api'],
                        abis=report['abis'], identity=report['identity'], free_mb=report['free_bytes'] // 1048576, status='running', progress=0,
                        ram_mb=ram // 1024 if ram else None, ram_total_mb=(report.get('ram_total_kb') or 0) // 1024 or None,
                        home=report.get('home', ''), source=source, interfaces=report.get('interfaces', {}),
                        vpn=vpn.get('message', ''), vpn_ok=vpn.get('ok'),
                        warnings=report.get('recommendations', {}).get('warnings', []),
                        report=str(folder.relative_to(self.data)), processed=0,
                        total_apps=len(selected) if action in ('install', 'retry') else 0)
            reports.write(folder, report, source)
            owner = self.claim(address, report)
            if owner:
                self.update(address, status='skipped', progress=100, total_apps=0, apps={},
                            message=f'Это тот же ТВ, что и {owner} (совпал серийный номер): второй адрес пропущен, чтобы шаги не выполнялись дважды.')
                self.event(address, f'Тот же ТВ, что и {owner}; повторная обработка пропущена.')
                return
            self.event(address, 'ADB подключён; диагностический отчёт сохранён' + (' · распознан проектор-источник' if source else ''))
            if source and action in ('sync_ui', 'restore_ui', 'open_incy', 'network_profile'):
                raise Problem('Это проектор-источник (совпал адрес или серийный номер). Его настройки и лаунчер не меняются.', code='SOURCE_PROTECTED')
            if action in ('sync_ui', 'restore_ui') and not (self.source_device or {}).get('device_key'):
                raise Problem('Сначала нажмите «Прочитать профиль проектора»: без запомненного серийного номера нельзя доказать, что этот адрес не проектор.', code='SOURCE_UNKNOWN')
            if action == 'vpn_check':
                apps['vpn_route'] = {'status': {True: 'done', False: 'error'}.get(vpn.get('ok'), 'manual'),
                                     'message': vpn.get('message', 'Состояние VPN не прочитано.')}
                save_json(folder / 'vpn-route.json', dict(vpn))
            elif action == 'diagnose_home':
                result = repair.diagnose_home(d, report)
                save_json(folder / 'launcher-diagnosis.json', result)
                self.update(address, launcher_diagnosis=result)
                apps['home_diagnosis'] = {'status':'manual' if result['wifi_owner_blockers'] else 'done', 'message':result['explanation'] if result['wifi_owner_blockers'] else 'Диагностика сохранена. Владельцы Wi-Fi не изменялись; Home не переключался.'}
            elif action == 'network_profile':
                before, actual = repair.network_profile(d)
                save_json(folder / 'ui-before.json', before)
                save_json(folder / 'ui-after.json', actual)
                apps['ui_profile'] = {'status':'done','message':'Русский язык и кнопка сети применены и проверены. Избранное сохранено; Home не переключался.'}
            elif action == 'play_test':
                info = options['media']
                tcp = repair.tv_port(d, info['server'])
                if tcp['status'] == 'error':
                    result = {'status': 'error', 'message': tcp['message']}
                else:
                    self.event(address, 'Открываю тестовый файл в плеере. Если плеер спросит логин и пароль SMB — введите их на ТВ и отметьте «Запомнить».')
                    result = repair.play_test(d, options['media_player'], options['test_uri'])
                verified = result.get('playback_verified', False)
                save_json(folder / 'media-play-test.json', {'media': info, 'pc_tcp': options['pc_tcp'], 'tv_tcp': tcp, 'player': result,
                                                           'source_saved': False, 'authenticated_read_verified': verified})
                self.update(address, media_result={'media': info, 'pc_tcp': options['pc_tcp'], 'tv_tcp': tcp, 'player': result})
                apps['media'] = result
            elif action in ('existing_setup', 'open_media'):
                if action == 'existing_setup' and not source:
                    try:
                        before, actual = repair.network_profile(d)
                        save_json(folder / 'ui-before.json', before)
                        save_json(folder / 'ui-after.json', actual)
                        apps['ui_profile'] = {'status':'done','message':'Кнопка сети и русский язык применены. Установка APK не выполнялась.'}
                    except Problem as exc:
                        apps['ui_profile'] = {'status':'manual','message':str(exc),'code':exc.code}
                info = options['media']
                tcp = repair.tv_port(d, info['server'])
                result = repair.handoff(d, options['media_player'], info['uri']) if tcp['status'] != 'error' else {'status':'error','message':tcp['message']}
                save_json(folder / 'media-handoff.json', {'media':info,'pc_tcp':options['pc_tcp'],'tv_tcp':tcp,'player':result,'source_saved':False,'authenticated_read_verified':False})
                self.update(address, media_result={'media':info,'pc_tcp':options['pc_tcp'],'tv_tcp':tcp,'player':result})
                apps['media'] = result
            elif action == 'read_source':
                if report.get('device_key'):
                    remembered = {'device_key': report['device_key'], 'model': report['model'], 'address': address,
                                  'android': report['android'], 'home': report.get('home', ''), 'time': time.strftime('%Y-%m-%d %H:%M:%S')}
                    save_json(self.data / 'source-device.json', remembered)
                    with self.lock:
                        self.source_device = remembered
                    self.event(address, f"Проектор-источник запомнен по серийному номеру ({report['model']}). Под любым IP его лаунчер и настройки не меняются.")
                else:
                    self.event(address, 'Android не сообщил серийный номер: защита источника работает только по этому IP.')
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
                keys = [k for k in selected if k in LAUNCH] or ['vlc']
                bench = optimize.benchmark(d, [(key, LAUNCH[key]) for key in keys], report['packages'],
                                           lambda msg: self.event(address, msg))
                for key, row in bench['apps'].items():
                    apps['launch_' + key] = row
                previous = optimize.last_benchmark(self.data, report['identity'])
                optimize.save_benchmark(self.data, report['identity'], folder, bench)
                comparison = optimize.compare(previous, bench)
                save_json(folder / 'benchmark.json', {**bench, 'comparison': comparison})
                self.update(address, benchmark={**bench, 'comparison': comparison})
                self.event(address, 'Замер сохранён в отчёт; ТВ возвращён на главный экран. Настройки не менялись.')
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
                        if key == 'freemytv' and source:
                            apps[key] = {'status': 'skipped', 'code': 'SOURCE_PROTECTED', 'message': 'Проектор-источник распознан (по адресу или серийному номеру): работающий лаунчер не обновлялся. Другие выбранные приложения обрабатываются независимо.'}
                        elif key == 'freemytv' and not (self.source_device or {}).get('device_key'):
                            apps[key] = {'status': 'skipped', 'code': 'SOURCE_UNKNOWN', 'message': 'Сначала нажмите «Прочитать профиль проектора»: менеджер запомнит проектор по серийному номеру. До этого Free My TV не обновляется ни на одном устройстве, чтобы случайно не заменить лаунчер проектора под другим IP.'}
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
                if 'byedpi' in selected and apps.get('byedpi', {}).get('status') == 'done' and not source:
                    try:
                        self.make_profile(d, report, folder, official)
                    except Problem as e:
                        self.event(address, 'Профиль VPN не создан: ' + str(e))
                save_json(folder / 'install.json', apps)
                if options.get('sync_ui') and not source:
                    try:
                        if not (self.source_device or {}).get('device_key'):
                            raise Problem('Перенос не выполнен: сначала прочитайте профиль проектора, чтобы менеджер запомнил его по серийному номеру.', code='SOURCE_UNKNOWN')
                        _, missing = self.transfer_ui(d, folder)
                    except Problem as exc:
                        apps['ui_profile'] = self.result(address, 'ui_profile', {'status': 'error', 'message': str(exc), 'code': exc.code, 'details': exc.details})
                        self.event(address, str(exc))
                    else:
                        apps['ui_profile'] = self.result(address, 'ui_profile', {'status': 'manual' if missing else 'done', 'missing_packages': missing,
                            'message': 'UI-профиль перенесён, избранное объединено и проверено. Home не менялся.' + (' Не установлены пакеты: ' + ', '.join(missing) if missing else '')})
                    self.update(address, total_apps=len(selected) + 1, processed=len(selected) + 1)
                    save_json(folder / 'install.json', apps)
            if action in ('diagnose_home', 'network_profile', 'existing_setup', 'open_media', 'play_test', 'vpn_check', 'measure'):
                self.update(address, total_apps=len(apps), processed=len(apps))
            after = d.measures()
            save_json(folder / 'after.json', after)
            # Observed deltas are not a benchmark: devices/apps may be doing different work.
            counts = {s: sum(x.get('status') == s for x in apps.values()) for s in ('done', 'error', 'skipped', 'manual')}
            state = 'error' if counts['error'] else 'warning' if counts['skipped'] or counts['manual'] or vpn.get('ok') is False else 'done'
            self.update(address, status=state, progress=100, apps=copy.deepcopy(apps))
            already = sum(x.get('status') == 'done' and bool(x.get('already_installed')) for x in apps.values())
            summary = (f"Установлено/выполнено: {counts['done'] - already}; уже было установлено: {already}; ошибки: {counts['error']}; "
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

    def manage_apps(self, payload):
        mode = payload.get('mode')
        address = targets([payload.get('address', '')])[0]
        if mode not in ('scan', 'plan', 'apply', 'undo', 'settings', 'cache_plan', 'cache_apply'):
            raise Problem('Неизвестная операция приложений.')
        with self.lock:
            if self.busy:
                raise Problem('Дождитесь завершения текущего шага.')
            self.busy = True
            self.message = 'Работа со списком приложений: ' + mode
        try:
            d = Device(address, self.tools)
            report = d.diagnose()
            rows = applications.inventory(d)
            if mode == 'scan':
                annotated, summary = optimize.analyse(rows, optimize.storage(d), report)
                return {'rows': annotated, 'summary': summary, 'address': address, 'model': report['model'],
                        'source': self.protected_source(address, report),
                        'history': applications.history(self.data, self.app_last.get(address, [])),
                        'message': 'Список прочитан. Системные и критические компоненты защищены. Удалённые записи — только те, которые Android ещё хранит; это не полная история удалений.'}
            if mode == 'cache_plan':
                if self.protected_source(address, report):
                    raise Problem('Проектор-источник: менеджер его не оптимизирует.', code='SOURCE_PROTECTED')
                totals = optimize.storage(d).get('totals', {})
                key = secrets.token_urlsafe(24)
                self.cache_plans = {key: {'expires': time.time() + 180, 'identity': report['identity'], 'address': address}}
                return {'plan': key, 'cache_bytes': totals.get('app_cache_size'), 'free_bytes': report['free_bytes'],
                        'message': 'Будет выполнена штатная команда Android «pm trim-caches»: удаляется только кэш, который приложения создают заново. Данные, аккаунты, настройки и APK не затрагиваются. Отменить очистку кэша нельзя (и не нужно).'}
            if mode == 'cache_apply':
                plan = self.cache_plans.pop(payload.get('plan'), None)
                if not plan or plan['expires'] < time.time() or plan['address'] != address or plan['identity'] != report['identity']:
                    raise Problem('План устарел или относится к другому ТВ.', code='CACHE_PLAN')
                folder = self.data / 'reports' / address.replace(':', '_') / str(time.time_ns())
                result = optimize.trim_caches(d)
                result['measures_after'] = {'ram_available_bytes': optimize.memory(d), 'process_count': optimize.processes(d)}
                save_json(folder / 'cache-trim.json', result)
                with self.lock:
                    self.message = 'Кэш очищен; результат измерен.'
                return {'cache': result, 'message': f"Освобождено {max(0, result['freed_bytes']) // 1048576} МБ в /data. {result['note']}"}
            if mode == 'settings':
                pkg = payload.get('package', '')
                if pkg not in {r['package'] for r in rows if r['installed']}:
                    raise Problem('Приложение не установлено.')
                out = d.shell('am','start','-a','android.settings.APPLICATION_DETAILS_SETTINGS','-d','package:'+pkg)
                if re.search(r'Error:|Exception', out):
                    raise Problem('Android не открыл карточку приложения.', details=out)
                return {'message':'На ТВ открыта системная карточка приложения. Менеджер ничего не удалил и не очищал.'}
            if mode == 'plan':
                chosen = applications.validate_selection(rows,payload.get('operation'),payload.get('packages'))
                states = {r['package']:applications.enabled_state(d,r['package']) for r in chosen} if payload['operation'] != 'restore' else {}
                key = secrets.token_urlsafe(24)
                self.app_plans = {key:{'expires':time.time()+180,'identity':report['identity'],'address':address,'action':payload['operation'],'packages':[r['package'] for r in chosen],'rows':chosen,'states':states}}
                return {'plan':key,'packages':[r['package'] for r in chosen],'operation':payload['operation'],
                        'message':'План ещё не выполнен. Удаление: только APK стороннего приложения, с сохранением данных (-k) и предварительной проверенной копией APK. Системные APK не затрагиваются. Отключение не освобождает место APK. Откат не является полной резервной копией данных.'}
            folder = self.data/'reports'/address.replace(':','_')/str(time.time_ns())
            before = {'free_bytes':d.free_space(),'measures':d.measures()}
            if mode == 'apply':
                plan = self.app_plans.pop(payload.get('plan'), None)
                if not plan or plan['expires'] < time.time() or plan['address'] != address or plan['identity'] != report['identity']:
                    raise Problem('План устарел или относится к другому ТВ. Обновите список.',code='APP_PLAN')
                chosen = applications.validate_selection(rows,plan['action'],plan['packages'])
                states = {r['package']:applications.enabled_state(d,r['package']) for r in chosen} if plan['action'] != 'restore' else {}
                if chosen != plan['rows'] or states != plan['states']:
                    raise Problem('Состояние приложений изменилось после просмотра плана.',code='APP_PLAN')
                save_json(folder/'before.json',report)
                # Register the journal location before mutations so a partial failure is reviewable.
                history = self.app_last.setdefault(address, [])
                history.append(str(folder.relative_to(self.data)))
                save_json(self.data/'last-app-changes.json',self.app_last)
                changes = applications.execute(d,report,plan['action'],plan['packages'],folder)
            else:
                history = self.app_last.get(address, [])
                relative = history[-1] if history else None
                if not relative:
                    raise Problem('Нет операции этого менеджера для отмены.')
                folder = (self.data/relative).resolve()
                if not folder.is_relative_to(self.data.resolve()):
                    raise Problem('Неверный путь журнала отмены.')
                changes = read_json(folder/'app-changes.json')
                if not changes:
                    raise Problem('Журнал не создан: изменения не начинались.')
                restored = applications.rollback(d,report,changes,folder)
                changes = {**changes,'restored_packages':restored}
                if all(e.get('restored') or e['status']=='planned' for e in changes['entries']):
                    history.pop()
                    save_json(self.data/'last-app-changes.json',self.app_last)
            after = {'free_bytes':d.free_space(),'measures':d.measures()}
            measurement = applications.metrics(before,after)
            save_json(folder/('undo-measurements.json' if mode=='undo' else 'measurements.json'),measurement)
            with self.lock:
                self.message = 'Операция завершена. Результаты подтверждены отдельно для каждого приложения; смотрите отчёт.'
            annotated, summary = optimize.analyse(applications.inventory(d), optimize.storage(d), report)
            return {'rows': annotated, 'summary': summary, 'address': address, 'changes': changes, 'metrics': measurement,
                    'history': applications.history(self.data, self.app_last.get(address, [])), 'message': self.message}
        except Problem as exc:
            with self.lock:
                self.message = str(exc)
            raise
        finally:
            with self.lock:
                self.busy = False

    # ---- your own APKs ---------------------------------------------------------------------------------------
    def apk(self, payload):
        mode = payload.get('mode')
        if mode == 'url':
            item = self.intake.start_download(payload.get('url'))
            return {'id': item, 'message': 'Скачивание началось. Ход загрузки — в карточке файла.'}
        if mode == 'cancel':
            return {'message': self.intake.cancel_item(payload.get('id'))}
        if mode == 'delete':
            return {'message': self.intake.delete(payload.get('id'))}
        if mode == 'icon':
            return {'icon': self.intake.icon_of(payload.get('id'))}
        if mode not in ('plan', 'install'):
            raise Problem('Неизвестная операция с APK.')
        if mode == 'plan':
            item = self.intake.item_analysis(payload.get('id'))
            addresses = targets(payload.get('ips', []))
            args = (item, addresses)
        else:
            plan = self.intake.take_plan(payload.get('plan'))
            args = (plan,)
        with self.lock:
            if self.busy:
                raise Problem('Дождитесь окончания текущей операции.')
            self.busy = True
            self.message = 'Проверка совместимости APK с выбранными ТВ…' if mode == 'plan' else 'Установка APK на выбранные ТВ…'
        threading.Thread(target=self.apk_job, args=(mode, *args), daemon=True).start()
        return {'message': self.message}

    def apk_row(self, key, /, **values):
        with self.intake.lock:
            row = self.intake.jobs.setdefault('rows', {}).setdefault(key, {})
            if values.get('stage') and values['stage'] != row.get('stage'):
                values['stage_started'] = time.time()
            row.update(values)

    def apk_job(self, mode, *args):
        stamp = str(time.time_ns())
        try:
            if mode == 'plan':
                item, addresses = args
                with self.intake.lock:
                    self.intake.jobs = {'mode': 'plan', 'item': item['id'], 'running': True, 'plan': None, 'expires': None,
                                        'rows': {a: {'status': 'queued', 'stage': 'В очереди'} for a in addresses}}
                def one(address):
                    folder = self.data / 'reports' / address.replace(':', '_') / (stamp + '-apk')
                    try:
                        self.apk_row(address, status='running', stage='Подключение и диагностика')
                        d = Device(address, self.tools)
                        report = d.diagnose()
                        save_json(folder / 'before.json', report)
                        row = self.intake.plan_device(d, report, item, folder, self.protected_source(address, report),
                                                      lambda text: self.apk_row(address, stage=text))
                        row['folder'] = str(folder)
                        self.apk_row(address, **row, stage='')
                        return address, row
                    except Problem as exc:
                        self.apk_row(address, status='error', stage='', message=str(exc), code=exc.code, details=exc.details)
                    except Exception:
                        self.apk_row(address, status='error', stage='', message='Непредвиденная ошибка проверки этого ТВ. На ТВ ничего не менялось.')
                    return address, None
                with ThreadPoolExecutor(max_workers=3) as pool:
                    planned = dict(pool.map(one, addresses))
                ready = {a: r for a, r in planned.items() if r and r['status'] in ('ready', 'update')}
                token = self.intake.remember_plan(item, ready) if ready else None
                with self.intake.lock:
                    self.intake.jobs.update(running=False, plan=token, expires=time.time() + apk_intake.PLAN_SECONDS if token else None)
                counts = {s: sum(1 for r in planned.values() if r and r['status'] == s) for s in ('already', 'blocked')}
                errors = sum(1 for r in planned.values() if not r)
                with self.lock:
                    self.message = (f'План готов: можно установить на {len(ready)} ТВ. Проверьте причины и предупреждения, затем нажмите «Установить по плану» в течение 3 минут.'
                                    if ready else f"Устанавливать некуда: уже установлено — {counts['already']}, нельзя установить — {counts['blocked']}, ошибка проверки — {errors}. Причины — в карточках устройств.")
            else:
                (plan,) = args
                item = self.intake.item_analysis(plan['item'])
                if item['analysis']['sha256'] != plan['sha256']:
                    raise Problem('Файл изменился после плана.', code='APK_CHANGED')
                with self.intake.lock:
                    old_rows = self.intake.jobs.get('rows', {}) if self.intake.jobs.get('item') == item['id'] else {}
                    self.intake.jobs = {'mode': 'install', 'item': item['id'], 'running': True, 'plan': None, 'expires': None,
                                        'rows': {a: {**r, 'status': 'skipped', 'message': 'Не входит в план установки: ' + '; '.join(r.get('reasons') or ['исключено'])}
                                                 for a, r in old_rows.items() if a not in plan['rows'] and r.get('status')}}
                    for a, r in plan['rows'].items():
                        self.intake.jobs['rows'][a] = {**r, 'status': 'queued', 'stage': 'В очереди'}
                results = {}
                def one(address):
                    row = plan['rows'][address]
                    folder = Path(row['folder'])
                    try:
                        self.apk_row(address, status='running', stage='Повторная проверка устройства')
                        d = Device(address, self.tools)
                        report = d.diagnose()
                        result = self.intake.install_device(d, report, item, row, folder,
                                                            lambda stage, text: self.apk_row(address, stage=text))
                    except Problem as exc:
                        result = {'status': 'error', 'message': str(exc), 'code': exc.code, 'details': exc.details}
                    except Exception:
                        result = {'status': 'error', 'message': 'Непредвиденная ошибка установки на этом ТВ. Повторите план: менеджер заново проверит версию.'}
                    self.apk_row(address, **result, stage='')
                    results[address] = {**result, 'backup': (row.get('old') or {}).get('backup')}
                    self.event(address, 'APK ' + item['analysis']['package'] + ': ' + result['message'])
                with ThreadPoolExecutor(max_workers=3) as pool:
                    list(pool.map(one, list(plan['rows'])))
                save_json(self.data / 'apk-installs' / (stamp + '.json'),
                          {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'file': item['name'], 'sha256': item['analysis']['sha256'],
                           'package': item['analysis']['package'], 'version': item['analysis']['version'], 'results': results,
                           'restore': 'Новую установку можно удалить в разделе «Приложения» (удаление APK с сохранением данных). Возврат прежней версии после обновления Android разрешает только через удаление приложения с потерей данных — менеджер этого не делает; копия прежних APK лежит в папке backup отчёта.'})
                ok = sum(r['status'] == 'done' for r in results.values())
                with self.intake.lock:
                    self.intake.jobs['running'] = False
                with self.lock:
                    self.message = f'Установка завершена: подтверждено на {ok} из {len(results)} ТВ. Подробности — в карточках.'
        except Problem as exc:
            with self.intake.lock:
                self.intake.jobs['running'] = False
            with self.lock:
                self.message = str(exc)
        except Exception:
            with self.intake.lock:
                self.intake.jobs['running'] = False
            with self.lock:
                self.message = 'Непредвиденная ошибка операции APK. Устройства не менялись после ошибки; проверьте журнал.'
        finally:
            with self.lock:
                self.busy = False

    # ---- remote control ----------------------------------------------------------------------------------
    def remote(self, payload):
        mode = payload.get('mode')
        if mode == 'states':
            values = [v for v in payload.get('ips', []) if isinstance(v, str) and v.strip()]
            return {'states': [remote.state(self.tools.adb, a) for a in targets(values)]}
        address = targets([payload.get('address', '')])[0]
        if mode == 'connect':
            return remote.connect(self.tools.adb, address)
        if mode == 'disconnect':
            with self.lock:
                if self.busy:
                    raise Problem('Дождитесь окончания текущей операции: отключение прервёт её.')
            return remote.disconnect(self.tools.adb, address)
        d = Device(address, self.tools)
        if remote.state(self.tools.adb, address)['adb_state'] != 'device':
            raise Problem('Сначала подключите этот ТВ и разрешите отладку на его экране.', code='ADB_NOT_READY')
        if mode == 'key':
            return remote.key(d, payload.get('key'))
        if mode == 'screenshot':
            return remote.screenshot(d, self.data / 'screenshots' / address.replace(':', '_'))
        if mode == 'apps':
            entries = remote.launchable(d)
            return {'apps': [{'package': p, 'label': (optimize.KB.get(p) or (applications.LABELS.get(p, p),))[0]} for p in sorted(entries)]}
        if mode == 'launch':
            return remote.launch(d, payload.get('package'))
        raise Problem('Неизвестная команда удалённого управления.')

    # ---- Wi-Fi blocker -------------------------------------------------------------------------------------
    def wifi(self, payload):
        mode = payload.get('mode')
        address = targets([payload.get('address', '')])[0]
        with self.lock:
            if self.busy:
                raise Problem('Дождитесь окончания текущей операции.')
            self.busy = True
        try:
            d = Device(address, self.tools)
            d.connect()
            info = repair.device_api(d)
            if mode == 'plan':
                plan = repair.forget_plan(d, info)
                identity = d.identity()
                self.wifi_plans = {secrets.token_urlsafe(16): {'expires': time.time() + 180, 'address': address, 'identity': identity,
                                                               'networks': {n['network_id']: n for n in plan['networks'] if n['allowed']}}}
                token = next(iter(self.wifi_plans))
                # SSIDs are returned for display only and never written to reports.
                return {'plan': token, **plan, 'message': 'Выберите только ту сеть, которую узнаёте. Пароли менеджер не видит и не хранит.'}
            if mode == 'forget':
                plan = self.wifi_plans.pop(payload.get('plan'), None)
                network_id = payload.get('network_id')
                if not plan or plan['expires'] < time.time() or plan['address'] != address or d.identity() != plan['identity']:
                    raise Problem('План устарел или относится к другому ТВ. Обновите список сетей.', code='WIFI_PLAN')
                if not isinstance(network_id, int) or network_id not in plan['networks']:
                    raise Problem('Эту сеть нельзя забыть из менеджера.', code='WIFI_PLAN')
                result = repair.forget_network(d, network_id, plan['networks'][network_id])
                folder = self.data / 'reports' / address.replace(':', '_') / str(time.time_ns())
                save_json(folder / 'wifi-forget.json', {'network_id': network_id, 'owner': plan['networks'][network_id]['owner'],
                                                        'forgotten': True, 'ssid_stored': False})
                self.event(address, result['message'])
                return result
            raise Problem('Неизвестная операция Wi-Fi.')
        finally:
            with self.lock:
                self.busy = False

    def smb(self, payload, restore=False, allow_busy=False):
        if os.name != 'nt':
            raise Problem('Настройка SMB доступна только на Windows 10/11 x64.')
        with self.lock:
            if self.busy and not allow_busy:
                raise Problem('Дождитесь завершения работы с устройствами.')
        path = payload.get('folder', '')
        if not restore:
            info = media.inspect(path)
            if info['kind'] == 'network':
                raise Problem('Это уже сетевой диск. Нажмите «Как подключить в плеерах»: используйте исходный NAS/SMB-сервер вместо повторной публикации W:.', code='SMB_ALREADY_NETWORK')
        if not restore and (not path or not Path(path).is_dir()):
            raise Problem('Сначала выберите существующую локальную папку Windows.')
        ips = [] if restore else [x.split(':')[0] for x in targets(payload.get('ips', []))]
        self.smb_request_id = secrets.token_hex(16)
        request = {'folder': path, 'ips': ips, 'data': str(self.data.resolve()), 'restore': restore, 'request_id':self.smb_request_id}
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
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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
            if self.path == '/upload':
                return self.upload()
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
                elif self.path == '/apps':
                    return self.send(200, json.dumps(manager.manage_apps(payload), ensure_ascii=False))
                elif self.path in ('/apk', '/remote', '/wifi'):
                    handler = {'/apk': manager.apk, '/remote': manager.remote, '/wifi': manager.wifi}[self.path]
                    return self.send(200, json.dumps(handler(payload), ensure_ascii=False))
                else:
                    return self.send(404, '{}')
                self.send(200, json.dumps({'message': message}, ensure_ascii=False))
            except Problem as e:
                self.send(400, json.dumps({'error': str(e), 'code': e.code, 'details': e.details}, ensure_ascii=False))
            except Exception:
                self.send(400, json.dumps({'error': 'Не удалось обработать запрос; проверьте поля и доступ к файлам.'}, ensure_ascii=False))

        def upload(self):
            # Raw file body streamed to disk; the JSON routes keep their 64 KB limit.
            if not self.authorized() or self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                self.close_connection = True
                return self.send(403, '{}')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= apk_intake.MAX_BYTES:
                    self.close_connection = True
                    return self.send(413, json.dumps({'error': 'Файл пустой или больше 1 ГБ.'}, ensure_ascii=False))
                item = manager.intake.receive(self.rfile, size, self.headers.get('X-File-Name', ''))
                body, status = {'id': item, 'message': 'Файл получен; идёт проверка.'}, 200
            except Problem as e:
                body, status = {'error': str(e), 'code': e.code}, 400
            except (ValueError, OSError):
                body, status = {'error': 'Передача файла прервана.'}, 400
            if status != 200:
                self.close_connection = True
            try:
                self.send(status, json.dumps(body, ensure_ascii=False))
            except OSError:
                pass  # The browser already closed the connection (cancelled upload).
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
