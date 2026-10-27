import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import profiles as p
import media
import engine as e
import app
from test_manager import FakeTools


class ProfileTests(unittest.TestCase):
    def test_merge_preserves_orders_adds_only_installed_and_has_no_duplicates(self):
        source=copy.deepcopy(p.DEFAULT);source['settings']['shelf']=['org.example.player','org.smarttube.stable']
        target=copy.deepcopy(p.DEFAULT);target['settings']['shelf']=['org.example.other','org.smarttube.stable']
        installed=['org.example.player','org.example.other','org.smarttube.stable','llc.itdev.incy','com.google.android.youtube.tv']
        result,missing=p.merge(source,target,installed)
        self.assertEqual(result['settings']['shelf'],['org.example.player','org.smarttube.stable','org.example.other','llc.itdev.incy','com.google.android.youtube.tv'])
        self.assertEqual(missing,[])
        self.assertEqual(target['settings']['shelf'],['org.example.other','org.smarttube.stable'])
    def test_missing_apps_not_falsely_added(self):
        result,missing=p.merge(p.DEFAULT,p.DEFAULT,['llc.itdev.incy'])
        self.assertEqual(result['settings']['shelf'],['llc.itdev.incy'])
        self.assertIn('org.smarttube.stable',missing)
    def test_capacity_does_not_drop_old_favorites(self):
        target=copy.deepcopy(p.DEFAULT);target['max_shelf']=1;target['settings']['shelf']=['org.example.old']
        with self.assertRaises(e.Problem) as cm:p.merge(p.DEFAULT,target,['org.example.old','llc.itdev.incy'])
        self.assertEqual(cm.exception.code,'PROFILE_CAPACITY')
        self.assertEqual(target['settings']['shelf'],['org.example.old'])
    def test_secrets_home_permissions_and_machine_identity_rejected(self):
        for key in ['tmdbKey','plexToken','adbkey','homeMode','nowPlaying','sleepAt','enabled_accessibility_services','android_id','vpn_password']:
            with self.subTest(key=key):
                profile=copy.deepcopy(p.DEFAULT);profile['settings'][key]='value'
                with self.assertRaises(e.Problem):p.validate(profile)
    def test_extra_root_fields_rejected(self):
        profile=copy.deepcopy(p.DEFAULT);profile['credentials']={}
        with self.assertRaises(e.Problem):p.validate(profile)
    def test_types_and_values_are_not_coerced(self):
        for key,value in [('cards',True),('weather','true'),('shelf',['org.app','org.app']),('theme','other'),('latitude','nan'),('longitude','181'),('topButtons',['arbitrary']),('language','ru;reboot')]:
            profile={'schema':1,'kind':'free-my-tv-ui','settings':{key:value}}
            with self.subTest(key=key),self.assertRaises(e.Problem):p.validate(profile)
    def test_schema_bool_rejected(self):
        profile=copy.deepcopy(p.DEFAULT);profile['schema']=True
        with self.assertRaises(e.Problem):p.validate(profile)
    def test_decode_android_cursor_and_bundle(self):
        payload=base64.b64encode(json.dumps(p.DEFAULT).encode()).decode()
        for text in ['Row: 0 payload='+payload, 'Bundle[{ok=true, payload='+payload+'}]']:
            self.assertEqual(p.decode(text),p.DEFAULT)
    def test_missing_provider_requires_manual_input_not_private_data_read(self):
        class D:
            def shell(self,*args,**kw):self.args=args;return 'Error while accessing provider: io.freemytv.profile'
        d=D()
        with self.assertRaises(e.Problem) as cm:p.export(d)
        self.assertEqual(cm.exception.code,'PROFILE_UNAVAILABLE')
        self.assertEqual(d.args,('content','query','--uri',p.URI+'/export'))
    def test_apply_checks_actual_returned_values(self):
        wrong=copy.deepcopy(p.DEFAULT);wrong['settings']['theme']='light'
        class D:
            def shell(self,*args,**kw):return 'Bundle[{ok=true, payload='+base64.b64encode(json.dumps(wrong).encode()).decode()+'}]'
        with self.assertRaises(e.Problem) as cm:p.apply(D(),p.DEFAULT)
        self.assertEqual(cm.exception.code,'PROFILE_VERIFY')
    def test_apply_only_calls_documented_own_provider_with_base64(self):
        class D:
            def shell(self,*args,**kw):self.args=args;return 'Bundle[{ok=true, payload='+args[-1]+'}]'
        d=D();self.assertEqual(p.apply(d,p.DEFAULT),p.DEFAULT)
        self.assertEqual(d.args[:6],('content','call','--uri',p.URI,'--method','apply'))
        self.assertEqual(json.loads(base64.b64decode(d.args[-1])),p.DEFAULT)
    def test_restore_does_not_touch_home_or_permissions(self):
        class D:
            def shell(self,*args,**kw):self.args=args;return 'Bundle[{ok=true, payload='+base64.b64encode(json.dumps(p.DEFAULT).encode()).decode()+'}]'
        d=D();self.assertEqual(p.restore(d),p.DEFAULT)
        self.assertEqual(d.args,('content','call','--uri',p.URI,'--method','restore'))


class MediaTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows folder inspection only')
    def test_real_windows_local_folder_probe_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            info=media.inspect(td,'192.168.31.232')
            self.assertTrue(info['exists']);self.assertEqual(info['kind'],'local')
            self.assertFalse(media.plan(info)['ready'])

    def test_local_intake_needs_share_first(self):
        info={'kind':'local','path':r'W:\.intake','exists':True,'computer':'PC','addresses':['192.168.31.10']}
        result=media.plan(info)
        self.assertFalse(result['ready']);self.assertNotIn('uri',result)
    def test_local_share_root_is_intake_without_duplicated_subfolder(self):
        info={'kind':'local','path':r'W:\.intake','exists':True,'computer':'PC','addresses':['192.168.31.10']}
        state={'path':r'w:\.intake','share':'TVMovies_1234abcd','account':'TVMRead_1234abcd','server':'PC'}
        result=media.plan(info,state)
        self.assertEqual(result['uri'],'smb://192.168.31.10/TVMovies_1234abcd')
        self.assertEqual(result['subfolder'],'')
        self.assertEqual(result['username'],r'PC\TVMRead_1234abcd')
        self.assertFalse(result['password_stored'])
    def test_existing_network_drive_uses_original_server(self):
        result=media.plan({'kind':'network','path':r'W:\.intake','exists':True,'remote':r'\\192.168.31.5\Movies\.intake'})
        self.assertEqual(result['server'],'192.168.31.5')
        self.assertEqual(result['share'],'Movies');self.assertEqual(result['subfolder'],'.intake')
        self.assertEqual(result['uri'],'smb://192.168.31.5/Movies/.intake')
    def test_unicode_and_reserved_path_segments_encoded(self):
        result=media.plan({'kind':'network','path':r'W:\.intake','exists':True,'remote':r'\\nas\Films & TV\Новое'})
        self.assertIn('Films%20%26%20TV',result['uri'])
        self.assertNotIn(' ',result['uri']);self.assertNotIn('@',result['uri'])
    def test_public_ip_is_not_recommended_for_smb(self):
        with self.assertRaises(e.Problem) as cm:media.unc_parts(r'\\8.8.8.8\movies')
        self.assertEqual(cm.exception.code,'SMB_PUBLIC')
    def test_traversal_and_bad_server_rejected(self):
        for path in [r'\\server\movies\..\private',r'\\user:pass@server\movies','W:\\folder',r'\\server']:
            with self.subTest(path=path),self.assertRaises(e.Problem):media.unc_parts(path)
    def test_missing_drive_and_restored_share(self):
        with self.assertRaises(e.Problem):media.plan({'kind':'local','path':r'W:\.intake','exists':False})
        result=media.plan({'kind':'local','path':r'W:\.intake','exists':True,'computer':'PC'}, {'path':r'W:\.intake','restored':True})
        self.assertFalse(result['ready'])
    def test_powershell_bom_state_reads(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'state.json';f.write_text('{"share":"Movies"}',encoding='utf-8-sig')
            self.assertEqual(e.read_json(f),{'share':'Movies'})


class WorkflowTests(unittest.TestCase):
    def test_source_launcher_is_not_updated(self):
        class D:
            def __init__(self,address,*a):self.address=address
            def diagnose(self):return {'model':'Projector','android':'9','api':28,'abis':['armeabi-v7a'],'identity':'id','free_bytes':1000000000}
            def measures(self):return {}
        with tempfile.TemporaryDirectory() as td,patch('app.Device',D),patch('app.install_app') as install:
            m=app.Manager(td,FakeTools(td));m.device_work('192.168.28.60:5555',['freemytv'],'install',False,{'protect_source':True,'source_ip':'192.168.28.60:7777'})
            install.assert_not_called();self.assertEqual(m.rows['192.168.28.60:5555']['apps']['freemytv']['status'],'skipped')
    def test_source_excluded_from_sync_even_when_port_differs(self):
        with tempfile.TemporaryDirectory() as td,patch('app.threading.Thread') as thread:
            m=app.Manager(td,FakeTools(td));m.start({'action':'sync_ui','ips':['192.168.28.60:5555','192.168.31.232'],'source_ip':'192.168.28.60:7777','apps':[]})
            args=thread.call_args.kwargs['args']
            self.assertEqual(args[0],['192.168.31.232:5555'])
    def test_failed_source_read_does_not_silently_apply_stale_profile(self):
        class D:
            def __init__(self,address,*a):self.address=address
            def diagnose(self):return {'model':'Projector','android':'9','api':28,'abis':['armeabi-v7a'],'identity':'id','free_bytes':1000000000,'packages':['org.example.player']}
            def shell(self,*a,**k):return 'org.example.player/.Main'
        with tempfile.TemporaryDirectory() as td,patch('app.Device',D),patch('profiles.export',side_effect=e.Problem('No provider',code='PROFILE_UNAVAILABLE')):
            m=app.Manager(td,FakeTools(td));m.save_manual_profile(p.DEFAULT)
            m.device_work('192.168.28.60:5555',[],'read_source',False)
            self.assertIsNone(m.source_profile)
            self.assertFalse((Path(td)/'source-profile.json').exists())
            self.assertEqual(len(list(Path(td).glob('source-profile.previous-*.json'))),1)

    def test_source_export_works_without_filling_target_fields(self):
        with tempfile.TemporaryDirectory() as td,patch('app.threading.Thread') as thread:
            m=app.Manager(td,FakeTools(td));m.start({'action':'read_source','ips':[],'source_ip':'192.168.28.60','apps':[]})
            self.assertEqual(thread.call_args.kwargs['args'][0],['192.168.28.60:5555'])
    def test_no_source_profile_does_not_apply_defaults(self):
        with tempfile.TemporaryDirectory() as td,patch('profiles.export') as export:
            m=app.Manager(td,FakeTools(td))
            with self.assertRaises(e.Problem) as cm:m.transfer_ui(object(),Path(td))
            self.assertEqual(cm.exception.code,'PROFILE_SOURCE_REQUIRED');export.assert_not_called()
    def test_manual_profile_is_validated_and_explicitly_named_manual(self):
        with tempfile.TemporaryDirectory() as td:
            m=app.Manager(td,FakeTools(td));message=m.save_manual_profile(p.DEFAULT)
            self.assertIn('ручной',message)
            self.assertEqual(e.read_json(Path(td)/'source-profile.json'),p.DEFAULT)
            bad=copy.deepcopy(p.DEFAULT);bad['settings']['vpn_password']='not-a-real-secret'
            with self.assertRaises(e.Problem):m.save_manual_profile(bad)
            self.assertEqual(e.read_json(Path(td)/'source-profile.json'),p.DEFAULT)
    def test_profile_transfer_saves_before_and_verifies_after(self):
        class D:
            address='192.168.31.232:5555'
            def packages(self):return ['llc.itdev.incy','com.google.android.youtube.tv','org.smarttube.stable']
        with tempfile.TemporaryDirectory() as td,patch('profiles.export',return_value=p.DEFAULT),patch('profiles.apply',side_effect=lambda d,v:v) as apply:
            m=app.Manager(td,FakeTools(td));m.source_profile=copy.deepcopy(p.DEFAULT)
            actual,missing=m.transfer_ui(D(),Path(td))
            self.assertEqual(missing,[]);apply.assert_called_once()
            self.assertTrue((Path(td)/'ui-before.json').exists());self.assertEqual(actual,e.read_json(Path(td)/'ui-after.json'))
    def test_network_w_drive_does_not_launch_elevation_or_reshare(self):
        with tempfile.TemporaryDirectory() as td:
            m=app.Manager(td,FakeTools(td))
            with patch('app.os.name','nt'),patch('media.inspect',return_value={'kind':'network'}),patch('app.run') as run:
                with self.assertRaises(e.Problem) as cm:m.smb({'folder':r'W:\.intake','ips':['192.168.28.60']})
                self.assertEqual(cm.exception.code,'SMB_ALREADY_NETWORK');run.assert_not_called()
    def test_incy_install_never_connects_and_header_mode_is_proxy(self):
        class D:
            def __init__(self,address,*a):self.address=address
            def diagnose(self):return {'model':'Box','android':'14','api':34,'abis':['armeabi-v7a'],'identity':'id','free_bytes':1000000000}
            def packages(self):return ['llc.itdev.incy','org.smarttube.stable','org.videolan.vlc']
            def measures(self):return {}
        with tempfile.TemporaryDirectory() as td,patch('app.Device',D),patch('app.install_app',return_value={'status':'done','message':'Verified'}):
            m=app.Manager(td,FakeTools(td));a='192.168.31.232:5555';m.device_work(a,['incy'],'install',False)
            result=m.rows[a]['apps']['incy'];self.assertEqual(result['status'],'manual');self.assertTrue(result['installation_verified'])
            report=list((Path(td)/'reports').glob('**/incy-checklist.json'))[0];data=e.read_json(report)
            self.assertFalse(data['vpn_started_by_manager']);self.assertFalse(data['routing_verified'])
            self.assertEqual(data['subscription_headers']['per-app-proxy-mode'],'proxy')
            self.assertEqual(data['subscription_headers']['per-app-proxy-list'],'org.smarttube.stable')

if __name__=='__main__':unittest.main()
