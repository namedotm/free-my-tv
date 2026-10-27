import copy
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e
import apk_intake as ai
import app as gui
import repair as r
import remote
import optimize
import media

ARTIFACT = e.ROOT.parent / 'artifacts' / 'FreeMyTV.apk'
REAL_TOOLS = os.name == 'nt' and (e.ROOT / 'runtime' / 'android' / 'android-14' / 'aapt2.exe').is_file()
CERT = 'c' * 64


def info(pkg='org.example.app', version=5, split='', abis=(), min_api=24, target=33):
    return {'package': pkg, 'version': version, 'min_api': None if split else min_api, 'split': bool(split), 'split_name': split,
            'abis': list(abis), 'version_name': '1.' + str(version), 'target_api': target, 'label': 'Пример', 'icons': [],
            'leanback': True, 'test_only': False}


class FakeTools:
    def __init__(self, data, infos=None):
        self.data = Path(data); self.adb = 'adb'; self.java = 'java'; self.jar = 'apksigner.jar'; self.infos = infos or {}
    def ensure(self, progress=None): pass
    def badging(self, path):
        with zipfile.ZipFile(path) as z:
            key = z.read('id.txt').decode() if 'id.txt' in z.namelist() else 'base'
        return copy.deepcopy(self.infos[key])
    def inspect(self, path, api=None, progress=None):
        return {**self.badging(path), 'certificates': [CERT]}


def fake_apk(path, key='base'):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('AndroidManifest.xml', b'binary')
        z.writestr('id.txt', key)
    return path


SIGNED = f'Verified using v2 scheme (APK Signature Scheme v2): true\nSigner #1 certificate DN: CN=Example\nSigner #1 certificate SHA-256 digest: {CERT}\n'


def intake(td, infos, catalog=None):
    return ai.Intake(td, FakeTools(td, infos), catalog or {'apps': {}})


def stored(it, td, data, name='app.apk'):
    item = it.new_item(name, 'file')
    (Path(td) / 'uploads' / item / ('original' + Path(name).suffix)).write_bytes(data)
    it.set(item, status='analyzing', size=len(data))
    return item


class NamesAndUrls(unittest.TestCase):
    def test_safe_name_strips_paths_and_checks_extension(self):
        self.assertEqual(ai.safe_name('..\\..\\Windows\\evil.apk'), 'evil.apk')
        self.assertEqual(ai.safe_name('a/b/c%2F..%2Fx.APK'), 'x.apk')
        self.assertEqual(ai.safe_name('CON.apk'), '_CON.apk')
        self.assertEqual(ai.safe_name('My: App?.xapk'), 'My_ App_.xapk')
        for bad in ('run.exe', 'x.apk.bat', 'noext', 'script.ps1'):
            with self.subTest(bad=bad), self.assertRaises(e.Problem):
                ai.safe_name(bad)

    def test_url_must_be_public_https(self):
        with patch('apk_intake.socket.getaddrinfo', return_value=[(0, 0, 0, '', ('140.82.112.3', 443))]):
            self.assertTrue(ai.check_public_url('https://github.com/x/app.apk').startswith('https://'))
        for bad in ('http://example.com/a.apk', 'ftp://x/a.apk', 'https://user:pw@example.com/a.apk', 'https://example.com:8443/a.apk',
                    'https://localhost/a.apk', 'https://nas.local/a.apk', 'file:///C:/a.apk'):
            with self.subTest(bad=bad), self.assertRaises(e.Problem):
                ai.check_public_url(bad)
        for addr in ('192.168.31.20', '127.0.0.1', '10.0.0.1', '169.254.1.1', '100.64.0.1'):
            with self.subTest(addr=addr), patch('apk_intake.socket.getaddrinfo', return_value=[(0, 0, 0, '', (addr, 443))]), self.assertRaises(e.Problem):
                ai.check_public_url('https://evil.example/a.apk')

    def test_redirect_into_home_network_is_refused(self):
        handler = ai.PublicRedirects()
        with patch('apk_intake.socket.getaddrinfo', return_value=[(0, 0, 0, '', ('192.168.28.60', 443))]), self.assertRaises(e.Problem):
            handler.redirect_request(None, None, 302, 'Found', {}, 'https://router.example/admin')
        with self.assertRaises(e.Problem):
            handler.redirect_request(None, None, 302, 'Found', {}, 'http://example.com/a.apk')


