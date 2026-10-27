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
# The package SmartTube used before its signing key was exposed (December 2025).
# Never added to favorites or VPN lists; removal stays the user's decision.
OLD_SMARTTUBE = 'com.teamsmart.videomanager.tv'
# Apps that must stay outside any tunnel: players, file managers, casting.
LOCAL_APPS = {'org.videolan.vlc': 'VLC', 'net.gtvbox.videoplayer': 'Vimu', 'com.mxtech.videoplayer.ad': 'MX Player',
              'com.mxtech.videoplayer.pro': 'MX Player Pro', 'com.smartdevice.tv.aircast': 'AirCast',
              'com.alphainventor.filemanager': 'File Commander', 'com.mobisystems.fileman': 'File Commander',
              'nextapp.fx': 'FX File Explorer', 'com.xiaomi.mitv.smartshare': 'Miracast/Smart Share'}
VPN_APPS = {'io.github.romanvht.byedpi': 'ByeByeDPI', 'llc.itdev.incy': 'INCY'}
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
    if not output.strip() and returncode == 255 and tool == 'adb.exe':
        return Problem('ADB потерял связь с ТВ: устройство offline, уснуло или сменило сеть. Включите экран ТВ, проверьте адрес и повторите шаг; изменения не выполнялись.', code='ADB_LOST')
    code = re.search(r'(INSTALL_(?:FAILED|PARSE_FAILED)_[A-Z_]+)', output)
    return Problem(f'Ошибка: {tool}' + (f' (код завершения {returncode})' if returncode is not None else '') + '. Откройте технические сведения ниже.',
                   code=code[1] if code else 'TOOL_FAILED', details=output)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
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
    target = re.search(r"^targetSdkVersion:\s*'(\d+)'", text, re.M)
    label = re.search(r"^application-label(?:-ru)?:\s*'([^'\n]{1,120})'", text, re.M) or re.search(r"^application:\s*label='([^'\n]{1,120})'", text, re.M)
    icons = sorted(((int(d), p) for d, p in re.findall(r"^application-icon-(\d+):'([^'\n]+)'", text, re.M)), reverse=True)
    banner = re.search(r"^application:.*?banner='([^'\n]+)'", text, re.M)
    return {'package': fields['name'], 'version': int(fields['versionCode']), 'min_api': int(sdk[1]) if sdk else None,
            'split': split, 'split_name': fields.get('split', ''), 'abis': re.findall(r"'([^']+)'", libs[1]) if libs else [],
            'version_name': fields.get('versionName', ''), 'target_api': int(target[1]) if target else None,
            'label': label[1] if label else '', 'icons': [p for _, p in icons] + ([banner[1]] if banner else []),
            'leanback': 'leanback-launchable-activity' in text or 'android.software.leanback' in text,
            'test_only': bool(re.search(r"^testOnly='-1'|^application-debuggable", text, re.M))}


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
            if len(str(java[0].parent / 'server' / 'jvm.dll')) >= 250:
                raise Problem('Папка менеджера лежит слишком глубоко: Java в Windows не запускается из путей длиннее 260 символов. Перенесите папку XiaomiTVManager, например, в C:\\XiaomiTVManager или на Рабочий стол.', code='PATH_TOO_LONG')
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

    def badging(self, apk):
        """Manifest facts only (aapt2 reads the file; nothing in it runs). Signatures are checked separately."""
        self.ensure()
        return parse_badging(run([self.aapt, 'dump', 'badging', apk], timeout=120))

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

    def uids(self):
        s = self.shell('pm', 'list', 'packages', '-U', check=False)
        return {p: int(u) for p, u in re.findall(r'^package:([\w.]+) uid:(\d+)', s, re.M)}

    def home(self):
        s = self.shell('cmd', 'package', 'resolve-activity', '--brief', '-a', 'android.intent.action.MAIN',
                       '-c', 'android.intent.category.HOME', check=False)
        found = re.findall(r'^\s*([A-Za-z][\w.]*/[\w.$]+)\s*$', s, re.M)
        return found[-1] if found else ''

    def installer(self, pkg):
        s = self.shell('dumpsys', 'package', pkg, check=False)
        m = re.search(r'installerPackageName=([\w.]+)', s)
        return m[1] if m else ''

    def vpn(self, uid_map=None):
        dump = self.shell('dumpsys', 'connectivity', check=False, timeout=60)
        return vpn_routing(parse_vpn(dump), self.uids() if uid_map is None else uid_map)

    def interfaces(self):
        """IPv4 per interface; tells whether ADB rides on Wi-Fi (forgetting that network drops it)."""
        s = self.shell('ip', '-o', '-4', 'addr', check=False)
        return {ip: name for name, ip in re.findall(r'^\d+:\s+(\w+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)', s, re.M)}

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

    def identity(self, props=None):
        props = props or {k:self.prop(k) for k in ('ro.serialno','ro.build.fingerprint','ro.product.model')}
        android_id = self.shell('settings','get','secure','android_id').strip()
        if android_id in ('', 'null') and props['ro.serialno'] in ('','unknown','null'):
            raise Problem('Нет устойчивого идентификатора устройства. Изменения запрещены.', code='DEVICE_IDENTITY')
        return hashlib.sha256(('\n'.join([props['ro.serialno'], props['ro.build.fingerprint'], props['ro.product.model'], android_id])).encode()).hexdigest()

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
        identity = self.identity(props)
        uid_map = self.uids()
        try:
            vpn = self.vpn(uid_map)
        except Problem as exc:
            vpn = {'active': None, 'scope': 'unknown', 'ok': None, 'message': 'Состояние VPN не прочитано: ' + str(exc)}
        measures = self.measures()
        report = {'address': self.address, 'properties': props, 'model': props['ro.product.model'],
                  'manufacturer': props['ro.product.manufacturer'],
                  'android': props['ro.build.version.release'], 'api': api, 'abis': abi.split(','),
                  'identity': identity, 'identity_kind': 'android-id+serial', 'device_key': device_key(props),
                  'packages': packages, 'versions': versions, 'home': self.home(), 'vpn': vpn, 'uids': uid_map,
                  'interfaces': self.interfaces(),
                  'free_bytes': self.free_space(), 'ram_available_kb': ram_available(measures.get('memory', '')),
                  'ram_total_kb': ram_total(measures.get('memory', '')), 'before': measures}
        report['recommendations'] = self.recommendations(packages, vpn)
        if report['free_bytes'] < 600 * 1048576:
            report['recommendations']['warnings'].append(
                f"Мало места во внутренней памяти: {report['free_bytes'] // 1048576} МБ. Большие APK могут не установиться: сначала посмотрите раздел «Приложения и очистка» (кэш и неиспользуемые сторонние приложения).")
        return report

    @staticmethod
    def recommendations(packages, vpn=None):
        candidates = [p for p in packages if any(w in p.lower() for w in ['telemetry', 'analytics', 'advert', 'demo'])]
        warnings = []
        if OLD_SMARTTUBE in packages:
            warnings.append('Найден старый SmartTube (' + OLD_SMARTTUBE + '), подписанный прежним ключом разработчика, который был скомпрометирован в декабре 2025. Менеджер его не трогает и не добавляет в избранное/VPN. Проверьте новый org.smarttube.stable и удалите старый осознанно в разделе «Приложения».')
        if vpn and vpn.get('ok') is False:
            warnings.append(vpn['message'] + ' Остановите VPN на ТВ и настройте белый список до повторного включения.')
        active_vpn_apps = [VPN_APPS[p] for p in VPN_APPS if p in packages]
        if len(active_vpn_apps) > 1:
            warnings.append('Установлены ' + ' и '.join(active_vpn_apps) + '. Android держит только один активный VPN: включайте одно приложение для SmartTube/YouTube.')
        return {'unverified_candidates': candidates, 'warnings': warnings,
                'note': 'Совпадение имени — не доказательство ненужности. Ничего не отключено. Каждый пакет требует анализа служб, разрешений, зависимостей и отдельного согласия.'}

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


