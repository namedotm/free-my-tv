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
                  'get.vimu.tv', 'vimu.tv', 'raw.githubusercontent.com', 'api.github.com', 'downloads.videolan.org', 'dl.google.com', 'api.nuget.org'}
_CACHE_LOCK = threading.Lock()

class Problem(Exception):
    """Safe, user-facing message, never include subprocess arguments containing secrets."""

    def __init__(self, message, *, code='', details=''):
        super().__init__(message)
        self.code = code
        self.details = safe_details(details)


def safe_details(text):
    text = re.sub(r'https?://\S+', '<HTTPS-адрес>', str(text))
    text = re.sub(r'(?i)(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|(?:token|password|authorization)\s*[:=]\s*\S+)', '[скрыто]', text)
    return text.strip()[:2400]


def command_problem(output, tool='инструмент', returncode=None):
    hints = {
        'INSTALL_FAILED_USER_RESTRICTED': 'Android запретил установку. Проверьте запрос и разрешения установки на экране ТВ; защиту не отключайте.',
        'INSTALL_FAILED_ABORTED': 'Установка отменена на ТВ или системным установщиком. Проверьте экран и повторите шаг.',
        'INSTALL_FAILED_DEPRECATED_SDK_VERSION': 'Android отклонил APK с устаревшим target SDK. Нужна совместимая сборка; обход ограничения не выполняется.',
        'INSTALL_FAILED_UPDATE_INCOMPATIBLE': 'Подписи не совпадают. Работающая версия сохранена; удаление и очистка данных не выполняются.',
        'INSTALL_FAILED_INSUFFICIENT_STORAGE': 'Android сообщил о нехватке места. Освободите место вручную и повторите шаг.',
        'INSTALL_FAILED_VERSION_DOWNGRADE': 'На ТВ более новая версия. Понижение и удаление приложения запрещены.',
        'INSTALL_FAILED_NO_MATCHING_ABIS': 'APK не подходит архитектуре ТВ. Требуется другой официальный вариант APK.',
        'INSTALL_FAILED_OLDER_SDK': 'APK требует более новую версию Android.',
        'INSTALL_PARSE_FAILED': 'Android не смог разобрать APK. Проверьте технические сведения; файл не будет установлен повторно без проверки.',
        'unauthorized': 'Подтвердите USB-отладку на экране ТВ и повторите шаг.',
        'offline': 'ADB offline. Проверьте соединение с ТВ и повторите подключение.',
        'device not found': 'ADB потерял подключение к ТВ. Проверьте IP, сеть и разрешение отладки.',
        'failed to connect': 'Не удалось подключиться к ADB. Проверьте IP, порт и отладку на ТВ.',
        'cannot connect': 'ADB недоступен по этому IP/порту. Проверьте сеть и отладку на ТВ.',
        'Permission denied': 'Недостаточно прав для чтения APK или выполнения команды. Настройки и данные не изменялись.',
        'DOES NOT VERIFY': 'Криптографическая проверка подписи APK не пройдена. Установка остановлена.',
    }
    for code, message in hints.items():
        if code.lower() in output.lower():
            return Problem(message, code=code, details=output)
    code = re.search(r'(INSTALL_(?:FAILED|PARSE_FAILED)_[A-Z_]+)', output)
    return Problem(f'Ошибка: {tool}' + (f' (код завершения {returncode})' if returncode is not None else '') + '. Откройте технические сведения ниже.',
                   code=code[1] if code else 'TOOL_FAILED', details=output)


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
        raise Problem('Время ожидания истекло. Проверьте экран ТВ и подключение. Перед повтором менеджер заново проверит установленную версию.', code='TIMEOUT') from None
    except OSError as exc:
        raise Problem('Не удалось запустить инструмент. Проверьте наличие ADB/Java и права доступа.', code='TOOL_START', details=str(exc)) from None
    output = r.stdout + '\n' + r.stderr
    if check and r.returncode:
        raise command_problem(output, Path(str(args[0])).name, r.returncode)
    return output.strip()


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != 'https' or u.hostname not in DOWNLOAD_HOSTS or u.username or u.password:
        raise Problem('Источник загрузки не входит в список официальных разрешённых адресов.')