class Analysis(unittest.TestCase):
    def test_not_a_zip_and_no_manifest_are_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            it = intake(td, {})
            a = stored(it, td, b'MZ this is not an apk')
            it.analyze(a)
            self.assertEqual((it.items[a]['status'], it.items[a]['code']), ('error', 'APK_CORRUPT'))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr('readme.txt', 'x')
            b = stored(it, td, buf.getvalue())
            it.analyze(b)
            self.assertEqual(it.items[b]['code'], 'APK_CORRUPT')

    def test_single_split_is_rejected_with_explanation(self):
        with tempfile.TemporaryDirectory() as td:
            it = intake(td, {'base': info(split='config.armeabi_v7a')})
            path = fake_apk(Path(td) / 'x.apk')
            a = stored(it, td, path.read_bytes())
            it.analyze(a)
            self.assertEqual(it.items[a]['code'], 'APK_SPLIT')
            self.assertIn('config.armeabi_v7a', it.items[a]['error'])

    def test_apks_bundle_extracts_and_checks_every_part(self):
        with tempfile.TemporaryDirectory() as td:
            infos = {'base': info(), 'v7': info(split='config.armeabi_v7a', abis=['armeabi-v7a']),
                     'v8': info(split='config.arm64_v8a', abis=['arm64-v8a']), 'ru': info(split='config.ru'),
                     'xh': info(split='config.xhdpi'), 'hd': info(split='config.hdpi')}
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as bundle:
                for key in infos:
                    part = fake_apk(Path(td) / (key + '.apk'), key)
                    bundle.write(part, ('base' if key == 'base' else 'split_' + key) + '.apk')
                bundle.writestr('toc.pb', b'')
            it = intake(td, infos)
            a = stored(it, td, buf.getvalue(), 'game.apks')
            with patch('apk_intake.run', return_value=SIGNED):
                it.analyze(a)
            item = it.items[a]
            self.assertEqual(item['status'], 'ready', item.get('error'))
            self.assertEqual(item['analysis']['kind'], 'apks')
            self.assertEqual(len(item['analysis']['parts']), 6)
            chosen, _ = ai.choose_parts(item['analysis']['parts'], ['armeabi-v7a', 'armeabi'], 320)
            names = sorted(p['split_name'] for p in chosen)
            self.assertEqual(names, ['', 'config.armeabi_v7a', 'config.ru', 'config.xhdpi'])
            with self.assertRaises(e.Problem):
                ai.choose_parts(item['analysis']['parts'], ['x86'], 320)
            for p in (Path(td) / 'uploads' / a / 'parts').iterdir():
                self.assertRegex(p.name, r'^part-\d\d\.apk$')

    def test_mixed_signers_in_bundle_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            infos = {'base': info(), 'v7': info(split='config.armeabi_v7a', abis=['armeabi-v7a'])}
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as bundle:
                for key in infos:
                    bundle.write(fake_apk(Path(td) / (key + '.apk'), key), key + '.apk')
            it = intake(td, infos)
            a = stored(it, td, buf.getvalue(), 'x.apks')
            outputs = iter([SIGNED, SIGNED.replace(CERT, 'd' * 64)])
            with patch('apk_intake.run', side_effect=lambda *a, **k: next(outputs)):
                it.analyze(a)
            self.assertEqual(it.items[a]['code'], 'APK_CERT_MIXED')

    def test_catalog_provenance(self):
        catalog = {'apps': {'vlc': {'package': 'org.videolan.vlc', 'label': 'VLC', 'certificate': CERT,
                                    'variants': {'armeabi-v7a': {'sha256': 'f' * 64}}, 'store': {'certificates': ['e' * 64]}}}}
        it = intake(tempfile.gettempdir(), {}, catalog)
        self.assertEqual(it.provenance({'package': 'org.videolan.vlc', 'certificates': [CERT]}, 'f' * 64)['kind'], 'catalog')
        self.assertEqual(it.provenance({'package': 'org.videolan.vlc', 'certificates': [CERT]}, '0' * 64)['kind'], 'catalog_signer')
        self.assertEqual(it.provenance({'package': 'org.videolan.vlc', 'certificates': ['e' * 64]}, '0' * 64)['kind'], 'catalog_signer')
        self.assertEqual(it.provenance({'package': 'org.videolan.vlc', 'certificates': ['b' * 64]}, '0' * 64)['kind'], 'catalog_mismatch')
        self.assertEqual(it.provenance({'package': 'my.own.app', 'certificates': ['b' * 64]}, '0' * 64)['kind'], 'unknown')

    def test_cleanup_removes_unfinished_uploads(self):
        with tempfile.TemporaryDirectory() as td:
            it = intake(td, {})
            a = it.new_item('half.apk', 'file')
            (Path(td) / 'uploads' / a / 'upload.part').write_bytes(b'x')
            it2 = intake(td, {})
            self.assertNotIn(a, it2.items)
            self.assertFalse((Path(td) / 'uploads' / a).exists())

    @unittest.skipUnless(REAL_TOOLS and ARTIFACT.is_file(), 'needs runtime/ build tools and artifacts/FreeMyTV.apk')
    def test_real_apk_with_windows_tools(self):
        with tempfile.TemporaryDirectory() as td:
            tools = e.Tools(td)
            tools.home = Path(os.path.realpath(e.ROOT / 'runtime'))  # Java cannot start from paths over 260 characters
            catalog = e.read_json(e.ROOT / 'catalog.json')
            it = ai.Intake(td, tools, catalog)
            a = stored(it, td, ARTIFACT.read_bytes(), 'FreeMyTV.apk')
            it.analyze(a)
            item = it.items[a]
            self.assertEqual(item['status'], 'ready', item.get('details'))
            x = item['analysis']
            self.assertEqual((x['package'], x['version'], x['version_name']), ('io.freemytv', 3, '1.2'))
            self.assertEqual(x['sha256'], e.APK_HASH)
            self.assertEqual(x['certificates'], ['8da630acb15da56191213ac858449ef5ee5908332d52b8774d52ad11ea3ec99c'])
            self.assertEqual(x['kind'], 'apk')
            self.assertTrue(x['leanback'])
            self.assertTrue(x['icon'] is None or x['icon'].startswith('data:image/'))
            # Personal signature differs from nothing in the catalog for io.freemytv's profile build: same signer.
            self.assertIn(x['provenance']['kind'], ('catalog_signer', 'catalog'))


