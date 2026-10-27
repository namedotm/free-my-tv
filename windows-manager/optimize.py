"""Per-device optimisation analysis, storage breakdown, cache trim and repeatable launch measurements.

The knowledge base below describes packages actually seen on the owner's projector and two Mi Box S. It explains;
it never decides. System packages stay view-only here: Free My TV's HomeMode turns its reviewed list off on the TV.
"""
from pathlib import Path
import json
import re
import shlex
import statistics
import time

from engine import Problem, save_json, read_json

# kind: critical (never), homemode (HomeMode turns it off in Free My TV mode), system (view-only, may be turned
# off in TV Settings by hand where Android allows), candidate (third-party: worth reviewing), keep, review.
KB = {
    'android': ('Система Android', 'critical', 'Ядро системы.'),
    'com.android.systemui': ('Системный интерфейс', 'critical', 'Панели, уведомления, громкость.'),
    'com.android.tv.settings': ('Настройки ТВ', 'critical', 'Единственный штатный путь к сети, приложениям и восстановлению.'),
    'com.google.android.tungsten.setupwraith': ('Первоначальная настройка Google TV', 'critical', 'Владеет сохранёнными Wi-Fi-сетями; отключение удалит их. HomeMode выключает её только по своим правилам.'),
    'com.google.android.gms': ('Сервисы Google Play', 'critical', 'Аккаунт, Cast, DRM-лицензии, обновления.'),
    'com.google.android.gsf': ('Google Services Framework', 'critical', 'Нужен сервисам Google.'),
    'com.android.vending': ('Google Play Маркет', 'critical', 'Установка и обновление приложений (VLC/MX/YouTube обновляются здесь).'),
    'com.google.android.apps.mediashell': ('Chromecast built-in', 'critical', 'Трансляция с телефона (Google Cast).'),
    'com.google.android.tv.remote.service': ('Пульт Google TV', 'critical', 'Управление с телефона.'),
    'com.google.android.inputmethod.latin': ('Клавиатура Gboard', 'critical', 'Ввод текста.'),
    'com.google.android.webview': ('Android WebView', 'critical', 'Встроенный браузерный движок для приложений.'),
    'com.google.android.packageinstaller': ('Установщик пакетов', 'critical', 'Установка APK.'),
    'com.google.android.permissioncontroller': ('Разрешения', 'critical', 'Управление разрешениями.'),
    'com.android.bluetooth': ('Bluetooth', 'critical', 'Пульт и звук по Bluetooth.'),
    'com.mi.bluetooth': ('Bluetooth Xiaomi', 'critical', 'Сопряжение пульта Xiaomi.'),
    'com.dolby.android.audio.service': ('Dolby Audio', 'critical', 'Звук.'),
    'com.google.android.katniss': ('Голосовой поиск Google', 'critical', 'Кнопка микрофона на пульте.'),
    'com.google.android.marvin.talkback': ('TalkBack', 'critical', 'Специальные возможности.'),
    'com.google.android.tts': ('Синтез речи Google', 'critical', 'Специальные возможности; скачанные голоса занимают место — управляются в Настройки → Специальные возможности → Синтез речи.'),
    'com.smartdevice.tv.aircast': ('AirCast (AirPlay-приёмник)', 'critical', 'Настроен на проекторе; не меняется.'),
    'com.xiaomi.mitv.smartshare': ('Miracast/Smart Share', 'critical', 'Беспроводной экран.'),
    'com.xiaomi.wfdsinkhelperservice': ('Miracast помощник', 'critical', 'Беспроводной экран.'),
    'mitv.service': ('Системная служба Xiaomi', 'critical', 'Базовые функции приставки.'),
    'com.xiaomi.android.tvsetup.partnercustomizer': ('Кнопки пульта Xiaomi', 'critical', 'Обработка кнопок Mi/PatchWall.'),
    'com.xiaomi.tvqs.overseas': ('Быстрые настройки Xiaomi', 'critical', 'Меню быстрых настроек.'),
    'com.xiaomi.mitv.updateservice': ('Обновления Xiaomi', 'critical', 'Системные обновления.'),
    'io.freemytv': ('Free My TV', 'critical', 'Главный экран и восстановление Original/watchdog.'),
    'com.google.android.apps.tv.launcherx': ('Стандартный главный экран Google TV', 'critical', 'Нужен режиму Original; HomeMode сам выключает и возвращает его.'),
    'com.google.android.tvlauncher': ('Стандартный главный экран Android TV', 'critical', 'Нужен режиму Original; HomeMode управляет им сам.'),
    'com.google.android.leanbacklauncher': ('Старый главный экран Android TV', 'critical', 'Запасной HOME.'),
    'com.google.android.apps.tv.dreamx': ('Заставка Google TV', 'critical', 'HomeMode заменяет заставку и возвращает её в Original.'),
    'com.google.android.backdrop': ('Заставка Ambient', 'critical', 'HomeMode заменяет заставку и возвращает её в Original.'),
    'com.mitv.tvhome.atv': ('PatchWall', 'critical', 'Нужен для выбора входов и кнопки Mi (см. CLAUDE.md).'),
    'com.google.android.play.games': ('Google Play Игры', 'homemode', 'Промо-приложение; HomeMode отключает его в режиме Free My TV.'),
    'com.google.android.youtube.tvmusic': ('YouTube Music', 'homemode', 'HomeMode отключает его в режиме Free My TV.'),
    'com.miui.tv.analytics': ('Аналитика MIUI TV', 'homemode', 'Телеметрия Xiaomi; HomeMode отключает её в режиме Free My TV.'),
    'com.xiaomi.statistic': ('Статистика Xiaomi', 'homemode', 'Телеметрия Xiaomi; HomeMode отключает её в режиме Free My TV.'),
    'com.mitv.tvhome.mitvplus': ('Mi TV+', 'homemode', 'Промо-каналы; HomeMode отключает их в режиме Free My TV.'),
    'com.xm.webcontent': ('Промо-контент Xiaomi', 'homemode', 'HomeMode отключает его в режиме Free My TV.'),
    'com.netflix.ninja': ('Netflix (предустановлен)', 'system', 'Системный; если не нужен — Настройки → Приложения → Отключить. Менеджер системные не трогает.'),
    'com.amazon.amazonvideo.livingroom': ('Prime Video (предустановлен)', 'system', 'Уже отключён на устройствах.'),
    'com.google.android.videos': ('Google TV (фильмы)', 'system', 'Покупка фильмов Google; отключается в настройках ТВ, если не нужен.'),
    'com.mitv.gallery': ('Галерея Xiaomi', 'system', 'Просмотр фото с USB.'),
    'com.xiaomi.mimusic2': ('Музыка Xiaomi', 'system', 'Плеер музыки с USB.'),
    'com.xiaomi.mitv.mediaexplorer': ('Медиаплеер Xiaomi', 'system', 'Открывает файлы с USB-накопителей.'),
    'com.mitv.videoplayer': ('Видеоплеер Xiaomi', 'system', 'Плеер для USB.'),
    'com.mitv.tvhome.oemtab': ('Вкладка Xiaomi', 'system', 'Часть PatchWall.'),
    'com.mitv.tvhome.michannel': ('Каналы Xiaomi', 'system', 'Часть PatchWall.'),
    'com.xiaomo.tv.milegal': ('Юридическая информация Xiaomi', 'system', 'Тексты соглашений.'),
    'com.google.android.feedback': ('Отзывы Google', 'system', 'Отправка отзывов.'),
    'com.google.android.tv.bugreportsender': ('Отчёты об ошибках', 'system', 'Отправка отчётов.'),
    'cn.formovie.fengos.usermanual': ('Руководство проектора', 'system', 'Уже отключено.'),
    'com.fengmi.projector.imaging': ('Изображение проектора', 'critical', 'Трапеция, изображение — не трогать.'),
    'com.fengmi.platform': ('Платформа проектора', 'critical', 'Системная служба проектора.'),
    'com.fengmi.fmsettings': ('Настройки проектора', 'critical', 'Проекционные настройки.'),
    'com.fengmi.autofocustest': ('Автофокус проектора', 'critical', 'Автофокусировка.'),
    'com.fengmi.testsuite': ('Тесты проектора', 'critical', 'Заводские тесты; защищены HomeMode.'),
    'com.formovie.launchboard': ('Лаунчер Formovie', 'critical', 'Стандартный главный экран проектора.'),
    'com.formovie.gsettings': ('Настройки Formovie', 'critical', 'Настройки проектора.'),
    'org.smarttube.stable': ('SmartTube', 'keep', 'Нужен для YouTube через выбранный VPN.'),
    'com.google.android.youtube.tv': ('YouTube', 'keep', 'Официальный YouTube.'),
    'llc.itdev.incy': ('INCY', 'keep', 'VPN-клиент только для SmartTube/YouTube.'),
    'io.github.romanvht.byedpi': ('ByeByeDPI', 'keep', 'Альтернатива INCY; одновременно включать нельзя.'),
    'org.videolan.vlc': ('VLC', 'keep', 'Плеер медиатеки.'),
    'net.gtvbox.videoplayer': ('Vimu Player', 'keep', 'Плеер медиатеки.'),
    'net.gtvbox.installer': ('Vimu Installer', 'review', 'Нужен только для установки/обновления Vimu Player; сам плеер на этой приставке не установлен.'),
    'com.mxtech.videoplayer.ad': ('MX Player', 'keep', 'Плеер медиатеки (с рекламой).'),
    'com.teamsmart.videomanager.tv': ('SmartTube (старый пакет)', 'candidate', 'Подписан прежним ключом, скомпрометированным в декабре 2025; заменён org.smarttube.stable. Кандидат на удаление после проверки нового SmartTube.'),
    'com.apkmirror.helper.prod': ('APKMirror Installer', 'candidate', 'Сторонний установщик APK. Если вы ставите APK этим менеджером, он не нужен; меньше установщиков — меньше риск.'),
    'com.uptodown.installer': ('Uptodown', 'candidate', 'Сторонний магазин приложений. Кандидат на удаление, если не используете.'),
    'com.tcl.browser': ('TCL Browser', 'candidate', 'Браузер TCL на приставке Xiaomi; кандидат, если не используете.'),
    'com.mobisystems.fileman': ('File Commander', 'review', 'Файловый менеджер. На приставке №1 их два (ещё FX): оставьте один.'),
    'nextapp.fx': ('FX File Explorer', 'review', 'Второй файловый менеджер на приставке №1: оставьте один.'),
    'com.alphainventor.filemanager': ('File Manager+', 'review', 'Файловый менеджер; умеет SMB — может открыть медиатеку и передать файл в плеер.'),
    'de.blinkt.openvpn': ('OpenVPN for Android', 'review', 'Ещё один VPN-клиент. Менеджер VPN-приложения не трогает; если не используете — остановите VPN и удалите вручную.'),
    'com.avo.vpn': ('VPN-клиент', 'review', 'VPN-клиент; одновременно активен только один VPN. Менеджер его не трогает.'),
    'octohide.vpn': ('Octohide VPN', 'review', 'VPN-клиент на проекторе; менеджер его не трогает.'),
    'io.github.krlvm.powertunnel.android': ('PowerTunnel', 'review', 'Ещё один VpnService (обход DPI). Не включайте вместе с INCY/ByeByeDPI.'),
    'com.google.android.youtube.tvkids': ('YouTube Детям', 'review', 'Нужен, только если пользуются дети.'),
    'com.neilturner.aerialviews': ('Aerial Views', 'review', 'Заставка; при Free My TV используется своя.'),
    'systems.sieber.fsclock': ('Часы-заставка', 'review', 'Заставка; при Free My TV используется своя.'),
    'com.oumi.utility.media.hub': ('Media Hub', 'review', 'Назначение не подтверждено; проверьте, пользуетесь ли.'),
}
KIND_TEXT = {'critical': 'Критично — не трогать', 'homemode': 'Отключает Free My TV (HomeMode)', 'system': 'Системное — только просмотр',
             'candidate': 'Кандидат на удаление', 'review': 'Проверьте, нужно ли', 'keep': 'Нужно для ваших сценариев'}


