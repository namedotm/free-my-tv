import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e
import app as gui
import reports
from test_manager import FakeTools, FakeDevice, BASE, REPORT, APPLICATION

# Shape of a real Mi Box S (Android 14) line: ByeByeDPI (uid 10013) tunnelling every other app.
GLOBAL_VPN = ('NetworkAgentInfo{network{130}  handle{1}  ni{VPN CONNECTED extra: } lp{{InterfaceName: tun0 LinkAddresses: [ 10.10.10.10/32 ] }}  '
              'nc{[ Transports: WIFI|VPN Capabilities: NOT_METERED&INTERNET TransportInfo: <VpnTransportInfo{type=1, sessionId=ByeDPI, bypassable=false}> '
              'Uids: <{0-10012, 10014-20012, 20014-99999}> OwnerUid: 10013 AdminUids: [10013] UnderlyingNetworks: [131]]}  factorySerialNumber=3}\n'
              'NetworkAgentInfo{network{131}  ni{WIFI CONNECTED extra: } nc{[ Transports: WIFI Capabilities: NOT_VPN ]}}')
PER_APP = ('NetworkAgentInfo{ ni{VPN CONNECTED} lp{{InterfaceName: tun0 }} nc{[ Transports: WIFI|VPN Capabilities: INTERNET '
           'Uids: <{10012, 10065}> OwnerUid: 10013 ]}}')
UIDS = {'io.github.romanvht.byedpi': 10013, 'org.smarttube.stable': 10012, 'com.google.android.youtube.tv': 10065,
        'org.videolan.vlc': 10040, 'com.smartdevice.tv.aircast': 10050, 'android': 1000}


class VpnRoutingTests(unittest.TestCase):
    def test_real_global_byedpi_tunnel_is_an_error(self):
        r = e.vpn_routing(e.parse_vpn(GLOBAL_VPN), UIDS)
        self.assertEqual((r['scope'], r['ok'], r['name']), ('global', False, 'ByeByeDPI'))
        self.assertIn('VLC', r['local_inside'])
        self.assertIn('AirCast', r['local_inside'])
    def test_per_app_tunnel_with_only_youtube_apps_passes(self):
        r = e.vpn_routing(e.parse_vpn(PER_APP), UIDS)
        self.assertTrue(r['ok'])
        self.assertEqual(r['inside'], ['com.google.android.youtube.tv', 'org.smarttube.stable'])
        self.assertEqual(r['local_inside'], [])
    def test_media_player_inside_per_app_tunnel_fails(self):
        dump = PER_APP.replace('10012, 10065', '10012, 10040')
        r = e.vpn_routing(e.parse_vpn(dump), UIDS)
        self.assertFalse(r['ok']); self.assertIn('org.videolan.vlc', r['extra'])
    def test_no_vpn_is_not_reported_as_verified_routing(self):
        r = e.vpn_routing(e.parse_vpn('NetworkAgentInfo{ nc{[ Transports: WIFI Capabilities: NOT_VPN ]}}'), UIDS)
        self.assertFalse(r['active']); self.assertIsNone(r['ok'])
    def test_missing_uid_list_is_unknown_not_success(self):
        r = e.vpn_routing(e.parse_vpn('NetworkAgentInfo{ nc{[ Transports: VPN Capabilities: INTERNET ]}}'), UIDS)
        self.assertEqual(r['scope'], 'unknown'); self.assertIsNone(r['ok'])
    def test_warning_recommendation_for_global_vpn_and_old_smarttube(self):
        r = e.vpn_routing(e.parse_vpn(GLOBAL_VPN), UIDS)
        rec = e.Device.recommendations([e.OLD_SMARTTUBE, 'llc.itdev.incy', 'io.github.romanvht.byedpi'], r)
        self.assertEqual(len(rec['warnings']), 3)


class IdentityTests(unittest.TestCase):
    def test_device_key_ignores_build_and_ip(self):
        a = {'ro.serialno': '3105', 'ro.product.model': 'MiProjM05', 'ro.build.fingerprint': 'old'}
        b = dict(a, **{'ro.build.fingerprint': 'after-ota'})
        self.assertEqual(e.device_key(a), e.device_key(b))
        self.assertNotEqual(e.device_key(a), e.device_key(dict(a, **{'ro.serialno': '4015'})))
        self.assertEqual(e.device_key({'ro.serialno': ''}), '')
    def test_ram_and_storage_are_different_numbers(self):
        self.assertEqual(e.ram_available('MemTotal: 2055220 kB\nMemAvailable:     926096 kB\n'), 926096)
        self.assertIsNone(e.ram_available('no data'))