class FakeDevice:
    def __init__(self, installed=None, free=10 ** 9, identity='id-1', output='Success', current=None):
        self.address = '192.168.31.20:5555'; self.old = installed; self.free = free; self._identity = identity
        self.output = output; self.commands = []; self.current = current; self.after = None
    def shell(self, *args, **kw):
        self.commands.append(('shell',) + args)
        if args[:2] == ('wm', 'density'):
            return 'Physical density: 320'
        if args[:2] == ('dumpsys', 'package'):
            v = self.current if self.current is not None else (self.old or {}).get('version')
            return f'versionCode={v} minSdk=24' if v else ''
        return ''
    def adb(self, *args, **kw):
        self.commands.append(args)
        if isinstance(self.output, Exception):
            raise self.output
        return self.output
    def installed(self, pkg, folder, api, progress=None):
        return copy.deepcopy(self.after if self.after and 'verified' in str(folder) else self.old)
    def free_space(self):
        return self.free


def report(packages=(), api=34, abis=('armeabi-v7a', 'armeabi'), free=10 ** 9, identity='id-1', home=''):
    return {'model': 'MiTV-AFKR0', 'android': '14', 'api': api, 'abis': list(abis), 'free_bytes': free, 'identity': identity,
            'packages': list(packages), 'home': home}


def ready_item(td, it, **over):
    a = {'kind': 'apk', 'package': 'org.example.app', 'label': 'Пример', 'version': 5, 'version_name': '1.5', 'min_api': 24, 'target_api': 33,
         'abis': [], 'base_abis': [], 'certificates': [CERT], 'schemes': ['v2'], 'debug_cert': False, 'test_only': False, 'leanback': True,
         'parts': [{'file': 'original.apk', 'split_name': '', 'kind': 'base', 'size': 10 * 1048576, 'abis': []}], 'obb_files': 0, 'icon': None,
         'sha256': None, 'provenance': {'kind': 'unknown', 'message': ''}, 'warnings': []}
    a.update(over)
    item = it.new_item('app.apk', 'file')
    path = Path(td) / 'uploads' / item / 'original.apk'
    path.write_bytes(b'apk-bytes')
    a['sha256'] = e.digest(path)
    it.set(item, status='ready', analysis=a, size=a['parts'][0]['size'])
    return it.item_analysis(item)


