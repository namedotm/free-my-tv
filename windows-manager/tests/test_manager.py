import copy
import hashlib
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e
import app as gui

CERT = 'a' * 64
BASE = {'package': 'test.player', 'version': 4, 'min_api': 26, 'abis': ['armeabi-v7a'], 'certificates': [CERT]}
REPORT = {'api': 28, 'abis': ['armeabi-v7a'], 'properties': {'ro.serialno': 'serial'}}
APPLICATION = {'package': 'test.player', 'label': 'Плеер', 'certificate': CERT,
               'variants': {'armeabi-v7a': {'sha256': 'f' * 64, 'url': 'https://github.com/test/player.apk'}}}

class FakeTools:
    def __init__(self, data):
        self.data = Path(data)
        self.info = copy.deepcopy(BASE)
        self.adb = 'adb'
    def inspect(self, *args):
        return copy.deepcopy(self.info)

class FakeDevice:
    def __init__(self, old=None, free=10**10):
        self.old = old
        self.free = free
        self.commands = []
        self.installed_calls = 0
        self.install_result = 'Success'
    def prop(self, key): return 'serial'
    def installed(self, *args):
        self.installed_calls += 1
        if self.installed_calls > 1 and self.install_result == 'Success': return copy.deepcopy(BASE)
        return self.old
    def free_space(self): return self.free
    def adb(self, *args, **kwargs):
        self.commands.append(args)
        return self.install_result