def ram_available(meminfo):
    """MemAvailable in kB: free RAM, unrelated to free /data storage."""
    m = re.search(r'^MemAvailable:\s+(\d+) kB', meminfo or '', re.M)
    return int(m[1]) if m else None


def ram_total(meminfo):
    m = re.search(r'^MemTotal:\s+(\d+) kB', meminfo or '', re.M)
    return int(m[1]) if m else None


def device_key(props):
    """Stable per-device key: serial and model, not IP and not the build (an OTA must not hide the source)."""
    serial = props.get('ro.serialno', '').strip()
    if not serial or serial in ('unknown', 'null'):
        return ''
    return hashlib.sha256(('serial:' + serial + '|model:' + props.get('ro.product.model', '')).encode()).hexdigest()


def parse_uid_ranges(text):
    """'<{0-10012, 10014-20012}>' -> [(0, 10012), (10014, 20012)]."""
    return [(int(a), int(b or a)) for a, b in re.findall(r'(\d+)(?:-(\d+))?', text)]


def parse_vpn(dump):
    """Active VPN networks from `dumpsys connectivity`. Read-only; Android 9 to 14 formats."""
    result = []
    for line in dump.splitlines():
        if 'NetworkAgentInfo{' not in line:
            continue
        transports = re.search(r'Transports: ([A-Z|]+)', line)
        if not transports or 'VPN' not in transports[1].split('|'):
            continue
        uids = re.search(r'Uids: <\{([^}]*)\}>', line)
        owner = re.search(r'OwnerUid: (\d+)', line)
        session = re.search(r'sessionId=([^,}]+)', line)
        result.append({'uid_ranges': parse_uid_ranges(uids[1]) if uids else None,
                       'owner_uid': int(owner[1]) if owner else None,
                       'session': session[1].strip() if session else '',
                       'interface': (re.search(r'InterfaceName: (\S+)', line) or [None, ''])[1]})
    return result