class PlanAndInstall(unittest.TestCase):
    def plan(self, device, rep, protected=False, **over):
        td = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, td, True)
        it = intake(td, {})
        item = ready_item(td, it, **over)
        return it, item, it.plan_device(device, rep, item, Path(td) / 'r', protected, lambda t: None)

    def test_new_install_is_ready_with_unknown_publisher_warning(self):
        _, _, row = self.plan(FakeDevice(), report())
        self.assertEqual(row['status'], 'ready'); self.assertTrue(any('Неизвестный издатель' in w for w in row['warnings']))

    def test_incompatible_api_abi_target_and_space(self):
        self.assertEqual(self.plan(FakeDevice(), report(api=28), min_api=30)[2]['status'], 'blocked')
        self.assertEqual(self.plan(FakeDevice(), report(), base_abis=['arm64-v8a'], abis=['arm64-v8a'])[2]['status'], 'blocked')
        self.assertEqual(self.plan(FakeDevice(), report(api=34), target_api=22)[2]['status'], 'blocked')
        self.assertEqual(self.plan(FakeDevice(), report(api=30), target_api=22)[2]['status'], 'ready')
        row = self.plan(FakeDevice(), report(free=40 * 1048576))[2]
        self.assertEqual(row['status'], 'blocked'); self.assertIn('Недостаточно места', ' '.join(row['reasons']))

    def test_installed_versions_and_certificates(self):
        old = {'version': 4, 'certificates': [CERT]}
        self.assertEqual(self.plan(FakeDevice(old), report(['org.example.app']))[2]['status'], 'update')
        self.assertEqual(self.plan(FakeDevice({**old, 'version': 5}), report(['org.example.app']))[2]['status'], 'already')
        self.assertEqual(self.plan(FakeDevice({**old, 'version': 9}), report(['org.example.app']))[2]['status'], 'blocked')
        row = self.plan(FakeDevice({**old, 'certificates': ['d' * 64]}), report(['org.example.app']))[2]
        self.assertEqual(row['status'], 'blocked'); self.assertIn('Подпись', ' '.join(row['reasons']))

    def test_source_projector_launcher_and_catalog_mismatch_are_blocked(self):
        self.assertEqual(self.plan(FakeDevice(), report(), protected=True, package='io.freemytv')[2]['status'], 'blocked')
        self.assertEqual(self.plan(FakeDevice(), report(), provenance={'kind': 'catalog_mismatch', 'message': ''})[2]['status'], 'blocked')
        self.assertEqual(self.plan(FakeDevice(), report(), package='com.android.vending')[2]['status'], 'blocked')
        self.assertEqual(self.plan(FakeDevice(), report(), obb_files=2)[2]['status'], 'blocked')

    def test_install_verifies_real_package_and_refuses_changed_device(self):
        it, item, row = self.plan(FakeDevice(), report())
        d = FakeDevice(); d.after = {'version': 5, 'certificates': [CERT]}
        res = it.install_device(d, report(), item, row, Path(tempfile.mkdtemp()), lambda *a: None)
        self.assertEqual(res['status'], 'done'); self.assertTrue(res['new_install'])
        self.assertIn(('install', '-r'), [c[:2] for c in d.commands])
        self.assertFalse(any('-d' in c or '-g' in c or '-t' in c for c in d.commands if c[0] == 'install'))
        with self.assertRaises(e.Problem) as ctx:
            it.install_device(FakeDevice(identity='other'), report(identity='other'), item, row, Path(tempfile.mkdtemp()), lambda *a: None)
        self.assertEqual(ctx.exception.code, 'DEVICE_CHANGED')

    def test_install_refuses_when_version_changed_after_plan(self):
        old = {'version': 4, 'certificates': [CERT]}
        it, item, row = self.plan(FakeDevice(old), report(['org.example.app']))
        d = FakeDevice(old, current=5)
        with self.assertRaises(e.Problem) as ctx:
            it.install_device(d, report(['org.example.app']), item, row, Path(tempfile.mkdtemp()), lambda *a: None)
        self.assertEqual(ctx.exception.code, 'APK_PLAN')
        self.assertFalse(any(c[0] == 'install' for c in d.commands))

    def test_android_errors_and_tv_prompts(self):
        it, item, row = self.plan(FakeDevice(), report())
        d = FakeDevice(output='Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]')
        with self.assertRaises(e.Problem) as ctx:
            it.install_device(d, report(), item, row, Path(tempfile.mkdtemp()), lambda *a: None)
        self.assertEqual(ctx.exception.code, 'INSTALL_FAILED_UPDATE_INCOMPATIBLE')
        d = FakeDevice(output=e.Problem('timeout', code='TIMEOUT'))
        self.assertEqual(it.install_device(d, report(), item, row, Path(tempfile.mkdtemp()), lambda *a: None)['status'], 'manual')
        d = FakeDevice(); d.after = {'version': 4, 'certificates': [CERT]}
        with self.assertRaises(e.Problem) as ctx:
            it.install_device(d, report(), item, row, Path(tempfile.mkdtemp()), lambda *a: None)
        self.assertEqual(ctx.exception.code, 'APK_VERIFY')

    def test_plan_expires_and_is_single_use(self):
        it, item, row = self.plan(FakeDevice(), report())
        key = it.remember_plan(item, {'a': row})
        self.assertTrue(it.take_plan(key))
        with self.assertRaises(e.Problem):
            it.take_plan(key)
        key = it.remember_plan(item, {'a': row}); it.plans[key]['expires'] = time.time() - 1
        with self.assertRaises(e.Problem):
            it.take_plan(key)