class ValidationTests(unittest.TestCase):
    def test_ipv4_validation_and_no_commands(self):
        self.assertEqual(e.target('192.168.28.60'), '192.168.28.60:5555')
        self.assertEqual(e.target('10.2.3.4:12345'), '10.2.3.4:12345')
        for value in ['127.0.0.1','8.8.8.8','169.254.1.1','::1','192.168.1.1;reboot','host','192.168.1.2:0','192.168.1.2:65536','192.168.1.999']:
            with self.subTest(value=value), self.assertRaises(e.Problem): e.target(value)
    def test_duplicates_prevent_whole_run(self):
        with self.assertRaises(e.Problem): e.targets(['192.168.1.2','192.168.1.2:5555'])
    def test_badging_and_certificate_parse(self):
        info=e.parse_badging("package: name='test.player' versionCode='4' versionName='1'\nsdkVersion:'26'\nnative-code: 'armeabi-v7a' 'arm64-v8a'")
        self.assertEqual(info['abis'], ['armeabi-v7a','arm64-v8a'])
        self.assertEqual(e.certs('Signer #1 certificate SHA-256 digest: '+CERT), [CERT])
        with self.assertRaises(e.Problem):e.certs('WARNING: unsigned')
    def test_abi_and_api_must_both_match(self):
        e.compatible(BASE, REPORT)
        for report in [{'api': 21, 'abis':['armeabi-v7a']}, {'api':28,'abis':['arm64-v8a']}]:
            with self.assertRaises(e.Problem):e.compatible(BASE,report)
    def test_variant_follows_device_not_model(self):
        app={'variants':{'armeabi-v7a':1,'arm64-v8a':2}}
        self.assertEqual(e.choose_variant(app,['armeabi-v7a']),1)
        self.assertEqual(e.choose_variant(app,['arm64-v8a','armeabi-v7a']),2)
    def test_empty_vpn_allowlist_is_rejected(self):
        with self.assertRaises(e.Problem):e.vpn_profile(['org.videolan.vlc','com.aircast.receiver'])
        with self.assertRaises(e.Problem):e.vpn_profile(['com.google.android.youtube.tv'],False)
    def test_only_youtube_allowed_in_vpn(self):
        p=e.vpn_profile(['org.smarttube.stable','org.videolan.vlc','com.mxtech.videoplayer.ad','com.aircast.receiver','com.google.android.youtube.tv'], True)
        self.assertEqual(p['apps'],['com.google.android.youtube.tv','org.smarttube.stable'])
        self.assertEqual(p['settings']['applist_type'],'whitelist')
        self.assertFalse(p['settings']['autostart'])
        self.assertFalse(p['settings']['auto_connect'])
    def test_dangerous_redirect_rejected(self):
        for u in ['http://github.com/x','https://evil.example/x','https://user:pass@github.com/x','https://github.com.evil.example/x']:
            with self.assertRaises(e.Problem):e.check_url(u)
    def test_zip_slip(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'bad.zip'
            with zipfile.ZipFile(f,'w') as z:z.writestr('../escape','bad')
            with self.assertRaises(e.Problem):e.extract_zip(f,Path(td)/'out')
            self.assertFalse((Path(td)/'escape').exists())
    def test_hash_tamper_never_cached_as_valid(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):
                if hasattr(self,'sent'):return b''
                self.sent=True;return b'tampered'
        with tempfile.TemporaryDirectory() as td, patch('engine.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=Response()
            dest=Path(td)/'app.apk'
            with self.assertRaises(e.Problem):e.download('https://github.com/valid.apk',dest,'0'*64)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix('.partial').exists())

class InstallationTests(unittest.TestCase):
    def exercise(self, device, info=None, report=None):
        with tempfile.TemporaryDirectory() as td:
            t=FakeTools(td)
            if info:t.info=info
            apk=Path(td)/'downloaded.apk';apk.write_bytes(b'APK')
            with patch('engine.download',return_value=apk), patch('engine.check_release'):
                return e.install_app(device, report or REPORT, 'vlc', APPLICATION, t, Path(td)/'report', lambda msg:None)
    def test_conflicting_certificate_never_installs(self):
        old=copy.deepcopy(BASE);old['certificates']=['b'*64]
        d=FakeDevice(old)
        self.assertEqual(self.exercise(d)['status'],'skipped')
        self.assertEqual(d.commands,[])
    def test_existing_same_version_is_idempotent(self):
        d=FakeDevice(copy.deepcopy(BASE))
        self.assertEqual(self.exercise(d)['status'],'done')
        self.assertEqual(d.commands,[])
    def test_existing_newer_version_never_downgrades(self):
        old=copy.deepcopy(BASE);old['version']=999
        d=FakeDevice(old)
        self.assertEqual(self.exercise(d)['status'],'done');self.assertEqual(d.commands,[])
    def test_insufficient_space_never_installs(self):
        d=FakeDevice(free=1)
        with self.assertRaises(e.Problem):self.exercise(d)
        self.assertEqual(d.commands,[])
    def test_untrusted_apk_certificate_never_installs(self):
        bad=copy.deepcopy(BASE);bad['certificates']=['c'*64]
        d=FakeDevice()
        with self.assertRaises(e.Problem):self.exercise(d,bad)
        self.assertEqual(d.commands,[])
    def test_wrong_package_never_installs(self):
        bad=copy.deepcopy(BASE);bad['package']='com.android.settings'
        d=FakeDevice()
        with self.assertRaises(e.Problem):self.exercise(d,bad)
        self.assertEqual(d.commands,[])
    def test_success_requires_post_install_verification(self):
        d=FakeDevice()
        result=self.exercise(d)
        self.assertTrue(result['new_install']);self.assertEqual(d.installed_calls,2)
        self.assertEqual(d.commands[0][:2],('install','-r'))
        self.assertNotIn('-d',d.commands[0]);self.assertNotIn('-g',d.commands[0])
    def test_adb_non_success_is_not_a_pass(self):
        d=FakeDevice();d.install_result='Failure [INSTALL_FAILED_USER_RESTRICTED]'
        with self.assertRaises(e.Problem):self.exercise(d)
    def test_reassigned_ip_never_installs(self):
        d=FakeDevice();d.prop=lambda key:'another'
        with self.assertRaises(e.Problem):self.exercise(d)
        self.assertEqual(d.commands,[])

class JobsTests(unittest.TestCase):
    def test_device_failure_does_not_stop_other_devices(self):
        class TestDevice:
            def __init__(self,address,tools):self.address=address
            def diagnose(self):
                if self.address.startswith('192.168.1.1:'):raise e.Problem('Недоступно')
                return {'model':'Box','android':'9','api':28,'abis':['armeabi-v7a'],'identity':'id','free_bytes':1000000000}
            def measures(self):return {'cpu':'test'}
        with tempfile.TemporaryDirectory() as td, patch('app.Device', TestDevice):
            m=gui.Manager(td,FakeTools(td));m.busy=True
            m.work(['192.168.1.1:5555','192.168.1.2:5555'],[],'check',False)
            self.assertEqual(m.rows['192.168.1.1:5555']['status'],'error')
            self.assertEqual(m.rows['192.168.1.2:5555']['status'],'done')
            self.assertFalse(m.busy)
    def test_profile_only_pushes_file_does_not_start_vpn(self):
        class D:
            address='192.168.1.2:5555'
            def packages(self):return ['org.smarttube.stable']
            def adb(self,*args):self.command=args
        with tempfile.TemporaryDirectory() as td:
            m=gui.Manager(td,FakeTools(td));folder=Path(td)/'123';folder.mkdir();d=D()
            m.make_profile(d,{},folder,False)
            self.assertEqual(d.command[0],'push')
            self.assertTrue(d.command[-1].startswith('/sdcard/Download/'))
            self.assertFalse(e.read_json(folder/'vpn-profile.json')['verified'])
    def test_read_only_check_does_not_install(self):
        class D:
            def __init__(self,*args):pass
            def diagnose(self):return {'model':'Test','android':'9','api':28,'abis':[],'identity':'id','free_bytes':123}
            def measures(self):return {}
        with tempfile.TemporaryDirectory() as td, patch('app.Device',D), patch('app.install_app') as install:
            m=gui.Manager(td,FakeTools(td));m.device_work('192.168.1.1:5555',['freemytv'],'check',False)
            install.assert_not_called()
            self.assertEqual(m.rows['192.168.1.1:5555']['status'],'done')

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.manager=gui.Manager(self.tmp.name,FakeTools(self.tmp.name))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),gui.make_handler(self.manager,'test-token'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,token='test-token',origin=None,host=None,body=None):
        headers={'X-Manager-Token':token,'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        return urllib.request.urlopen(urllib.request.Request(self.base+path,data=body,headers=headers))
    def test_status_and_ui_work(self):
        with self.request('/status') as r:self.assertFalse(json.load(r)['busy'])
        with self.request('/') as r:self.assertIn('Xiaomi TV Manager',r.read().decode())
    def test_token_required(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/status',token='wrong')
        self.assertEqual(cm.exception.code,403)
    def test_cross_origin_rejected(self):
        with self.assertRaises(urllib.error.HTTPError):self.request('/action',origin='https://evil.example',body=b'{}')
        self.assertFalse(self.manager.busy)
    def test_dns_rebinding_host_rejected(self):
        with self.assertRaises(urllib.error.HTTPError):self.request('/status',host='evil.example')
    def test_invalid_addresses_cannot_start_job(self):
        data=json.dumps({'ips':['8.8.8.8'],'apps':[],'action':'install'}).encode()
        with self.assertRaises(urllib.error.HTTPError):self.request('/action',body=data)
        self.assertFalse(self.manager.busy)

@unittest.skipUnless(os.environ.get('TVM_REAL_TOOLS') == '1', 'Opt-in signature check; downloads Windows verification tools, no TV connection')
class RealArtifactTests(unittest.TestCase):
    def test_actual_first_stage_apk(self):
        artifact=e.ROOT/'payload/FreeMyTV.apk'
        if not artifact.exists():artifact=e.ROOT.parent/'artifacts/FreeMyTV.apk'
        self.assertEqual(e.digest(artifact),e.APK_HASH)
        t=e.Tools();info=t.inspect(artifact,28)
        self.assertEqual(info['package'],'io.freemytv');self.assertEqual(info['version'],3)
        self.assertEqual(info['certificates'],['8da630acb15da56191213ac858449ef5ee5908332d52b8774d52ad11ea3ec99c'])

if __name__=='__main__':unittest.main()