def parse_diskstats(text):
    """{'package': {'app': bytes, 'data': bytes, 'cache': bytes}} plus totals; Android 8+ format."""
    names = re.search(r'Package Names: \[(.*?)\]', text or '')
    if not names:
        return {'packages': {}, 'totals': {}}
    lists = {k: re.search(k + r': \[(.*?)\]', text) for k in ('App Sizes', 'App Data Sizes', 'Cache Sizes')}
    pkgs = [x.strip().strip('"') for x in names[1].split(',')]
    def numbers(m):
        return [int(x) for x in m[1].split(',')] if m and m[1].strip() else [0] * len(pkgs)
    app, data, cache = (numbers(lists[k]) for k in ('App Sizes', 'App Data Sizes', 'Cache Sizes'))
    if not len(app) == len(data) == len(cache) == len(pkgs):
        return {'packages': {}, 'totals': {}}
    totals = {k.lower().replace(' ', '_'): int(v) for k, v in re.findall(r'^(App Size|App Data Size|App Cache Size): (\d+)', text, re.M)}
    free = re.search(r'^Data-Free: (\d+)K / (\d+)K', text, re.M)
    if free:
        totals.update(data_free=int(free[1]) * 1024, data_total=int(free[2]) * 1024)
    return {'packages': {p: {'app': a, 'data': d, 'cache': c} for p, a, d, c in zip(pkgs, app, data, cache)}, 'totals': totals}