class ManagerJobs(unittest.TestCase):
    def test_partial_success_is_reported_per_device(self):
        with tempfile.TemporaryDirectory() as td:
            m = gui.Manager(td, FakeTools(td))
            item = ready_item(td, m.intake)
            devices = {'192.168.31.232:5555': FakeDevice(), '192.168.31.20:5555': FakeDevice(output='Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]')}
            for a, d in devices.items():
                d.address = a; d.after = {'version': 5, 'certificates': [CERT]}
            class D:
                def __new__(cls, address, tools):
                    return devices[address]
            reports = {a: report(identity='id-' + a) for a in devices}
            for a, d in devices.items():
                d.diagnose = (lambda a=a: copy.deepcopy(reports[a]))
            with patch('app.Device', D):
                m.apk_job('plan', item, list(devices))
                jobs = m.intake.jobs
                self.assertTrue(jobs['plan']); self.assertEqual({r['status'] for r in jobs['rows'].values()}, {'ready'})
                m.apk_job('install', m.intake.take_plan(jobs['plan']))
            rows = m.intake.jobs['rows']
            self.assertEqual(rows['192.168.31.232:5555']['status'], 'done')
            self.assertEqual(rows['192.168.31.20:5555']['status'], 'error')
            self.assertEqual(rows['192.168.31.20:5555']['code'], 'INSTALL_FAILED_INSUFFICIENT_STORAGE')
            self.assertFalse(m.busy)
            journal = list((Path(td) / 'apk-installs').glob('*.json'))
            self.assertEqual(len(journal), 1)
            self.assertNotIn('icon', m.snapshot()['uploads']['items'][0]['analysis'])

    def test_status_never_carries_icons(self):
        with tempfile.TemporaryDirectory() as td:
            m = gui.Manager(td, FakeTools(td))
            ready_item(td, m.intake, icon='data:image/png;base64,AAAA')
            a = m.snapshot()['uploads']['items'][0]['analysis']
            self.assertTrue(a['has_icon']); self.assertNotIn('icon', a)


