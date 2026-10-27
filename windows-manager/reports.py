"""Plain Russian per-device report from read-only diagnostics. No serials, keys or IDs in the text."""
import re
from pathlib import Path

WATCH = [('io.freemytv', 'Free My TV'), ('llc.itdev.incy', 'INCY'), ('com.google.android.youtube.tv', 'YouTube (официальный)'),
         ('org.smarttube.stable', 'SmartTube'), ('com.teamsmart.videomanager.tv', 'SmartTube (старый пакет)'),
         ('org.videolan.vlc', 'VLC'), ('net.gtvbox.videoplayer', 'Vimu Player'), ('net.gtvbox.installer', 'Vimu Installer'),
         ('com.mxtech.videoplayer.ad', 'MX Player'), ('io.github.romanvht.byedpi', 'ByeByeDPI'),
         ('com.smartdevice.tv.aircast', 'AirCast'), ('com.android.vending', 'Google Play')]


def package_versions(dump):
    """versionName/versionCode/installer of watched packages from `dumpsys package packages`."""
    result = {}
    for block in re.split(r'\n\s*Package \[', dump or ''):
        name = re.match(r'([\w.]+)\]', block)
        # The first block is the installed update; "Hidden system packages" repeat the factory copy later.
        if not name or name[1] not in dict(WATCH) or name[1] in result:
            continue
        get = lambda key: (re.search(key + r'=(\S+)', block) or [None, ''])[1]
        result[name[1]] = {'version_name': get('versionName'), 'version_code': get('versionCode'),
                           'installer': get('installerPackageName')}
    return result


def text(report, source=False):
    mb = lambda b: f'{b / 1048576:.0f} МБ' if isinstance(b, (int, float)) else 'неизвестно'
    ram = report.get('ram_available_kb')
    lines = [f"Устройство: {report.get('manufacturer', '')} {report.get('model', '')}".strip(),
             f"Адрес ADB в этом запуске: {report.get('address', '')}",
             f"Роль: {'проектор-источник (только чтение настроек; лаунчер не обновляется)' if source else 'целевое устройство'}",
             f"Android {report.get('android')} · API {report.get('api')} · ABI {', '.join(report.get('abis', []))}",
             f"Свободно во внутренней памяти /data: {mb(report.get('free_bytes'))}",
             f"Доступно оперативной памяти (MemAvailable): {f'{ram // 1024} МБ' if ram else 'неизвестно'} — это не место для APK",
             f"Текущий главный экран (HOME): {report.get('home') or 'не определён'}",
             f"VPN: {report.get('vpn', {}).get('message', 'не проверялся')}", '', 'Приложения:']
    versions = package_versions(report.get('versions', ''))
    packages = set(report.get('packages', []))
    for pkg, label in WATCH:
        if pkg in packages:
            v = versions.get(pkg, {})
            channel = {'com.android.vending': 'Google Play', 'null': 'ADB/APK',
                       'com.google.android.packageinstaller': 'установщик пакетов (APK)'}.get(v.get('installer'), v.get('installer') or 'не указан (ADB или система)')
            lines.append(f"  + {label}: {v.get('version_name') or '?'} (code {v.get('version_code') or '?'}), источник: {channel}")
        else:
            lines.append(f'  - {label}: не установлено')
    rec = report.get('recommendations', {})
    if rec.get('warnings'):
        lines += ['', 'Требует внимания:'] + ['  ! ' + w for w in rec['warnings']]
    if rec.get('unverified_candidates'):
        lines += ['', 'Пакеты для отдельного анализа (ничего не отключено, совпадение имени ничего не доказывает):',
                  '  ' + ', '.join(rec['unverified_candidates'])]
    lines += ['', 'Сырые данные: before.json/after.json в этой папке. Отчёт не содержит серийных номеров и ключей.']
    return '\n'.join(lines) + '\n'


def write(folder, report, source=False):
    Path(folder, 'REPORT-RU.txt').write_text(text(report, source), encoding='utf-8-sig')
