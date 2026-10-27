"""ADB/package operations. No root, removal, Home switching, or device settings writes."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
APK_HASH = 'e15a7b9f0fce46ff90c6fd7d23f41b1689c6eaed755a4a50099096b2c36e8c04'
ALLOWED_YOUTUBE = {'org.smarttube.stable', 'org.smarttube.beta', 'com.google.android.youtube.tv'}
DOWNLOAD_HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                  'raw.githubusercontent.com', 'api.github.com', 'downloads.videolan.org', 'dl.google.com', 'api.nuget.org'}
_CACHE_LOCK = threading.Lock()

class Problem(Exception):
    """Safe, user-facing message, never include subprocess arguments containing secrets."""


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def target(value):
    """Only literal RFC1918 IPv4 and optional ADB port; no shell/hostname input."""
    parts = value.strip().split(':')
    if len(parts) > 2:
        raise Problem('Нужен локальный IPv4, например 192.168.28.60:5555.')
    try:
        ip = ipaddress.IPv4Address(parts[0])
        private = any(ip in ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        port = int(parts[1]) if len(parts) == 2 else 5555
        if not private or not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, ipaddress.AddressValueError):
        raise Problem('Введите домашний IPv4 и порт от 1 до 65535.') from None
    return f'{ip}:{port}'


def targets(values):
    result = [target(v) for v in values if v.strip()]
    if not result:
        raise Problem('Укажите хотя бы одно устройство.')
    if len(set(result)) != len(result) or len(result) > 3:
        raise Problem('Укажите до трёх разных адресов устройств.')
    return result


def run(args, timeout=40, check=True):
    try:
        r = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise Problem('Время ожидания истекло. Проверьте экран ТВ, ADB и сеть; повторите шаг.') from None
    except OSError:
        raise Problem('Не удалось запустить инструмент. Проверьте наличие ADB/Java и права доступа.') from None
    output = r.stdout + '\n' + r.stderr
    if check and r.returncode:
        hints = {'unauthorized': 'Подтвердите разрешение USB-отладки на экране ТВ.',
                 'offline': 'ADB offline: проверьте соединение и повторите проверку.',
                 'INSTALL_FAILED_UPDATE_INCOMPATIBLE': 'Подписи не совпадают; установленное приложение сохранено.',
                 'INSTALL_FAILED_INSUFFICIENT_STORAGE': 'Недостаточно памяти; ничего не удаляйте автоматически.',
                 'INSTALL_FAILED_VERSION_DOWNGRADE': 'На устройстве более новая версия; понижение запрещено.'}
        for key, message in hints.items():
            if key in output:
                raise Problem(message)
        raise Problem('Инструмент завершился с ошибкой. Проверьте ADB, совместимость APK и разрешения на ТВ.')
    return output.strip()


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != 'https' or u.hostname not in DOWNLOAD_HOSTS or u.username or u.password:
        raise Problem('Источник загрузки не входит в список официальных разрешённых адресов.')


def download(url, path, expected, algorithm='sha256'):
    check_url(url)
    if not re.fullmatch(r'[a-f0-9]{' + ('64' if algorithm == 'sha256' else '40') + '}', expected):
        raise Problem('Нет доверенной контрольной суммы; загрузка пропущена.')
    path = Path(path)
    with _CACHE_LOCK:
        if path.exists() and digest(path, algorithm) == expected:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.partial')
        try:
            opener = urllib.request.build_opener(HTTPSOnly())
            with opener.open(url, timeout=40) as response, tmp.open('wb') as f:
                total = 0
                while b := response.read(1024 * 1024):
                    total += len(b)
                    if total > 600 * 1024 * 1024:
                        raise Problem('Размер загрузки превышает ожидаемый предел.')
                    f.write(b)
            if digest(tmp, algorithm) != expected:
                raise Problem('Контрольная сумма не совпала. Файл не использован.')
            tmp.replace(path)
        except Problem:
            raise
        except Exception:
            raise Problem('Не удалось скачать файл по HTTPS. Проверьте интернет; сохранённые APK не удалены.') from None
        finally:
            tmp.unlink(missing_ok=True)
    return path


def extract_zip(archive, destination):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            name = item.filename.replace('\\', '/')
            p = (destination / name).resolve()
            if not p.is_relative_to(destination) or ':' in name or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise Problem('Небезопасный путь в архиве; распаковка отменена.')
        z.extractall(destination)


def parse_badging(text):
    pkg = re.search(r"package: name='([^']+)' versionCode='(\d+)'", text)
    sdk = re.search(r"sdkVersion:'(\d+)'", text)
    if not pkg or not sdk:
        raise Problem('Не удалось определить пакет, API или версию APK.')
    libs = re.search(r'native-code: (.+)', text)
    return {'package': pkg[1], 'version': int(pkg[2]), 'min_api': int(sdk[1]),
            'abis': re.findall(r"'([^']+)'", libs[1]) if libs else []}


def certs(output):
    values = re.findall(r'Signer #\d+ certificate SHA-256 digest: ([a-fA-F0-9]{64})', output)
    if not values:
        raise Problem('Не удалось проверить сертификат APK; установка пропущена.')
    return sorted(set(v.lower() for v in values))


class Tools:
    def __init__(self, data=DATA):
        self.data = Path(data)
        self.adb = Path(r'C:\platform-tools\adb.exe') if os.name == 'nt' else Path(shutil.which('adb') or '/workspace/android-sdk/platform-tools/adb')
        self.home = ROOT / 'runtime'
        self.lock = threading.Lock()

    def ensure(self):
        with self.lock:
            if os.name != 'nt':
                self.java = Path('/workspace/jdk/jdk-17.0.21+9/bin/java')
                self.aapt = Path('/workspace/android-sdk/build-tools/34.0.0/aapt2')
                self.jar = Path('/workspace/android-sdk/build-tools/34.0.0/lib/apksigner.jar')
                return
            d = read_json(ROOT / 'dependencies.json')
            self.home.mkdir(exist_ok=True)
            java = list((self.home / 'jre').glob('*/bin/java.exe'))
            if not java:
                f = download(d['jre']['url'], self.data / 'cache/jre.zip', d['jre']['sha256'])
                extract_zip(f, self.home / 'jre')
                java = list((self.home / 'jre').glob('*/bin/java.exe'))
            if not java:
                raise Problem('Java не найдена после распаковки.')
            self.java = java[0]
            self.aapt = self.home / 'android/android-14/aapt2.exe'
            self.jar = self.home / 'android/android-14/lib/apksigner.jar'
            if not self.aapt.is_file() or not self.jar.is_file():
                f = download(d['android']['url'], self.data / 'cache/android.zip', d['android']['sha256'])
                if digest(f, 'sha1') != d['android']['sha1']:
                    raise Problem('Не совпала опубликованная Google контрольная сумма SDK.')
                extract_zip(f, self.home / 'android')
            if not self.aapt.is_file() or not self.jar.is_file():
                raise Problem('Android Build Tools 34 не найдены после распаковки.')

    def inspect(self, apk, api=None):
        self.ensure()
        info = parse_badging(run([self.aapt, 'dump', 'badging', apk]))
        # Verify every APK with Android's cryptographic verifier, including installed ones.
        out = run([self.java, '-jar', self.jar, 'verify', '--min-sdk-version',
                   str(api or info['min_api']), '--print-certs', apk], timeout=90)
        info['certificates'] = certs(out)
        return info


class Device:
    def __init__(self, address, tools):
        self.address = target(address)
        self.tools = tools

    def adb(self, *args, timeout=40, check=True):
        return run([self.tools.adb, '-s', self.address, *args], timeout, check)

    def shell(self, *args, **kw):
        return self.adb('shell', *args, **kw)

    def connect(self):
        output = run([self.tools.adb, 'connect', self.address])
        if 'unauthorized' in output:
            raise Problem('Подтвердите USB-отладку на экране телевизора.')
        if self.adb('get-state') != 'device':
            raise Problem('Устройство недоступно. Разрешите USB-отладку и проверьте IP/порт.')

    def prop(self, key):
        return self.shell('getprop', key).strip()

    def packages(self):
        s = self.shell('pm', 'list', 'packages')
        if not s or 'Error' in s or 'Exception' in s:
            raise Problem('Не удалось получить список пакетов; установка остановлена для этого ТВ.')
        return sorted(set(re.findall(r'^package:([\w.]+)$', s, re.M)))

    def free_space(self):
        s = self.shell('df', '-k', '/data')
        lines = [l.split() for l in s.splitlines() if l.strip()]
        try:
            cols = lines[-1]
            return int(cols[3]) * 1024
        except (ValueError, IndexError):
            raise Problem('Не удалось определить свободное место /data; установка пропущена.') from None

    def measures(self):
        result = {}
        for name, args in [('memory', ('cat', '/proc/meminfo')), ('cpu', ('dumpsys', 'cpuinfo')),
                           ('processes', ('ps', '-A')), ('launcher_memory', ('dumpsys', 'meminfo', 'io.freemytv')),
                           ('launcher_frames', ('dumpsys', 'gfxinfo', 'io.freemytv', 'framestats'))]:
            try:
                result[name] = self.shell(*args)
            except Problem as e:
                result[name] = str(e)
        result['process_count'] = max(0, len(result['processes'].splitlines()) - 1)
        return result

    def diagnose(self):
        self.connect()
        props = {k: self.prop(k) for k in ['ro.product.model', 'ro.product.manufacturer',
                 'ro.build.version.release', 'ro.build.version.sdk', 'ro.product.cpu.abilist',
                 'ro.product.cpu.abi', 'ro.serialno', 'ro.build.fingerprint']}
        try:
            api = int(props['ro.build.version.sdk'])
        except ValueError:
            raise Problem('Не удалось определить Android API.') from None
        packages = self.packages()
        versions = self.shell('dumpsys', 'package', 'packages')
        abi = props['ro.product.cpu.abilist'] or props['ro.product.cpu.abi']
        identity = hashlib.sha256((props['ro.serialno'] + props['ro.build.fingerprint'] + props['ro.product.model']).encode()).hexdigest()
        return {'address': self.address, 'properties': props, 'model': props['ro.product.model'],
                'android': props['ro.build.version.release'], 'api': api, 'abis': abi.split(','),
                'identity': identity, 'packages': packages, 'versions': versions,
                'free_bytes': self.free_space(), 'before': self.measures(),
                'recommendations': self.recommendations(packages)}

    @staticmethod
    def recommendations(packages):
        candidates = [p for p in packages if any(w in p.lower() for w in ['telemetry', 'analytics', 'advert', 'demo'])]
        return {'unverified_candidates': candidates, 'note': 'Совпадение имени — не доказательство ненужности. Ничего не отключено. Каждый пакет требует анализа служб, разрешений, зависимостей и отдельного согласия.'}

    def installed(self, pkg, folder, api):
        if pkg not in self.packages():
            return None
        s = self.shell('pm', 'path', pkg)
        paths = re.findall(r'^package:(/[^\r\n]+\.apk)$', s, re.M)
        if not paths:
            raise Problem('Не удалось сохранить установленный APK; обновление пропущено.')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        result = []
        for i, path in enumerate(paths):
            if not re.fullmatch(r'/[A-Za-z0-9_./+=,@~-]+\.apk', path):
                raise Problem('Неожиданный путь APK; обновление пропущено.')
            local = folder / f'{i}-{Path(path).name}'
            self.adb('pull', path, str(local), timeout=150)
            info = self.tools.inspect(local, api)
            if info['package'] != pkg:
                raise Problem('Сохранённый APK относится к другому пакету.')
            result.append(info)
        base = next((x for x, p in zip(result, paths) if p.endswith('/base.apk')), result[0])
        if any(x['certificates'] != base['certificates'] for x in result):
            raise Problem('Сертификаты установленного split APK различаются; обновление пропущено.')
        save_json(folder / 'metadata.json', {'package': pkg, 'parts': result, 'paths': paths})
        return base


def choose_variant(app, abis):
    for abi in abis:
        if abi in app['variants']:
            return app['variants'][abi]
    if 'universal' in app['variants']:
        return app['variants']['universal']
    raise Problem('Нет APK для архитектуры этого устройства.')


def compatible(info, report):
    if info['min_api'] > report['api']:
        raise Problem('APK требует более новую версию Android; установка пропущена.')
    if info['abis'] and not set(info['abis']).intersection(report['abis']):
        raise Problem('Архитектура APK не подходит этому устройству.')


def install_app(device, report, key, app, tools, folder, event):
    variant = choose_variant(app, report['abis'])
    path = Path(tools.data) / 'cache' / (variant['sha256'] + '.apk')
    # Prefer the exact first-stage artifact shipped in the Windows bundle.
    local = ROOT / 'payload/FreeMyTV.apk'
    if key == 'freemytv' and local.exists() and digest(local) == APK_HASH:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            shutil.copyfile(local, path)
    check_release(key, app, variant)
    event('Скачивание и проверка ' + app['label'])
    path = download(variant['url'], path, variant['sha256'])
    info = tools.inspect(path, report['api'])
    if info['package'] != app['package'] or info['certificates'] != [app['certificate']]:
        raise Problem('Пакет или сертификат не совпадает с проверенным каталогом; установка запрещена.')
    compatible(info, report)
    if device.prop('ro.serialno') != report['properties']['ro.serialno']:
        raise Problem('По этому IP отвечает другое устройство. Повторите диагностику.')
    old = device.installed(info['package'], Path(folder) / 'backups' / key, report['api'])
    if old:
        if old['certificates'] != info['certificates']:
            return {'status': 'skipped', 'message': 'Конфликт сертификатов: существующая версия сохранена. Переход — вручную после резервного копирования; не удаляйте работающий Home.'}
        if old['version'] >= info['version']:
            return {'status': 'done', 'message': 'Такая же или более новая версия уже установлена; повторная установка не нужна.'}
    if device.free_space() < path.stat().st_size * 4 + 128 * 1024 * 1024:
        raise Problem('Недостаточно места для безопасной установки. Автоматическая очистка запрещена.')
    event('Установка ' + app['label'])
    output = device.adb('install', '-r', str(path), timeout=240)
    if not re.search(r'^Success\s*$', output, re.M):
        raise Problem('ADB не подтвердил установку. Проверьте системный запрос на ТВ.')
    # Query and verify the actual installed APK, not only ADB's success string.
    actual = device.installed(info['package'], Path(folder) / 'verified' / key, report['api'])
    if not actual or actual['version'] != info['version'] or actual['certificates'] != info['certificates']:
        raise Problem('После установки пакет/версия/подпись не подтвердились. Не удаляйте приложение; повторите диагностику.')
    return {'status': 'done', 'message': 'Версия и подпись установленного приложения проверены.', 'new_install': old is None,
            'package': info['package'], 'certificate': app['certificate'], 'version': info['version']}


def vpn_profile(packages, include_official=False):
    allowed = [p for p in packages if p in ALLOWED_YOUTUBE and (include_official or p != 'com.google.android.youtube.tv')]
    if not allowed:
        raise Problem('Нет установленного SmartTube/YouTube. Пустой белый список НЕ создаётся: Android может направить через VPN все приложения.')
    return {'app': 'io.github.romanvht.byedpi', 'version': '1.8.2', 'apps': sorted(allowed),
            'settings': {'byedpi_mode': 'vpn', 'applist_type': 'whitelist', 'autostart': False, 'auto_connect': False}}


def check_release(key, app, variant):
    """Fail closed if upstream removed/replaced the reviewed release or published a newer one."""
    try:
        opener = urllib.request.build_opener(HTTPSOnly())
        if key in ('smarttube', 'byedpi'):
            repo = 'yuliskov/SmartTube' if key == 'smarttube' else 'romanvht/ByeByeDPI'
            req = urllib.request.Request('https://api.github.com/repos/' + repo + '/releases/latest', headers={'User-Agent': 'XiaomiTVManager/1.0'})
            with opener.open(req, timeout=20) as r:
                release = json.loads(r.read(2 * 1024 * 1024))
            if release.get('tag_name') != app['release'] or release.get('draft') or release.get('prerelease'):
                raise Problem('Официальный релиз изменился: обновите каталог менеджера перед установкой. Старую сборку автоматически не устанавливаем.')
            asset = next((a for a in release.get('assets', []) if a.get('browser_download_url') == variant['url']), None)
            if not asset or asset.get('digest') != 'sha256:' + variant['sha256']:
                raise Problem('Официальные данные о происхождении APK изменились; установка пропущена.')
        elif key == 'vlc':
            with opener.open(variant['url'] + '.sha256', timeout=20) as r:
                published = r.read(4096).decode().split()[0].lower()
            if published != variant['sha256']:
                raise Problem('Опубликованная VideoLAN контрольная сумма изменилась; установка пропущена.')
    except Problem:
        raise
    except Exception:
        raise Problem('Не удалось подтвердить актуальный официальный источник APK. Приложение пропущено; проверьте интернет.') from None