class HttpUpload(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.m = gui.Manager(self.td, FakeTools(self.td, {'base': info()}))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), gui.make_handler(self.m, 'secret'))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = f'127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); shutil.rmtree(self.td, True)
    def post(self, path, body, headers):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        c.request('POST', path, body=body, headers={'Host': self.host, **headers})
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data) if data else {})
    def test_upload_requires_token_and_octet_stream(self):
        apk = fake_apk(Path(self.td) / 'a.apk').read_bytes()
        self.assertEqual(self.post('/upload', apk, {'Content-Type': 'application/octet-stream', 'X-File-Name': 'a.apk'})[0], 403)
        self.assertEqual(self.post('/upload', apk, {'X-Manager-Token': 'secret', 'Content-Type': 'application/json', 'X-File-Name': 'a.apk'})[0], 403)
        self.assertEqual(self.post('/upload', apk, {'X-Manager-Token': 'secret', 'Content-Type': 'application/octet-stream', 'X-File-Name': 'a.apk', 'Origin': 'http://evil.example'})[0], 403)
        status, body = self.post('/upload', apk, {'X-Manager-Token': 'secret', 'Content-Type': 'application/octet-stream', 'X-File-Name': '..%2F..%2Fa.apk'})
        self.assertEqual(status, 200, body)
        item = body['id']
        for _ in range(400):
            if self.m.intake.items[item]['status'] not in ('receiving', 'analyzing'):
                break
            time.sleep(.05)
        self.assertEqual(self.m.intake.items[item]['name'], 'a.apk')
        self.assertTrue((Path(self.td) / 'uploads' / item / 'original.apk').is_file())
    def test_size_limits_and_json_route_limit_kept(self):
        status, _ = self.post('/upload', b'x', {'X-Manager-Token': 'secret', 'Content-Type': 'application/octet-stream', 'X-File-Name': 'a.apk', 'Content-Length': str(ai.MAX_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(self.post('/apk', json.dumps({'mode': 'x', 'pad': 'y' * 70000}), {'X-Manager-Token': 'secret', 'Content-Type': 'application/json'})[0], 413)
    def test_truncated_upload_leaves_no_partial_file(self):
        s = socket.create_connection(('127.0.0.1', self.server.server_port))
        s.sendall(f'POST /upload HTTP/1.1\r\nHost: {self.host}\r\nX-Manager-Token: secret\r\nContent-Type: application/octet-stream\r\nX-File-Name: big.apk\r\nContent-Length: 5000000\r\n\r\n'.encode() + b'x' * 100000)
        s.close()
        for _ in range(400):
            states = [i['status'] for i in self.m.intake.items.values()]
            if states and states[0] == 'error':
                break
            time.sleep(.05)
        self.assertEqual(states, ['error'])
        self.assertEqual(list((Path(self.td) / 'uploads').rglob('*.part')), [])


WIFI = '''mWifiInfo SSID: Home5, BSSID: 02:00:00:00:00:00, Net ID: 5, Frequency: 5240
WifiConfigManager - Configured networks Begin ----
ID: 1 SSID: "OldNet" PROVIDER-NAME: null BSSID=null
 PSK=*
IP config:
 cuid=10053 cname=com.google.android.tungsten.setupwraith luid=10053 lname=com.google.android.tungsten.setupwraith mostRecentlyConnected=false
ID: 2 SSID: "Other" PROVIDER-NAME: null BSSID=null
 cuid=1000 cname=android.uid.system:1000 luid=1000 mostRecentlyConnected=false
ID: 5 SSID: "Home5" PROVIDER-NAME: null BSSID=null
 cuid=1000 cname=com.android.tv.settings luid=1000 mostRecentlyConnected=true
WifiConfigManager - Configured networks End ----
WifiConfigManager - ConfigurationMap Begin ----
, 1=ID: 1 SSID: "OldNet" PROVIDER-NAME: null
 cuid=10053 cname=com.google.android.tungsten.setupwraith
WifiConfigManager - ConfigurationMap End ----
WifiConfigManager - Last selected network ID 5
'''


class WifiDevice:
    address = '192.168.31.232:5555'
    def __init__(self, wifi=WIFI, iface='wlan0'):
        self.wifi = wifi; self.iface = iface; self.commands = []
    def shell(self, *args, **kw):
        self.commands.append(args)
        if args == ('dumpsys', 'wifi'):
            return self.wifi
        if args[:2] == ('cmd', 'wifi') and args[2] == 'help':
            return 'list-networks\nforget-network <networkId>'
        if args[:3] == ('cmd', 'wifi', 'forget-network'):
            self.wifi = self.wifi.replace('ID: ' + args[3] + ' ', 'GONE ')
            return 'Forget successful'
        if args[:2] == ('cmd', 'package'):
            return 'com.google.android.apps.tv.launcherx/.home.HomeActivity'
        return ''
    def interfaces(self):
        return {'192.168.31.232': self.iface}
    def prop(self, key):
        return {'ro.build.version.sdk': '30', 'ro.build.version.release': '11'}[key]


class WifiTests(unittest.TestCase):
    def test_real_format_blocks_are_parsed_without_storing_ssid(self):
        nets = r.saved_networks(WIFI)
        self.assertEqual([(n['network_id'], n['owner'], n['current']) for n in nets],
                         [(1, r.SETUP, False), (2, 'android.uid.system', False), (5, 'com.android.tv.settings', True)])
        with patch.object(r.profiles, 'export', side_effect=e.Problem('old')):
            diag = r.diagnose_home(WifiDevice(), {'packages': ['io.freemytv'], 'api': 30})
        self.assertEqual(diag['owned_network_ids'], [{'network_id': 1, 'owner': r.SETUP, 'current': False}])
        self.assertNotIn('OldNet', json.dumps(diag)); self.assertNotIn('Home5', json.dumps(diag))
        self.assertTrue(any('ID 1' in s for s in diag['steps']))

    def test_forget_only_reviewed_non_active_setup_network(self):
        d = WifiDevice()
        plan = r.forget_plan(d, {'api': 30, 'android': '11'})
        allowed = {n['network_id'] for n in plan['networks'] if n['allowed']}
        self.assertEqual(allowed, {1})
        net = next(n for n in plan['networks'] if n['network_id'] == 1)
        res = r.forget_network(d, 1, net)
        self.assertTrue(res['forgotten'])
        self.assertIn(('cmd', 'wifi', 'forget-network', '1'), d.commands)
        with self.assertRaises(e.Problem):
            r.forget_network(WifiDevice(), 2, {'owner': 'android.uid.system', 'ssid': 'Other'})

    def test_active_setup_network_is_not_forgotten_over_wifi(self):
        wifi = WIFI.replace('cname=com.android.tv.settings', 'cname=com.google.android.tungsten.setupwraith')
        plan = r.forget_plan(WifiDevice(wifi), {'api': 30, 'android': '11'})
        self.assertEqual({n['network_id'] for n in plan['networks'] if n['allowed']}, {1})
        plan = r.forget_plan(WifiDevice(wifi, 'eth0'), {'api': 30, 'android': '11'})
        self.assertEqual({n['network_id'] for n in plan['networks'] if n['allowed']}, {1, 5})

    def test_android_9_has_no_forget_command(self):
        with self.assertRaises(e.Problem) as ctx:
            r.forget_plan(WifiDevice(), {'api': 28, 'android': '9'})
        self.assertEqual(ctx.exception.code, 'WIFI_UNSUPPORTED')


SESSION = '''    VLC org.videolan.vlc/VLC (userId=0)
      package=org.videolan.vlc
      active=true
      state=PlaybackState {state=PLAYING(3), position=%d, buffered position=0, speed=1.0, updated=1, actions=0, custom actions=[], active item id=0, error=null}
    Old com.vk.tv/x (userId=0)
      package=com.vk.tv
      state=PlaybackState {state=2, position=50, buffered position=0}'''


class PlayerDevice:
    def __init__(self, positions):
        self.positions = list(positions); self.commands = []
    def packages(self):
        return ['org.videolan.vlc']
    def shell(self, *args, **kw):
        self.commands.append(args)
        if args[:2] == ('cmd', 'package'):
            return 'priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=false\norg.videolan.vlc/.StartActivity'
        if args[0] == 'am':
            return 'Starting: Intent\nStatus: ok\nTotalTime: 900'
        if args == ('dumpsys', 'media_session'):
            p = self.positions.pop(0) if self.positions else None
            return SESSION % p if p is not None else 'no sessions'
        return ''


class MediaTests(unittest.TestCase):
    URI = 'smb://192.168.31.2/TVMovies_ab/%D0%A4%D0%B8%D0%BB%D1%8C%D0%BC.mkv'
    def test_session_parser_handles_android_9_and_14(self):
        s = r.media_sessions(SESSION % 1200)
        self.assertEqual(s[0], {'package': 'org.videolan.vlc', 'state': 3, 'position': 1200})
        self.assertEqual(s[1]['state'], 2)
    def test_playback_verified_only_when_position_advances(self):
        d = PlayerDevice([None, 1000, 4000])
        res = r.play_test(d, 'vlc', self.URI, wait=30, poll=5, sleep=lambda s: None)
        self.assertTrue(res['playback_verified']); self.assertEqual(res['status'], 'done')
        am = next(c for c in d.commands if c[0] == 'am')
        self.assertIn('-t', am); self.assertIn('video/*', am)
        d = PlayerDevice([1000, 1000, 1000, 1000, 1000, 1000])
        res = r.play_test(d, 'vlc', self.URI, wait=30, poll=5, sleep=lambda s: None)
        self.assertFalse(res['playback_verified']); self.assertEqual(res['status'], 'manual')
    def test_folder_address_is_not_handed_to_player(self):
        d = PlayerDevice([])
        res = r.handoff(d, 'vlc', 'smb://192.168.31.2/TVMovies_ab')
        self.assertEqual(res['status'], 'manual'); self.assertFalse(any(c[0] == 'am' for c in d.commands))
    def test_test_file_is_smallest_video_inside_share(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'big.mkv').write_bytes(b'x' * 100)
            (Path(td) / 'sub').mkdir(); (Path(td) / 'sub' / 'Мой фильм.mp4').write_bytes(b'x' * 10)
            (Path(td) / 'notes.txt').write_bytes(b'x')
            uri = media.test_uri({'uri': 'smb://192.168.31.2/TVMovies_ab'}, td)
            self.assertEqual(uri, 'smb://192.168.31.2/TVMovies_ab/sub/%D0%9C%D0%BE%D0%B9%20%D1%84%D0%B8%D0%BB%D1%8C%D0%BC.mp4')
            r.player_uri(uri)
        with tempfile.TemporaryDirectory() as td, self.assertRaises(e.Problem):
            media.pick_test_file(td)


class RemoteTests(unittest.TestCase):
    def test_states_are_explained(self):
        with patch('remote.run', return_value='List of devices attached\n192.168.31.20:5555\tunauthorized\n192.168.28.60:5555\tdevice\n'):
            self.assertEqual(remote.state('adb', '192.168.31.20')['kind'], 'action')
            self.assertEqual(remote.state('adb', '192.168.28.60')['kind'], 'ready')
            self.assertEqual(remote.state('adb', '192.168.31.232')['kind'], 'error')
        with self.assertRaises(e.Problem):
            remote.state('adb', '8.8.8.8')
    def test_only_remote_keys_and_valid_packages(self):
        class D:
            def __init__(self): self.calls = []
            def shell(self, *a, **k): self.calls.append(a); return ''
        d = D()
        remote.key(d, 'home'); self.assertEqual(d.calls[-1], ('input', 'keyevent', 'KEYCODE_HOME'))
        for bad in ('POWER', 'text', 'KEYCODE_FACTORY_RESET', '; reboot'):
            with self.assertRaises(e.Problem):
                remote.key(d, bad)
        for bad in ('x; reboot', '../a', ''):
            with self.assertRaises(e.Problem):
                remote.launch(d, bad)
    def test_component_with_dollar_is_quoted(self):
        class D:
            def __init__(self): self.calls = []
            def shell(self, *a, **k):
                self.calls.append(a)
                return 'com.mxtech.videoplayer.ad/com.mxtech.videoplayer.ActivityScreen$WebDelegate' if a[0] == 'cmd' else 'Status: ok\nTotalTime: 5'
        d = D(); remote.launch(d, 'com.mxtech.videoplayer.ad')
        self.assertIn("'com.mxtech.videoplayer.ad/com.mxtech.videoplayer.ActivityScreen$WebDelegate'", d.calls[-1])


DISK = '''Data-Free: 492544K / 4519032K total = 10% free
App Size: 2477830144
App Data Size: 1004347392
App Cache Size: 456757248
Package Names: ["com.vk.tv","com.teamsmart.videomanager.tv","com.miui.tv.analytics"]
App Sizes: [137363456,76546048,1000]
App Data Sizes: [69206016,22020096,0]
Cache Sizes: [62914560,20971520,0]'''


class OptimizeTests(unittest.TestCase):
    def test_diskstats_and_advice(self):
        disk = optimize.parse_diskstats(DISK)
        self.assertEqual(disk['packages']['com.vk.tv']['cache'], 62914560)
        self.assertEqual(disk['totals']['app_cache_size'], 456757248)
        rows = [{'package': 'com.teamsmart.videomanager.tv', 'label': 'x', 'installed': True, 'system': False, 'disabled': False, 'protected': False},
                {'package': 'com.miui.tv.analytics', 'label': 'x', 'installed': True, 'system': True, 'disabled': False, 'protected': True},
                {'package': 'com.vk.tv', 'label': 'x', 'installed': True, 'system': False, 'disabled': False, 'protected': False}]
        out, summary = optimize.analyse(rows, disk, {'free_bytes': 400 * 1048576, 'home': ''})
        kinds = {x['package']: x['kind'] for x in out}
        self.assertEqual(kinds, {'com.teamsmart.videomanager.tv': 'candidate', 'com.miui.tv.analytics': 'homemode', 'com.vk.tv': 'keep'})
        self.assertEqual(summary['homemode_pending'], ['com.miui.tv.analytics'])
        self.assertEqual(out[0]['package'], 'com.teamsmart.videomanager.tv')
        self.assertTrue(any('кэш' in n.lower() for n in summary['notes']))
    def test_trim_caches_is_never_pm_clear(self):
        class D:
            def __init__(self): self.calls = []; self.free = iter([100, 300])
            def free_space(self): return next(self.free)
            def shell(self, *a, **k): self.calls.append(a); return DISK if a[0] == 'dumpsys' else ''
        d = D(); res = optimize.trim_caches(d)
        self.assertEqual(res['freed_bytes'], 200); self.assertFalse(res['data_cleared'])
        self.assertIn(('pm', 'trim-caches', '999G'), d.calls)
        self.assertFalse(any(c[:2] == ('pm', 'clear') for c in d.calls))
    def test_compare_flags_different_launch_types(self):
        a = {'time': 't', 'ram_available_bytes': 100, 'process_count': 10, 'apps': {'vlc': {'status': 'done', 'ms': 900, 'launch_states': ['WARM']}}}
        b = {'time': 't2', 'ram_available_bytes': 150, 'process_count': 9, 'apps': {'vlc': {'status': 'done', 'ms': 700, 'launch_states': ['COLD']}}}
        c = optimize.compare(a, b)
        self.assertEqual(c['apps'][0]['delta_ms'], -200); self.assertFalse(c['apps'][0]['comparable'])
        self.assertEqual(optimize.compare(None, b)['available'], False)


if __name__ == '__main__':
    unittest.main()