def download(url, path, expected, algorithm='sha256', progress=None):
    check_url(url)
    if not re.fullmatch(r'[a-f0-9]{' + ('64' if algorithm == 'sha256' else '40') + '}', expected):
        raise Problem('Нет доверенной контрольной суммы; загрузка пропущена.')
    path = Path(path)
    notify = progress or (lambda *args, **kwargs: None)
    notify('download', 'Ожидание очереди загрузки', waiting=True)
    with _CACHE_LOCK:
        if path.exists() and digest(path, algorithm) == expected:
            notify('download', 'Файл уже скачан; контрольная сумма проверена', received=path.stat().st_size, total=path.stat().st_size, cached=True)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.partial')
        try:
            opener = urllib.request.build_opener(HTTPSOnly())
            with opener.open(url, timeout=40) as response, tmp.open('wb') as f:
                received = 0
                length = response.headers.get('Content-Length') if hasattr(response, 'headers') else None
                total = int(length) if length and length.isdigit() else None
                notified = 0
                notify('download', 'Скачивание APK', received=0, total=total)
                while b := response.read(1024 * 1024):
                    received += len(b)
                    if received > 600 * 1024 * 1024:
                        raise Problem('Размер загрузки превышает ожидаемый предел.')
                    f.write(b)
                    if time.monotonic() - notified >= .2:
                        notify('download', 'Скачивание файла', received=received, total=total)
                        notified = time.monotonic()
                notify('download', 'Загрузка завершена; проверка SHA-256', received=received, total=total)
            if digest(tmp, algorithm) != expected:
                raise Problem('Контрольная сумма не совпала. Файл не использован.')
            tmp.replace(path)
        except Problem:
            raise
        except Exception as exc:
            raise Problem('Не удалось скачать файл по HTTPS. Проверьте интернет; сохранённые APK не удалены.', code='DOWNLOAD_FAILED', details=str(exc)) from None
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
    header = re.search(r'^package:\s*(.+)$', text, re.M)
    fields = dict(re.findall(r"([\w]+)='([^']*)'", header[1])) if header else {}
    sdk = re.search(r"^sdkVersion:\s*'([^']+)'", text, re.M)
    split = bool(fields.get('split'))
    if not fields.get('name') or not fields.get('versionCode', '').isdigit():
        raise Problem('Не удалось прочитать пакет или versionCode APK. Откройте технические сведения.', code='APK_METADATA', details=text)
    # A config split does not have to repeat uses-sdk from its base. Its API
    # compatibility is checked against the base APK, never guessed from the split.
    if sdk and not sdk[1].isdigit():
        raise Problem('APK требует предварительную/неизвестную версию Android.', code='APK_PREVIEW_SDK', details=sdk[0])
    if not sdk and not split:
        raise Problem('В базовом APK не найдена минимальная версия Android. Нужна проверка манифеста.', code='APK_MIN_SDK', details=text)
    libs = re.search(r'native-code: (.+)', text)
    return {'package': fields['name'], 'version': int(fields['versionCode']), 'min_api': int(sdk[1]) if sdk else None,
            'split': split, 'abis': re.findall(r"'([^']+)'", libs[1]) if libs else []}


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

    def ensure(self, progress=None):
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
                notify = (lambda stage, msg, **meta: progress('tools', 'Подготовка Java: ' + msg, **meta)) if progress else None
                f = download(d['jre']['url'], self.data / 'cache/jre.zip', d['jre']['sha256'], progress=notify)
                extract_zip(f, self.home / 'jre')
                java = list((self.home / 'jre').glob('*/bin/java.exe'))
            if not java:
                raise Problem('Java не найдена после распаковки.')
            self.java = java[0]
            self.aapt = self.home / 'android/android-14/aapt2.exe'
            self.jar = self.home / 'android/android-14/lib/apksigner.jar'
            if not self.aapt.is_file() or not self.jar.is_file():
                notify = (lambda stage, msg, **meta: progress('tools', 'Android-инструменты: ' + msg, **meta)) if progress else None
                f = download(d['android']['url'], self.data / 'cache/android.zip', d['android']['sha256'], progress=notify)
                if digest(f, 'sha1') != d['android']['sha1']:
                    raise Problem('Не совпала опубликованная Google контрольная сумма SDK.')
                extract_zip(f, self.home / 'android')
            if not self.aapt.is_file() or not self.jar.is_file():
                raise Problem('Android Build Tools 34 не найдены после распаковки.')

    def inspect(self, apk, api=None, progress=None):
        self.ensure(progress)
        info = parse_badging(run([self.aapt, 'dump', 'badging', apk]))
        if info['split'] and api is None:
            raise Problem('Отдельный split-APK нельзя установить как полное приложение.', code='APK_SPLIT')
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
        output = run([self.tools.adb, 'connect', self.address], check=False)
        if 'unauthorized' in output:
            raise Problem('Подтвердите USB-отладку на экране телевизора.')
        state = self.adb('get-state', check=False)
        if state != 'device':
            raise command_problem(output + '\n' + state, 'подключение к ADB')

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

    def installed(self, pkg, folder, api, progress=None):
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
            if progress:
                progress('backup', f'Сохранение и проверка установленного APK: часть {i + 1}/{len(paths)}', part=i + 1, parts=len(paths))
            if not re.fullmatch(r'/[A-Za-z0-9_./+=,@~-]+\.apk', path):
                raise Problem('Неожиданный путь APK; обновление пропущено.')
            local = folder / f'{i}-{Path(path).name}'
            self.adb('pull', path, str(local), timeout=150)
            info = self.tools.inspect(local, api, progress=progress)
            if info['package'] != pkg:
                raise Problem('Сохранённый APK относится к другому пакету.')
            result.append(info)
        bases = [info for info in result if not info.get('split')]
        if len(bases) != 1:
            raise Problem('Не удалось однозначно найти базовый APK установленного приложения. Обновление пропущено.', code='APK_BASE_MISSING')
        base = bases[0]
        if any(x['certificates'] != base['certificates'] for x in result):
            raise Problem('Сертификаты установленного split APK различаются; обновление пропущено.')
        if any(x['version'] != base['version'] for x in result):
            raise Problem('Версии базового APK и его частей различаются; обновление пропущено.', code='APK_SPLIT_VERSION')
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
    if info.get('split') or info['min_api'] is None:
        raise Problem('Для установки нужен полный APK, а не отдельный split.', code='APK_SPLIT')
    if info['min_api'] > report['api']:
        raise Problem('APK требует более новую версию Android; установка пропущена.')
    if info['abis'] and not set(info['abis']).intersection(report['abis']):
        raise Problem('Архитектура APK не подходит этому устройству.')