def storage(device):
    return parse_diskstats(device.shell('dumpsys', 'diskstats', timeout=90, check=False))


def analyse(rows, disk, report):
    """Annotate inventory rows; returns (rows, summary). Explains only; protection stays in applications.py."""
    sizes = disk.get('packages', {})
    out = []
    for r in rows:
        label, kind, reason = KB.get(r['package'], (None, None, None))
        if kind is None:
            kind = 'system' if r['system'] else 'keep'
            reason = 'Системный пакет: менеджер не изменяет системные компоненты.' if r['system'] else 'Стороннее приложение; назначение решаете вы. Совпадение слов в имени ничего не доказывает.'
        if r['protected'] and kind in ('candidate', 'review', 'keep') and not r['system']:
            reason += ' Менеджер защищает этот пакет (VPN, Cast, плеер, лаунчер или роль).'
        size = sizes.get(r['package'], {})
        homemode_state = None
        if kind == 'homemode':
            homemode_state = 'уже отключено' if r['disabled'] else 'будет отключено при включении режима Free My TV на ТВ'
        out.append({**r, 'label': label or r.get('label') or r['package'], 'kind': kind, 'kind_text': KIND_TEXT[kind],
                    'advice': reason, 'homemode_state': homemode_state,
                    'app_bytes': size.get('app'), 'data_bytes': size.get('data'), 'cache_bytes': size.get('cache')})
    out.sort(key=lambda x: ({'candidate': 0, 'review': 1, 'homemode': 2, 'keep': 3, 'system': 4, 'critical': 5}[x['kind']], -(x['app_bytes'] or 0)))
    totals = disk.get('totals', {})
    summary = {'free_bytes': report.get('free_bytes'), 'cache_bytes': totals.get('app_cache_size'),
               'candidates': [x['package'] for x in out if x['kind'] == 'candidate' and x['installed']],
               'review': [x['package'] for x in out if x['kind'] == 'review' and x['installed']],
               'homemode_pending': [x['package'] for x in out if x['kind'] == 'homemode' and x['installed'] and not x['disabled']],
               'home': report.get('home', '')}
    notes = []
    if summary['homemode_pending']:
        notes.append('Системные промо- и телеметрические пакеты ещё включены: их отключает сам Free My TV при переходе в режим Free My TV на ТВ. Если переход блокируется Wi-Fi, сначала устраните блокировку в разделе «Free My TV».')
    if summary['cache_bytes']:
        notes.append(f"Кэш приложений: {summary['cache_bytes'] // 1048576} МБ. Его можно очистить отдельной операцией: данные, вход в аккаунты и настройки не удаляются.")
    if summary['free_bytes'] is not None and summary['free_bytes'] < 600 * 1048576:
        notes.append('Мало свободного места. Отключение приложения место не освобождает; удаление APK стороннего приложения освобождает размер APK, данные (-k) остаются.')
    summary['notes'] = notes
    return out, summary