def fake_report(key, model='Box'):
    return {'model': model, 'android': '11', 'api': 30, 'abis': ['armeabi-v7a'], 'identity': 'id-' + key,
            'device_key': key, 'free_bytes': 10 ** 9, 'packages': [], 'properties': {'ro.serialno': key}}


class SourceProtectionTests(unittest.TestCase):
    def manager(self, td, devices):
        class D:
            def __init__(self, address, tools): self.address = address
            def diagnose(self): return copy.deepcopy(devices[self.address])
            def measures(self): return {}
            def packages(self): return []
            def shell(self, *args, **kwargs): return ''
        patcher = patch('app.Device', D); patcher.start(); self.addCleanup(patcher.stop)
        return gui.Manager(td, FakeTools(td))

    def test_projector_on_new_ip_is_recognized_by_serial(self):
        with tempfile.TemporaryDirectory() as td, patch('app.install_app') as install:
            m = self.manager(td, {'192.168.28.72:5555': fake_report('projector', 'MiProjM05')})
            m.source_device = {'device_key': 'projector'}
            m.device_work('192.168.28.72:5555', ['freemytv'], 'install', False, {'source_ip': '192.168.28.60:5555'})
            install.assert_not_called()
            task = m.rows['192.168.28.72:5555']['apps']['freemytv']
            self.assertEqual((task['status'], task['code']), ('skipped', 'SOURCE_PROTECTED'))
            self.assertTrue(m.rows['192.168.28.72:5555']['source'])
    def test_unknown_source_blocks_launcher_updates_everywhere(self):
        with tempfile.TemporaryDirectory() as td, patch('app.install_app') as install:
            m = self.manager(td, {'192.168.31.232:5555': fake_report('box1')})
            m.device_work('192.168.31.232:5555', ['freemytv'], 'install', False, {'source_ip': '192.168.28.60:5555'})
            install.assert_not_called()
            self.assertEqual(m.rows['192.168.31.232:5555']['apps']['freemytv']['code'], 'SOURCE_UNKNOWN')
    def test_known_source_allows_other_box(self):
        with tempfile.TemporaryDirectory() as td, patch('app.install_app', return_value={'status': 'done', 'message': 'ok', 'new_install': False}) as install:
            m = self.manager(td, {'192.168.31.232:5555': fake_report('box1')})
            m.source_device = {'device_key': 'projector'}
            m.device_work('192.168.31.232:5555', ['freemytv'], 'install', False, {'source_ip': '192.168.28.60:5555'})
            install.assert_called_once()
    def test_device_without_serial_counts_as_possible_source(self):
        with tempfile.TemporaryDirectory() as td, patch('app.install_app') as install:
            m = self.manager(td, {'192.168.31.20:5555': fake_report('')})
            m.source_device = {'device_key': 'projector'}
            m.device_work('192.168.31.20:5555', ['freemytv'], 'install', False, {'source_ip': '192.168.28.60:5555'})
            install.assert_not_called()
    def test_profile_sync_refuses_source_under_other_ip(self):
        with tempfile.TemporaryDirectory() as td, patch('profiles.apply') as apply:
            m = self.manager(td, {'192.168.28.72:5555': fake_report('projector')})
            m.source_device = {'device_key': 'projector'}
            m.device_work('192.168.28.72:5555', [], 'sync_ui', False, {'source_ip': '192.168.28.60:5555'})
            apply.assert_not_called()
            self.assertEqual(m.rows['192.168.28.72:5555']['code'], 'SOURCE_PROTECTED')
    def test_same_tv_on_two_addresses_runs_once(self):
        with tempfile.TemporaryDirectory() as td, patch('app.install_app', return_value={'status': 'done', 'message': 'ok'}) as install:
            r = fake_report('box1')
            m = self.manager(td, {'192.168.31.232:5555': r, '192.168.30.5:5555': r})
            m.source_device = {'device_key': 'projector'}
            m.busy = True
            m.work(['192.168.31.232:5555', '192.168.30.5:5555'], ['vlc'], 'install', False, {'source_ip': '192.168.28.60:5555'})
            self.assertEqual(install.call_count, 1)
            self.assertIn('skipped', [row['status'] for row in m.rows.values()])
    def test_read_source_remembers_serial_even_without_export(self):
        with tempfile.TemporaryDirectory() as td, patch('profiles.export', side_effect=e.Problem('нет', code='PROFILE_UNAVAILABLE')):
            m = self.manager(td, {'192.168.28.72:5555': fake_report('projector', 'MiProjM05')})
            m.device_work('192.168.28.72:5555', [], 'read_source', False, {'source_ip': '192.168.28.72:5555'})
            self.assertEqual(e.read_json(Path(td) / 'source-device.json')['device_key'], 'projector')
            self.assertNotIn('device_key', m.snapshot()['source_device'])