def vpn_routing(vpns, uid_map, allowed=ALLOWED_YOUTUBE):
    """Which known apps a VPN actually covers. A per-app tunnel lists only allowed apps' UIDs."""
    if not vpns:
        return {'active': False, 'scope': 'none', 'ok': None,
                'message': 'Активного VPN нет. Маршрутизацию можно проверить только после того, как вы включите VPN на ТВ.'}
    vpn = vpns[0]
    owner = next((p for p, u in uid_map.items() if u == vpn['owner_uid']), None) if vpn['owner_uid'] is not None else None
    name = VPN_APPS.get(owner, owner or vpn['session'] or 'неизвестное приложение')
    if vpn['uid_ranges'] is None:
        return {'active': True, 'owner': owner, 'name': name, 'scope': 'unknown', 'ok': None,
                'message': f'Активен VPN ({name}), но Android не показал список UID. Маршрутизация не подтверждена.'}
    def covered(uid):
        return any(a <= uid <= b for a, b in vpn['uid_ranges'])
    size = sum(b - a + 1 for a, b in vpn['uid_ranges'])
    inside = sorted(p for p, u in uid_map.items() if covered(u))
    local = sorted(LOCAL_APPS.get(p, p) for p in inside if p in LOCAL_APPS)
    extra = [p for p in inside if p not in allowed and p not in VPN_APPS]
    chosen = [p for p in inside if p in allowed]
    if size > 50000 or covered(1000):
        scope, ok = 'global', False
        message = f'Активен глобальный VPN ({name}): через туннель идут все приложения' + (', включая ' + ', '.join(local) if local else '') + '. Это не соответствует требованию «только SmartTube/YouTube».'
    elif extra:
        scope, ok = 'per-app', False
        message = f'VPN ({name}) охватывает лишние приложения: ' + ', '.join(LOCAL_APPS.get(p, p) for p in extra[:12]) + '.'
    elif not chosen:
        scope, ok = 'per-app', False
        message = f'VPN ({name}) активен, но SmartTube/YouTube в нём нет.'
    else:
        scope, ok = 'per-app', True
        message = f'VPN ({name}) охватывает только: ' + ', '.join(chosen) + '. Медиаплееры, AirCast и остальной трафик идут обычным путём. Воспроизведение проверьте на ТВ.'
    return {'active': True, 'owner': owner, 'name': name, 'scope': scope, 'ok': ok, 'inside': chosen,
            'local_inside': local, 'extra': extra[:50], 'message': message}


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
    bundled = app.get('bundled_file', 'FreeMyTV.apk')
    if bundled not in ('FreeMyTV.apk', 'FreeMyTV-profiles.apk'):
        raise Problem('Неизвестное имя локального APK.', code='APK_CATALOG')
    local = ROOT / 'payload' / bundled
    if key == 'freemytv' and local.exists() and digest(local) == variant['sha256']:
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
            store = app.get('store', {})
            if store and old['certificates'] == store.get('certificates') and device.installer(info['package']) == store.get('installer'):
                # The developer's own Google Play edition: official, re-signed by Play, updated by Play.
                return {'status': 'done', 'already_installed': True, 'package': info['package'], 'version': old['version'],
                        'certificate': old['certificates'][0], 'channel': 'google-play',
                        'message': 'Установлена официальная версия из Google Play (подпись магазина). Она обновляется через Google Play; APK с сайта поверх неё не ставится.'}
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
        if key == 'incy':
            with opener.open('https://raw.githubusercontent.com/INCY-DEV/incy-platforms/main/RELEASE.json', timeout=20) as r:
                manifest = json.loads(r.read(512 * 1024))
            if manifest.get('android', {}).get('version') != app['android_version'] or manifest['android'].get('download') != variant['url']:
                raise Problem('Официальная Android-версия INCY изменилась. Требуется обновить проверенный каталог.', code='INCY_RELEASE_CHANGED')
            req = urllib.request.Request('https://api.github.com/repos/INCY-DEV/incy-platforms/releases/tags/' + app['release'], headers={'User-Agent': 'XiaomiTVManager/1.1'})
            with opener.open(req, timeout=20) as r:
                release = json.loads(r.read(2 * 1024 * 1024))
            asset = next((a for a in release.get('assets', []) if a.get('browser_download_url') == variant['url']), None)
            if not asset or asset.get('digest') != 'sha256:' + variant['sha256'] or release.get('draft') or release.get('prerelease'):
                raise Problem('Официальный digest INCY не подтвердился; установка остановлена.', code='INCY_SOURCE')
        elif key in ('smarttube', 'byedpi'):
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