def trim_caches(device):
    """Android's own cache trim: removes only cache files apps can rebuild; never `pm clear`."""
    before = device.free_space()
    disk_before = storage(device).get('totals', {})
    out = device.shell('pm', 'trim-caches', '999G', timeout=180, check=False)
    if re.search(r'Error|Exception|Unknown|Permission', out, re.I):
        raise Problem('Прошивка отказала в очистке кэша.', code='CACHE_TRIM', details=out)
    time.sleep(2)
    after = device.free_space()
    disk_after = storage(device).get('totals', {})
    return {'before_free_bytes': before, 'after_free_bytes': after, 'freed_bytes': after - before,
            'cache_before': disk_before.get('app_cache_size'), 'cache_after': disk_after.get('app_cache_size'),
            'data_cleared': False, 'note': 'Очищен только кэш через штатную команду Android pm trim-caches. Данные, аккаунты и настройки приложений не удалялись. Кэш снова растёт при использовании; dumpsys diskstats обновляет цифры с задержкой.'}


def memory(device):
    text = device.shell('cat', '/proc/meminfo', check=False)
    m = re.search(r'^MemAvailable:\s+(\d+) kB', text, re.M)
    return int(m[1]) * 1024 if m else None


def processes(device):
    return max(0, len(device.shell('ps', '-A', check=False).splitlines()) - 1)


