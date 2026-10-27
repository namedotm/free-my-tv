"""Your own APKs: streamed upload or HTTPS download, offline analysis, per-device plan, install and verify.

Nothing in an uploaded file runs on Windows: aapt2 only reads its manifest and apksigner only checks its signature.
Files live in data/uploads/<id>/ under names the manager chooses; the user's file name is display text only.
"""
from __future__ import annotations
import base64
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from engine import Problem, save_json, read_json, digest, run, certs, parse_badging, command_problem

MAX_BYTES = 1024 * 1024 * 1024          # one file, upload or download
MAX_PARTS = 60                          # APKs inside one .apks/.xapk
MAX_ITEMS = 30                          # remembered uploads; older ones are removed
ICON_BYTES = 400 * 1024
EXTENSIONS = {'.apk', '.apks', '.xapk', '.apkm'}
PLAN_SECONDS = 180
STATES = {'receiving', 'downloading', 'analyzing', 'ready', 'error', 'cancelled'}
DENSITIES = {'ldpi': 120, 'mdpi': 160, 'tvdpi': 213, 'hdpi': 240, 'xhdpi': 320, 'xxhdpi': 480, 'xxxhdpi': 640}
ABI_SPLITS = {'armeabi_v7a': 'armeabi-v7a', 'arm64_v8a': 'arm64-v8a', 'armeabi': 'armeabi', 'x86': 'x86', 'x86_64': 'x86_64'}
PROTECTED_PACKAGES = {'android', 'com.android.vending', 'com.google.android.gms', 'com.google.android.gsf',
                      'com.android.systemui', 'com.android.tv.settings', 'com.google.android.tungsten.setupwraith',
                      'com.google.android.apps.mediashell', 'com.smartdevice.tv.aircast'}


def safe_name(name):
    """Display/download name only: no directories, no reserved characters, a known extension."""
    name = urllib.parse.unquote(str(name or ''))[-200:]
    name = re.split(r'[\\/]', name)[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]+', '_', name).strip(' .') or 'file.apk'
    stem, dot, ext = name.rpartition('.')
    ext = ('.' + ext.lower()) if dot else ''
    if ext not in EXTENSIONS:
        raise Problem('Поддерживаются файлы .apk, .apks и .xapk. Другие файлы менеджер не принимает.', code='APK_EXTENSION')
    stem = re.sub(r'\s+', ' ', stem)[:100] or 'file'
    if stem.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(?i)(COM|LPT)\d', stem):
        stem = '_' + stem
    return stem + ext


def check_public_url(url):
    """HTTPS to a public internet host on port 443 only: the manager is not a proxy into the home network."""
    if not isinstance(url, str) or len(url) > 2048:
        raise Problem('Неверная ссылка.', code='URL')
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme != 'https':
        raise Problem('Нужна ссылка HTTPS. HTTP и другие схемы не используются.', code='URL_SCHEME')
    if u.username or u.password or not u.hostname or u.port not in (None, 443):
        raise Problem('Ссылка не должна содержать логин, пароль или нестандартный порт.', code='URL')
    host = u.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.local', '.lan', '.home', '.internal', '.localhost', '.arpa')):
        raise Problem('Ссылки на устройства домашней сети не принимаются.', code='URL_PRIVATE')
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        raise Problem('Не удалось найти сервер по ссылке. Проверьте адрес и интернет.', code='URL_DNS') from None
    for a in addresses:
        ip = ipaddress.ip_address(a.split('%')[0])
        if not ip.is_global:
            raise Problem('Ссылка ведёт в локальную или служебную сеть. Такие загрузки запрещены.', code='URL_PRIVATE')
    return u.geturl()


class PublicRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 6

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def split_kind(name):
    if not name:
        return 'base'
    tail = name.split('.')[-1]
    if name.startswith('config.'):
        if tail in ABI_SPLITS:
            return 'abi'
        if tail in DENSITIES or tail == 'nodpi':
            return 'density'
        if re.fullmatch(r'[a-z]{2,3}', tail):
            return 'language'
        return 'config'
    return 'feature'