def install_app(device, report, key, app, tools, folder, event, progress=None):
    emit = progress or (lambda *args, **kwargs: None)
    variant = choose_variant(app, report['abis'])
    path = Path(tools.data) / 'cache' / (variant['sha256'] + '.apk')
    # Prefer the exact first-stage artifact shipped in the Windows bundle.
    local = ROOT / 'payload/FreeMyTV.apk'
    if key == 'freemytv' and local.exists() and digest(local) == APK_HASH:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            shutil.copyfile(local, path)
    emit('source', 'Проверка официального источника и выбранной архитектуры')
    check_release(key, app, variant)
    event('Скачивание и проверка ' + app['label'])
    path = download(variant['url'], path, variant['sha256'], progress=emit)
    emit('tools', 'Подготовка инструментов проверки APK')
    tools.ensure(progress=emit)
    emit('inspect', 'Проверка SHA-256, подписи, версии Android и архитектуры APK')
    info = tools.inspect(path, report['api'], progress=emit)
    if info['package'] != app['package'] or info['certificates'] != [app['certificate']]:
        raise Problem('Пакет или сертификат не совпадает с проверенным каталогом; установка запрещена.')
    compatible(info, report)
    if device.prop('ro.serialno') != report['properties']['ro.serialno']:
        raise Problem('По этому IP отвечает другое устройство. Повторите диагностику.')
    emit('backup', 'Проверка установленной версии и сохранение APK перед обновлением')
    old = device.installed(info['package'], Path(folder) / 'backups' / key, report['api'], progress=emit)
    if old:
        if old['certificates'] != info['certificates']:
            return {'status': 'skipped', 'message': 'Конфликт сертификатов: существующая версия сохранена. Переход — вручную после резервного копирования; не удаляйте работающий Home.'}
        if old['version'] >= info['version']:
            return {'status': 'done', 'already_installed': True, 'package': info['package'],
                    'version': old['version'], 'certificate': app['certificate'],
                    'message': 'Такая же или более новая версия уже установлена; повторная установка не нужна.'}
    available = device.free_space()
    required = path.stat().st_size * 4 + 128 * 1024 * 1024
    if available < required:
        raise Problem(f'Недостаточно места: доступно {available // 1048576} МБ, для этого APK требуется запас {required // 1048576} МБ. Освободите место вручную.', code='STORAGE')
    event('Установка ' + app['label'])
    emit('install', 'Передача APK на ТВ и установка. При запросе подтвердите действие на экране ТВ.')
    output = device.adb('install', '-r', str(path), timeout=240)
    if not re.search(r'^Success\s*$', output, re.M):
        raise command_problem(output, 'установка APK')
    # Query and verify the actual installed APK, not only ADB's success string.
    emit('verify', 'Установка завершена; проверка реально установленной версии и подписи')
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