def benchmark(device, apps, packages, event, runs=3):
    """Same steps every time: Home, settle, launch with `am start -W`, back to Home. Median of `runs`; no force-stop."""
    from remote import launchable
    result = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'runs': runs, 'apps': {}}
    device.shell('input', 'keyevent', 'KEYCODE_HOME', check=False)
    time.sleep(5)
    result['ram_available_bytes'] = memory(device)
    result['process_count'] = processes(device)
    entries = launchable(device)
    for key, pkg in apps:
        if pkg not in packages:
            result['apps'][key] = {'status': 'skipped', 'message': f'{pkg} не установлен; замер пропущен.'}
            continue
        component = entries.get(pkg)
        if not component:
            result['apps'][key] = {'status': 'error', 'message': 'Не найден значок запуска; запуск не выполнялся.'}
            continue
        times, states = [], []
        for i in range(runs):
            event(f'Замер {pkg}: запуск {i + 1}/{runs}')
            out = device.shell('am', 'start', '-W', '-n', shlex.quote(component), timeout=90, check=False)
            total = re.search(r'^TotalTime: (\d+)', out, re.M) or re.search(r'^WaitTime: (\d+)', out, re.M)
            if re.search(r'^Status: ok', out, re.M) and total:
                times.append(int(total[1]))
                states.append((re.search(r'^LaunchState: (\w+)', out, re.M) or [None, 'неизвестно'])[1])
            device.shell('input', 'keyevent', 'KEYCODE_HOME', check=False)
            time.sleep(4)
        if not times:
            result['apps'][key] = {'status': 'error', 'message': 'Android не подтвердил запуск; время не считается измеренным.'}
            continue
        median = int(statistics.median(times))
        result['apps'][key] = {'status': 'done', 'ms': median, 'samples': times, 'launch_states': states,
                               'message': f'Медиана {median} мс из {len(times)} запусков (тип: {", ".join(sorted(set(states)))}). Без force-stop; сравнивайте только при похожей нагрузке.'}
    result['ram_available_after_bytes'] = memory(device)
    return result


def history_file(data, identity):
    return Path(data) / 'benchmarks' / (identity[:24] + '.jsonl')


def last_benchmark(data, identity):
    path = history_file(data, identity)
    if not path.exists():
        return None
    lines = [l for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def save_benchmark(data, identity, folder, bench):
    path = history_file(data, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps({**bench, 'report': str(Path(folder).name)}, ensure_ascii=False) + '\n')


def compare(previous, current):
    if not previous:
        return {'available': False, 'message': 'Это первый замер на этом устройстве: сравнивать пока не с чем.'}
    rows = []
    for key, now in current['apps'].items():
        before = previous.get('apps', {}).get(key, {})
        if now.get('status') == 'done' and before.get('status') == 'done':
            same = set(now.get('launch_states', [])) == set(before.get('launch_states', []))
            rows.append({'app': key, 'before_ms': before['ms'], 'after_ms': now['ms'], 'delta_ms': now['ms'] - before['ms'], 'comparable': same})
    ram = (current.get('ram_available_bytes') or 0) - (previous.get('ram_available_bytes') or 0)
    return {'available': True, 'previous_time': previous.get('time'), 'apps': rows, 'ram_delta_bytes': ram,
            'process_delta': (current.get('process_count') or 0) - (previous.get('process_count') or 0),
            'message': 'Разница между двумя замерами. Колебания ±10–20% нормальны; изменение свободной ОЗУ само по себе не доказывает ускорения.'}