def choose_parts(parts, abis, density=None):
    """Base, every feature and language split, the best ABI split and the closest density split."""
    chosen, reasons = [], []
    by_kind = {}
    for p in parts:
        by_kind.setdefault(split_kind(p['split_name']), []).append(p)
    chosen += by_kind.get('base', []) + by_kind.get('feature', []) + by_kind.get('language', []) + by_kind.get('config', [])
    abi_parts = {ABI_SPLITS[p['split_name'].split('.')[-1]]: p for p in by_kind.get('abi', [])}
    if abi_parts:
        match = next((abi_parts[a] for a in abis if a in abi_parts), None)
        if not match:
            raise Problem('В комплекте нет части для архитектуры этого ТВ (' + ', '.join(abis) + ').', code='APK_NO_ABI')
        chosen.append(match)
    dens = {p['split_name'].split('.')[-1]: p for p in by_kind.get('density', [])}
    if dens:
        target = density or 320
        pick = min((d for d in dens if d in DENSITIES), key=lambda d: (DENSITIES[d] < target, abs(DENSITIES[d] - target)), default=None)
        chosen.append(dens[pick] if pick else next(iter(dens.values())))
        reasons.append('Часть экрана: ' + (pick or 'nodpi'))
    return chosen, reasons


class Intake:
    def __init__(self, data, tools, catalog):
        self.data = Path(data)
        self.root = self.data / 'uploads'
        self.root.mkdir(parents=True, exist_ok=True)
        self.tools = tools
        self.catalog = catalog
        self.lock = threading.Lock()
        self.items = {}
        self.cancel = set()
        self.plans = {}
        self.jobs = {}
        self.cleanup()

    # ---- storage -------------------------------------------------------------------------------------------
    def cleanup(self):
        """Drop unfinished transfers from an earlier run and keep the newest MAX_ITEMS uploads."""
        found = []
        for folder in self.root.iterdir():
            if not folder.is_dir() or not re.fullmatch(r'[0-9a-f]{16}', folder.name):
                continue
            meta = read_json(folder / 'meta.json', None)
            if not meta or meta.get('status') in ('receiving', 'downloading'):
                shutil.rmtree(folder, ignore_errors=True)
                continue
            if meta.get('status') == 'analyzing':
                meta.update(status='error', error='Анализ прерван закрытием менеджера. Загрузите файл заново.')
            found.append(meta)
        found.sort(key=lambda m: m.get('time', 0), reverse=True)
        for meta in found[MAX_ITEMS:]:
            shutil.rmtree(self.root / meta['id'], ignore_errors=True)
        self.items = {m['id']: m for m in found[:MAX_ITEMS]}

    def folder(self, item_id):
        if not isinstance(item_id, str) or not re.fullmatch(r'[0-9a-f]{16}', item_id):
            raise Problem('Неизвестный файл.', code='APK_ID')
        path = (self.root / item_id).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_dir():
            raise Problem('Файл уже удалён.', code='APK_ID')
        return path

    def set(self, item_id, **values):
        with self.lock:
            item = self.items.setdefault(item_id, {'id': item_id})
            item.update(values)
            meta = dict(item)
        if values.get('status') not in ('receiving', 'downloading') or 'size' in values:
            save_json(self.root / item_id / 'meta.json', meta)

    def snapshot(self):
        """Polled every second: icons are left out (the page asks for each one once)."""
        with self.lock:
            items = json.loads(json.dumps(sorted(self.items.values(), key=lambda m: m.get('time', 0), reverse=True)))
            jobs = json.loads(json.dumps(self.jobs))
        for item in items:
            if item.get('analysis'):
                item['analysis']['has_icon'] = bool(item['analysis'].pop('icon', None))
        return {'items': items, 'jobs': jobs}

    def icon_of(self, item_id):
        self.folder(item_id)
        with self.lock:
            return ((self.items.get(item_id) or {}).get('analysis') or {}).get('icon')

    def new_item(self, name, origin):
        with self.lock:
            active = sum(m.get('status') in ('receiving', 'downloading', 'analyzing') for m in self.items.values())
            if active >= 2:
                raise Problem('Дождитесь окончания текущих загрузок (одновременно не больше двух).', code='APK_BUSY')
        item_id = secrets.token_hex(8)
        (self.root / item_id).mkdir()
        self.set(item_id, name=safe_name(name), origin=origin, status='receiving', received=0, total=None,
                 time=time.time(), error='', analysis=None)
        return item_id

    def stored_file(self, item_id):
        folder = self.folder(item_id)
        files = [p for p in folder.iterdir() if p.name.startswith('original.')]
        if len(files) != 1:
            raise Problem('Файл не найден; загрузите его заново.', code='APK_ID')
        return files[0]

    # ---- receiving -----------------------------------------------------------------------------------------
    def receive(self, stream, length, name):
        """Stream the request body to disk in 1 MB pieces; never holds the APK in memory or in JSON."""
        if not 0 < length <= MAX_BYTES:
            raise Problem(f'Размер файла должен быть от 1 байта до {MAX_BYTES // 1048576} МБ.', code='APK_SIZE')
        item_id = self.new_item(name, 'file')
        ext = Path(self.items[item_id]['name']).suffix
        part = self.root / item_id / 'upload.part'
        try:
            self.copy(item_id, stream, part, length, 'receiving')
        except Exception as exc:
            self.fail(item_id, exc if isinstance(exc, Problem) else Problem('Передача файла прервана.', code='APK_TRUNCATED'))
            raise
        part.replace(self.root / item_id / ('original' + ext))
        self.analyze_async(item_id)
        return item_id

    def copy(self, item_id, source, part, total, state):
        received, notified = 0, 0.0
        self.set(item_id, status=state, received=0, total=total)
        with part.open('wb') as out:
            while total is None or received < total:
                chunk = source.read(min(1024 * 1024, (total - received) if total else 1024 * 1024))
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_BYTES:
                    raise Problem('Файл больше допустимого размера; загрузка остановлена.', code='APK_SIZE')
                out.write(chunk)
                if item_id in self.cancel:
                    raise Problem('Загрузка отменена пользователем.', code='APK_CANCELLED')
                if time.monotonic() - notified > .25:
                    with self.lock:
                        self.items[item_id].update(received=received)
                    notified = time.monotonic()
        if total is not None and received != total:
            raise Problem('Передача оборвалась до конца файла. Повторите загрузку.', code='APK_TRUNCATED')
        self.set(item_id, received=received, size=received)

    def fail(self, item_id, exc):
        cancelled = isinstance(exc, Problem) and exc.code == 'APK_CANCELLED'
        for p in (self.root / item_id).glob('*.part'):
            p.unlink(missing_ok=True)
        self.set(item_id, status='cancelled' if cancelled else 'error',
                 error=str(exc) if isinstance(exc, Problem) else 'Непредвиденная ошибка при обработке файла.',
                 details=getattr(exc, 'details', ''), code=getattr(exc, 'code', ''))
        self.cancel.discard(item_id)

    def start_download(self, url):
        url = check_public_url(url)
        name = Path(urllib.parse.urlsplit(url).path).name or 'download.apk'
        if Path(name).suffix.lower() not in EXTENSIONS:
            name += '.apk'
        item_id = self.new_item(name, 'url')
        self.set(item_id, status='downloading', source_host=urllib.parse.urlsplit(url).hostname)
        threading.Thread(target=self.download, args=(item_id, url), daemon=True).start()
        return item_id

    def download(self, item_id, url):
        part = self.root / item_id / 'download.part'
        try:
            opener = urllib.request.build_opener(PublicRedirects())
            request = urllib.request.Request(url, headers={'User-Agent': 'XiaomiTVManager/2.0'})
            with opener.open(request, timeout=60) as response:
                final = response.geturl()
                check_public_url(final)
                length = response.headers.get('Content-Length')
                total = int(length) if length and length.isdigit() else None
                if total and total > MAX_BYTES:
                    raise Problem('Файл по ссылке больше допустимого размера.', code='APK_SIZE')
                disposition = response.headers.get('Content-Disposition', '')
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
                if m:
                    try:
                        self.set(item_id, name=safe_name(m[1]))
                    except Problem:
                        pass
                self.set(item_id, source_host=urllib.parse.urlsplit(final).hostname)
                self.copy(item_id, response, part, total, 'downloading')
            ext = Path(self.items[item_id]['name']).suffix
            part.replace(self.root / item_id / ('original' + ext))
            self.analyze(item_id)
        except Problem as exc:
            self.fail(item_id, exc)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.fail(item_id, Problem('Не удалось скачать файл по HTTPS. Проверьте ссылку и интернет; TLS-проверка не отключается.',
                                       code='DOWNLOAD_FAILED', details=str(exc)))

    def cancel_item(self, item_id):
        self.folder(item_id)
        with self.lock:
            state = self.items.get(item_id, {}).get('status')
        if state not in ('receiving', 'downloading'):
            raise Problem('Отменить можно только незавершённую загрузку.', code='APK_STATE')
        self.cancel.add(item_id)
        return 'Отмена запрошена; временный файл будет удалён.'

    def delete(self, item_id):
        folder = self.folder(item_id)
        with self.lock:
            if self.items.get(item_id, {}).get('status') in ('receiving', 'downloading', 'analyzing'):
                raise Problem('Сначала отмените загрузку или дождитесь анализа.', code='APK_STATE')
            if any(j.get('item') == item_id and j.get('running') for j in self.jobs.values()):
                raise Problem('Файл используется текущей установкой.', code='APK_STATE')
            self.items.pop(item_id, None)
        shutil.rmtree(folder, ignore_errors=True)
        return 'Файл удалён из менеджера. Установленные на ТВ приложения не затронуты.'

    # ---- analysis ------------------------------------------------------------------------------------------
    def analyze_async(self, item_id):
        threading.Thread(target=self.analyze, args=(item_id,), daemon=True).start()

    def analyze(self, item_id):
        try:
            self.set(item_id, status='analyzing', stage='Проверка формата')
            path = self.stored_file(item_id)
            sha = digest(path)
            self.set(item_id, sha256=sha, size=path.stat().st_size, stage='Подготовка инструментов проверки')
            self.tools.ensure(progress=lambda stage, msg, **meta: self.set(item_id, stage=msg))
            analysis = self.inspect_file(item_id, path)
            analysis['sha256'] = sha
            analysis['provenance'] = self.provenance(analysis, sha)
            analysis['warnings'] = self.warnings(analysis)
            with self.lock:
                origin = self.items[item_id].get('origin')
                host = self.items[item_id].get('source_host')
            if origin == 'url':
                analysis['warnings'].insert(0, f'Скачан по HTTPS с {host}. TLS подтверждает только сервер, а не автора приложения.')
            self.set(item_id, status='ready', stage='', analysis=analysis)
        except Problem as exc:
            self.fail(item_id, exc)
        except (OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
            self.fail(item_id, Problem('Файл повреждён или это не APK.', code='APK_CORRUPT', details=str(exc)))

    def inspect_file(self, item_id, path):
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise Problem('Файл повреждён или это не APK (не ZIP-архив).', code='APK_CORRUPT') from None
        with archive:
            names = archive.namelist()
            if archive.testzip() is not None:
                raise Problem('Архив APK повреждён (не совпала контрольная сумма записи).', code='APK_CORRUPT')
            inner = [n for n in names if n.lower().endswith('.apk') and '/' not in n.strip('/')]
            if 'AndroidManifest.xml' in names:
                kind = 'apk'
            elif inner and 'manifest.json' in names:
                kind = 'xapk'
            elif inner and ('info.json' in names) and any(i.flag_bits & 1 for i in archive.infolist()):
                raise Problem('Это зашифрованный пакет APKM (APKMirror). Его можно установить только их собственным установщиком; менеджер не расшифровывает такие файлы.', code='APK_APKM')
            elif inner:
                kind = 'apks'
            else:
                raise Problem('В файле нет AndroidManifest.xml или вложенных APK: это не приложение Android.', code='APK_CORRUPT')
            obb = [n for n in names if n.lower().startswith('android/obb/') and not n.endswith('/')]
            if kind == 'apk':
                info = self.tools.badging(path)
                if info['split']:
                    raise Problem(f"Это отдельная часть (split «{info['split_name']}») приложения {info['package']}, а не полное приложение. Загрузите полный APK или комплект .apks/.xapk.", code='APK_SPLIT')
                parts = [{**info, 'file': path.name, 'size': path.stat().st_size, 'kind': 'base'}]
                icon = self.icon(archive, info)
            else:
                if len(inner) > MAX_PARTS:
                    raise Problem('Слишком много частей в комплекте.', code='APK_PARTS')
                folder = self.root / item_id / 'parts'
                shutil.rmtree(folder, ignore_errors=True)
                folder.mkdir()
                parts, total = [], 0
                for index, name in enumerate(sorted(inner)):
                    member = archive.getinfo(name)
                    total += member.file_size
                    if total > MAX_BYTES:
                        raise Problem('Распакованный комплект больше допустимого размера.', code='APK_SIZE')
                    target = folder / f'part-{index:02d}.apk'
                    with archive.open(member) as src, target.open('wb') as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    self.set(item_id, stage=f'Проверка части {index + 1}/{len(inner)}')
                    info = self.tools.badging(target)
                    parts.append({**info, 'file': 'parts/' + target.name, 'entry': name[-120:], 'size': member.file_size,
                                  'kind': split_kind(info['split_name'])})
                bases = [p for p in parts if not p['split']]
                if len(bases) != 1:
                    raise Problem('В комплекте не найден ровно один базовый APK.', code='APK_BASE_MISSING')
                base = bases[0]
                for p in parts:
                    if p['package'] != base['package'] or p['version'] != base['version']:
                        raise Problem('Части комплекта относятся к разным приложениям или версиям.', code='APK_SPLIT_VERSION')
                with zipfile.ZipFile(self.root / item_id / base['file']) as base_zip:
                    icon = self.icon(base_zip, base)
                info = base
            for p in parts:
                self.set(item_id, stage='Проверка подписи ' + (p['split_name'] or 'базового APK'))
                out = run([self.tools.java, '-jar', self.tools.jar, 'verify', '--min-sdk-version', str(info['min_api'] or 24),
                           '--print-certs', '-v', self.root / item_id / p['file']], timeout=180)
                p['certificates'] = certs(out)
                p['schemes'] = sorted(set(re.findall(r'Verified using (v[\d.]+) scheme[^:]*: true', out)))
                p['debug_cert'] = bool(re.search(r'certificate DN: .*CN=Android Debug', out))
            if any(p['certificates'] != parts[0]['certificates'] for p in parts):
                raise Problem('Части комплекта подписаны разными сертификатами. Установка запрещена.', code='APK_CERT_MIXED')
        return {'kind': kind, 'package': info['package'], 'label': info.get('label', ''), 'version': info['version'],
                'version_name': info.get('version_name', ''), 'min_api': info['min_api'], 'target_api': info.get('target_api'),
                'abis': sorted({a for p in parts for a in p['abis']}), 'base_abis': info['abis'],
                'certificates': parts[0]['certificates'], 'schemes': parts[0]['schemes'],
                'debug_cert': parts[0]['debug_cert'], 'test_only': info.get('test_only', False), 'leanback': info.get('leanback', False),
                'parts': [{k: p[k] for k in ('file', 'split_name', 'kind', 'size', 'abis')} for p in parts],
                'obb_files': len(obb), 'icon': icon}

    @staticmethod
    def icon(archive, info):
        names = set(archive.namelist())
        candidates = [p for p in info.get('icons', []) if p.lower().endswith(('.png', '.webp'))]
        for p in info.get('icons', []):
            stem = Path(p).stem
            candidates += sorted((n for n in names if re.fullmatch(r'res/(?:mipmap|drawable)-[\w-]+/' + re.escape(stem) + r'\.(?:png|webp)', n)),
                                 key=lambda n: -archive.getinfo(n).file_size)
        for name in candidates:
            if name in names and 0 < archive.getinfo(name).file_size <= ICON_BYTES:
                data = archive.read(name)
                mime = 'image/png' if data[:8] == b'\x89PNG\r\n\x1a\n' else 'image/webp' if data[8:12] == b'WEBP' else None
                if mime:
                    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode()
        return None

    def provenance(self, analysis, sha):
        for key, app in self.catalog['apps'].items():
            if app.get('package') != analysis['package']:
                continue
            if analysis['certificates'] == [app.get('certificate')]:
                exact = any(v.get('sha256') == sha for v in app.get('variants', {}).values())
                return {'kind': 'catalog' if exact else 'catalog_signer', 'catalog_key': key, 'label': app['label'],
                        'message': 'Файл побайтно совпадает с проверенным каталогом.' if exact else
                        'Подпись совпадает с проверенным издателем из каталога, но этот файл (версия/сборка) в каталоге не закреплён.'}
            store = app.get('store', {})
            if analysis['certificates'] == store.get('certificates'):
                return {'kind': 'catalog_signer', 'catalog_key': key, 'label': app['label'],
                        'message': 'Подпись совпадает с официальной версией из магазина, указанной в каталоге.'}
            return {'kind': 'catalog_mismatch', 'catalog_key': key, 'label': app['label'],
                    'message': 'Пакет есть в проверенном каталоге, но подписан ДРУГИМ сертификатом. Это может быть подделка или неофициальная сборка.'}
        return {'kind': 'unknown', 'message': 'Издатель неизвестен менеджеру. Действительная подпись подтверждает только целостность файла, а не безопасность приложения.'}

    @staticmethod
    def warnings(a):
        w = []
        if a['provenance']['kind'] == 'catalog_mismatch':
            w.append(a['provenance']['message'])
        if a['debug_cert']:
            w.append('Приложение подписано отладочным сертификатом (Android Debug).')
        if a['test_only']:
            w.append('Сборка помечена как отладочная/тестовая; Android может отказать в установке без специальных флагов, менеджер их не добавляет.')
        if a['target_api'] is not None and a['target_api'] < 23:
            w.append(f"targetSdk {a['target_api']}: Android 14 (API 34) запрещает установку таких приложений.")
        if not a['leanback']:
            w.append('В APK не найдено TV-значка запуска (LEANBACK). Приложение может не появиться в списке приложений ТВ.')
        if a['obb_files']:
            w.append(f"В XAPK есть {a['obb_files']} файл(ов) OBB с данными. Перенос OBB менеджер не выполняет: установка такого комплекта запрещена, чтобы не получить неработающее приложение.")
        if a['package'] in PROTECTED_PACKAGES:
            w.append('Это критический системный пакет. Менеджер не устанавливает его поверх системы.')
        return w

    # ---- per-device plan -----------------------------------------------------------------------------------
    def item_analysis(self, item_id):
        with self.lock:
            item = json.loads(json.dumps(self.items.get(item_id) or {}))
        if item.get('status') != 'ready' or not item.get('analysis'):
            raise Problem('Файл ещё не проверен или проверка завершилась ошибкой.', code='APK_STATE')
        return item

    def plan_device(self, device, report, item, folder, protected_source, progress):
        """Read-only per-device checks. Pulls the installed APK as a verified backup before any update."""
        a = item['analysis']
        row = {'address': device.address, 'model': report['model'], 'android': report['android'], 'api': report['api'],
               'abis': report['abis'], 'free_bytes': report['free_bytes'], 'identity': report['identity'],
               'reasons': [], 'warnings': [], 'files': [], 'old': None, 'status': 'ready'}
        def block(reason):
            row['status'] = 'blocked'
            row['reasons'].append(reason)
        if a['package'] in PROTECTED_PACKAGES:
            block('Критический системный пакет: установка через менеджер запрещена.')
        if a['obb_files']:
            block('Комплект с OBB-данными не поддерживается.')
        if a['provenance']['kind'] == 'catalog_mismatch':
            block('Подпись не совпадает с проверенным каталогом для этого пакета.')
        if a['package'] == 'io.freemytv' and protected_source:
            block('Проектор-источник: его работающий лаунчер Free My TV не обновляется.')
        if a['min_api'] and a['min_api'] > report['api']:
            block(f"Нужен Android API {a['min_api']} или новее, на ТВ API {report['api']}.")
        if a['target_api'] is not None and a['target_api'] < 23 and report['api'] >= 34:
            block(f"Android {report['android']} не устанавливает приложения с targetSdk {a['target_api']}.")
        if a['base_abis'] and not set(a['base_abis']) & set(report['abis']):
            block('Архитектура APK (' + ', '.join(a['base_abis']) + ') не подходит ТВ (' + ', '.join(report['abis']) + ').')
        if a['kind'] == 'apk':
            row['files'] = [a['parts'][0]['file']]
        elif row['status'] != 'blocked':
            try:
                density = int((re.search(r'(\d+)', device.shell('wm', 'density', check=False)) or [0, 320])[1])
                chosen, notes = choose_parts([{**p, 'split_name': p['split_name']} for p in a['parts']], report['abis'], density)
                row['files'] = [p['file'] for p in chosen]
                row['warnings'] += notes
            except Problem as exc:
                block(str(exc))
        size = sum(p['size'] for p in a['parts'] if p['file'] in row['files']) or item.get('size', 0)
        row['install_bytes'] = size
        if report['free_bytes'] < size + 64 * 1048576:
            block(f"Недостаточно места: свободно {report['free_bytes'] // 1048576} МБ, нужно не меньше {(size + 64 * 1048576) // 1048576} МБ.")
        elif report['free_bytes'] < size * 3 + 128 * 1048576:
            row['warnings'].append(f"Мало места ({report['free_bytes'] // 1048576} МБ). Android может отказать при оптимизации приложения; менеджер покажет точную ошибку.")
        if a['package'] in report['packages'] and row['status'] != 'blocked':
            progress('Сохранение и проверка установленной версии')
            old = device.installed(a['package'], Path(folder) / 'backup', report['api'])
            row['old'] = {'version': old['version'], 'certificates': old['certificates'], 'backup': str(Path(folder) / 'backup')}
            if old['certificates'] != a['certificates']:
                block('Подпись отличается от установленной версии. Android не обновит приложение; удалять старую версию и данные менеджер не будет.')
            elif old['version'] > a['version']:
                block(f"На ТВ более новая версия (code {old['version']} > {a['version']}). Понижение версии не выполняется.")
            elif old['version'] == a['version']:
                row['status'] = 'already'
                row['reasons'].append('Эта версия уже установлена; повторная установка не нужна.')
            else:
                row['status'] = 'update'
                row['reasons'].append(f"Обновление: code {old['version']} → {a['version']}. Копия прежней версии сохранена; данные приложения не удаляются.")
            if report.get('home', '').split('/')[0] == a['package']:
                row['warnings'].append('Это текущий главный экран ТВ: после обновления он перезапустится.')
        elif row['status'] != 'blocked':
            row['reasons'].append('Новая установка.')
        if row['status'] in ('ready', 'update') and a['provenance']['kind'] == 'unknown':
            row['warnings'].append('Неизвестный издатель: устанавливайте только то, чему доверяете. Android может показать предупреждение Play Protect — ответ даётся на экране ТВ.')
        return row

    def remember_plan(self, item, rows):
        key = secrets.token_urlsafe(24)
        with self.lock:
            self.plans = {k: v for k, v in self.plans.items() if v['expires'] > time.time()}
            self.plans[key] = {'expires': time.time() + PLAN_SECONDS, 'item': item['id'], 'sha256': item['analysis']['sha256'],
                               'rows': rows}
        return key

    def take_plan(self, key):
        with self.lock:
            plan = self.plans.pop(key, None) if isinstance(key, str) else None
        if not plan or plan['expires'] < time.time():
            raise Problem('План устарел (действует 3 минуты). Подготовьте его заново.', code='APK_PLAN')
        return plan

    def install_device(self, device, report, item, row, folder, progress):
        """Re-check, install the planned files, then verify the APK Android actually installed."""
        a = item['analysis']
        if report['identity'] != row['identity']:
            raise Problem('По этому адресу теперь отвечает другое устройство. Установка отменена.', code='DEVICE_CHANGED')
        current = None
        if a['package'] in report['packages']:
            text = device.shell('dumpsys', 'package', a['package'])
            m = re.search(r'versionCode=(\d+)', text)
            current = int(m[1]) if m else None
        if (row['old'] or {}).get('version') != current:
            raise Problem('Установленная версия изменилась после плана. Подготовьте план заново.', code='APK_PLAN')
        files = [self.root / item['id'] / f for f in row['files']]
        for f in files:
            if not f.resolve().is_relative_to((self.root / item['id']).resolve()) or not f.is_file():
                raise Problem('Файл установки не найден.', code='APK_ID')
        if digest(self.stored_file(item['id'])) != a['sha256']:
            raise Problem('Файл изменился после проверки. Загрузите и проверьте его заново.', code='APK_CHANGED')
        free = device.free_space()
        if free < row['install_bytes'] + 64 * 1048576:
            raise Problem(f'Недостаточно места: свободно {free // 1048576} МБ.', code='STORAGE')
        progress('install', 'Передача и установка. Если на ТВ появится запрос (Play Protect, разрешение) — ответьте на экране ТВ.')
        timeout = 180 + row['install_bytes'] // (256 * 1024)
        try:
            output = device.adb('install-multiple' if len(files) > 1 else 'install', '-r', *[str(f) for f in files], timeout=timeout, check=False)
        except Problem as exc:
            if exc.code == 'TIMEOUT':
                return {'status': 'manual', 'message': 'Android ждёт ответа на экране ТВ или установка идёт слишком долго. Проверьте экран ТВ и повторите проверку плана — повторная установка выполнится, только если версия ещё не установлена.'}
            raise
        if not re.search(r'^Success\s*$', output, re.M):
            raise command_problem(output, 'установка APK')
        progress('verify', 'Проверка реально установленной версии и подписи')
        actual = device.installed(a['package'], Path(folder) / 'verified', report['api'])
        if not actual or actual['version'] != a['version'] or actual['certificates'] != a['certificates']:
            raise Problem('ADB сообщил Success, но версия или подпись установленного пакета не подтвердились. Не удаляйте приложение; повторите проверку.', code='APK_VERIFY')
        return {'status': 'done', 'package': a['package'], 'version': a['version'], 'new_install': row['old'] is None,
                'message': ('Установлено' if row['old'] is None else 'Обновлено') + f" и проверено: {a['package']} code {a['version']}, подпись совпадает."}