class PlayEditionTests(unittest.TestCase):
    def test_official_play_edition_is_kept_not_conflict(self):
        app = copy.deepcopy(APPLICATION); app['store'] = {'installer': 'com.android.vending', 'certificates': ['c' * 64]}
        old = copy.deepcopy(BASE); old['certificates'] = ['c' * 64]; old['version'] = 9
        d = FakeDevice(old); d.installer = lambda pkg: 'com.android.vending'
        with tempfile.TemporaryDirectory() as td:
            apk = Path(td) / 'a.apk'; apk.write_bytes(b'APK')
            with patch('engine.download', return_value=apk), patch('engine.check_release'):
                r = e.install_app(d, REPORT, 'vlc', app, FakeTools(td), Path(td) / 'r', lambda m: None)
        self.assertEqual((r['status'], r['channel']), ('done', 'google-play'))
        self.assertTrue(r['already_installed']); self.assertEqual(d.commands, [])
    def test_same_signature_from_unknown_installer_stays_conflict(self):
        app = copy.deepcopy(APPLICATION); app['store'] = {'installer': 'com.android.vending', 'certificates': ['c' * 64]}
        old = copy.deepcopy(BASE); old['certificates'] = ['c' * 64]
        d = FakeDevice(old); d.installer = lambda pkg: 'com.uptodown.installer'
        with tempfile.TemporaryDirectory() as td:
            apk = Path(td) / 'a.apk'; apk.write_bytes(b'APK')
            with patch('engine.download', return_value=apk), patch('engine.check_release'):
                r = e.install_app(d, REPORT, 'vlc', app, FakeTools(td), Path(td) / 'r', lambda m: None)
        self.assertEqual(r['status'], 'skipped'); self.assertEqual(d.commands, [])


class ReportTests(unittest.TestCase):
    def test_report_has_no_serial_and_separates_ram_from_storage(self):
        r = fake_report('k'); r.update(properties={'ro.serialno': 'SECRET-SERIAL'}, ram_available_kb=900000, home='io.freemytv/.HomeActivity',
                                      packages=['org.videolan.vlc'], versions='Packages:\n  Package [org.videolan.vlc] (1):\n    versionCode=13070109 minSdk=30\n    versionName=3.7.1\n    installerPackageName=com.android.vending\n',
                                      vpn={'message': 'Активного VPN нет.'}, recommendations={'warnings': ['w']})
        t = reports.text(r)
        self.assertNotIn('SECRET-SERIAL', t)
        self.assertIn('/data: 954 МБ', t); self.assertIn('ОЗУ', t.replace('оперативной памяти', 'ОЗУ'))
        self.assertIn('VLC: 3.7.1 (code 13070109), источник: Google Play', t)
    def test_updated_system_app_shows_installed_not_factory_version(self):
        dump = ('Packages:\n  Package [com.google.android.youtube.tv] (1):\n    versionCode=727302320\n    versionName=7.27.302\n    installerPackageName=com.android.vending\n'
                'Hidden system packages:\n  Package [com.google.android.youtube.tv] (2):\n    versionCode=21017320\n    versionName=2.10.17\n')
        self.assertEqual(reports.package_versions(dump)['com.google.android.youtube.tv']['version_name'], '7.27.302')


if __name__ == '__main__':
    unittest.main()
