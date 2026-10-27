import copy
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import applications as a
import repair as r
import engine as e
import app

class Device:
    address='192.168.31.232:5555'
    def __init__(self):self.state=0;self.is_installed=True;self.commands=[];self.system=False;self.identity='box'
    def shell(self,*args,**kw):
        self.commands.append(args)
        if args[:3]==('pm','list','packages'):
            if '-s' in args:return 'package:com.android.settings'
            if '-3' in args:return 'package:org.example.game'
            if '-d' in args:return 'package:org.example.game' if self.is_installed and self.state in (2,3,4) else ''
            return 'package:com.android.settings\n'+('package:org.example.game' if self.is_installed or '-u' in args else '')
        if args[:2]==('dumpsys','package'):return f'userId=10100\n pkgFlags=[ HAS_CODE {"SYSTEM" if self.system else ""} ]\n User 0: installed={str(self.is_installed).lower()} hidden=false enabled={self.state}'
        if args[:2]==('pm','disable-user'):self.state=3;return 'new state: disabled-user'
        if args[:2]==('pm','enable'):self.state=1;return 'new state: enabled'
        if args[:2]==('pm','default-state'):self.state=0;return 'new state: default'
        if args[:2]==('pm','uninstall'):self.is_installed=False;return 'Success'
        if args[:2]==('cmd','package'):return 'com.android.launcher/.Home'
        return ''
    def diagnose(self):return {'identity':self.identity,'api':30,'model':'Box','packages':['io.freemytv']}
    def free_space(self):return 1024*1024*1024
    def measures(self):return {'memory':'MemAvailable: 10000 kB','process_count':40}

class ApplicationTests(unittest.TestCase):
    def test_inventory_distinguishes_system_removed_disabled(self):
        d=Device();rows={i['package']:i for i in a.inventory(d)}
        self.assertTrue(rows['com.android.settings']['protected']);self.assertFalse(rows['org.example.game']['protected'])
        d.state=3;self.assertTrue(next(i for i in a.inventory(d) if i['package']=='org.example.game')['disabled'])
        d.is_installed=False;self.assertFalse(next(i for i in a.inventory(d) if i['package']=='org.example.game')['installed'])
    def test_injection_and_protected_names_rejected(self):
        rows=a.inventory(Device())
        for names in [['com.android.settings'],['org.example.game;reboot'],['org.example.game','org.example.game'],[{}]]:
            with self.subTest(names=names),self.assertRaises(e.Problem):a.validate_selection(rows,'disable',names)
    def test_all_originally_protected_classes_remain_protected(self):
        for pkg in ['io.freemytv','llc.itdev.incy','org.videolan.vlc','net.gtvbox.videoplayer','com.mxtech.videoplayer.ad','com.example.aircast','org.example.miracast','org.example.keystone','com.google.android.gms','com.xiaomi.mitv','com.example.keyboard']:
            self.assertIsNotNone(a.PROTECTED.search(pkg),pkg)
    def test_system_reclassification_aborts_before_disable(self):
        d=Device();d.system=True
        with tempfile.TemporaryDirectory() as td,self.assertRaises(e.Problem):a.execute(d,{'api':30,'identity':'id'},'disable',['org.example.game'],td)
        self.assertFalse(any(c[:2]==('pm','disable-user') for c in d.commands))
    def test_missing_flags_and_privileged_uid_are_rejected(self):
        for text in ['User 0: installed=true enabled=0','userId=1000\npkgFlags=[ HAS_CODE ]\nUser 0: installed=true enabled=0','userId=10100\npkgFlags=[ HAS_CODE ]\nandroid.permission.BIND_VPN_SERVICE\nUser 0: installed=true enabled=0']:
            d=Device()
            with patch.object(d,'shell',return_value=text),self.assertRaises(e.Problem):a.enabled_state(d,'org.example.game')
    def test_disable_rollback_restores_exact_default_not_enable(self):
        d=Device()
        with tempfile.TemporaryDirectory() as td:
            changes=a.execute(d,{'api':30,'identity':'id'},'disable',['org.example.game'],td)
            self.assertEqual(d.state,3)
            a.rollback(d,{'api':30,'identity':'id'},changes,td)
            self.assertEqual(d.state,0);self.assertTrue(changes['entries'][0]['restored'])
            self.assertEqual(a.rollback(d,{'api':30,'identity':'id'},changes,td),[])
    def test_rollback_rejects_different_tv(self):
        with self.assertRaises(e.Problem):a.rollback(Device(),{'identity':'new'},{'identity':'old','entries':[]},'.')
    def test_partial_error_can_restore_observed_disable(self):
        d=Device();d.state=3
        changes={'identity':'id','entries':[{'package':'org.example.game','action':'disable','status':'error','before_enabled':0}]}
        with tempfile.TemporaryDirectory() as td:a.rollback(d,{'identity':'id'},changes,td)
        self.assertEqual(d.state,0)
    def test_later_state_change_not_overwritten(self):
        d=Device();d.state=2
        changes={'identity':'id','entries':[{'package':'org.example.game','action':'disable','status':'done','before_enabled':0}]}
        # enabled_state 2 and disabled flags true are not enough: preserve exact state.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(e.Problem):a.rollback(d,{'identity':'id'},changes,td)
    def test_metrics_do_not_conflate_ram_and_storage(self):
        before={'free_bytes':100,'measures':{'memory':'MemAvailable: 1024 kB','process_count':30}}
        after={'free_bytes':200,'measures':{'memory':'MemAvailable: 512 kB','process_count':31}}
        m=a.metrics(before,after);self.assertEqual(m['storage_delta_bytes'],100);self.assertEqual(m['after_available_ram'],512*1024)

class RepairTests(unittest.TestCase):
    def test_wifi_warning_is_diagnosis_not_install_failure(self):
        d=Device()
        def shell(*args,**kw):return 'cname=com.google.android.tungsten.setupwraith SSID=private' if args==('dumpsys','wifi') else 'io.freemytv/.HomeActivity'
        with patch.object(d,'shell',side_effect=shell),patch.object(r.profiles,'export',side_effect=e.Problem('old version')):
            info=r.diagnose_home(d,{'packages':['io.freemytv']})
        self.assertTrue(info['installed']);self.assertEqual(info['wifi_owner_blockers'],['com.google.android.tungsten.setupwraith'])
        self.assertNotIn('SSID',str(info));self.assertFalse(info['networks_forgotten'])
    def test_minimal_profile_keeps_shelf_and_settings(self):
        before={'settings':{'topButtons':['live','settings'],'shelf':['old.player'],'theme':'dark'}}
        with patch.object(r.profiles,'export',return_value=before),patch.object(r.profiles,'apply',return_value={}) as apply:r.network_profile(Device())
        settings=apply.call_args.args[1]['settings'];self.assertEqual(set(settings),{'language','topButtons'});self.assertIn('network',settings['topButtons'])
    def test_smb_uri_does_not_accept_credentials_or_public_ip(self):
        self.assertEqual(r.player_uri('smb://192.168.31.10/TVMovies_x'),'smb://192.168.31.10/TVMovies_x')
        for uri in ['http://192.168.1.2/x','smb://user:password@192.168.1.2/x','smb://8.8.8.8/x','smb://192.168.1.2/x/%2e%2e/secrets','smb://192.168.1.2/x?token=secret']:
            with self.subTest(uri=uri),self.assertRaises(e.Problem):r.player_uri(uri)
    def test_activity_launch_does_not_claim_authenticated_source(self):
        d=Device();d.packages=lambda:['org.videolan.vlc']
        with patch.object(d,'shell',side_effect=['org.videolan.vlc/.StartActivity','Status: ok']):result=r.handoff(d,'vlc','smb://192.168.1.2/Movies')
        self.assertEqual(result['status'],'manual');self.assertNotIn('password',result)
    def test_unsupported_player_is_manual_not_fake_success(self):
        d=Device();d.packages=lambda:['net.gtvbox.videoplayer']
        with patch.object(d,'shell',return_value='No activity found'):result=r.handoff(d,'vimu','smb://192.168.1.2/Movies')
        self.assertEqual(result['status'],'manual')
    def test_missing_tcp_tool_is_unknown_not_network_failure(self):
        with patch.object(Device,'shell',return_value='TVM_TCP_RESULT:127'):self.assertEqual(r.tv_port(Device(),'192.168.31.10')['status'],'unknown')
    def test_tcp_error_is_not_authenticated_read(self):
        with patch.object(Device,'shell',return_value='TVM_TCP_RESULT:1'):self.assertEqual(r.tv_port(Device(),'192.168.31.10')['status'],'error')

class PlanTests(unittest.TestCase):
    def test_confirmed_plan_cannot_switch_address_or_be_replayed(self):
        with tempfile.TemporaryDirectory() as td,patch.object(app,'Device',return_value=Device()):
            m=app.Manager(td)
            result=m.manage_apps({'mode':'plan','address':'192.168.31.232','operation':'disable','packages':['org.example.game']})
            with self.assertRaises(e.Problem):m.manage_apps({'mode':'apply','address':'192.168.31.20','plan':result['plan']})
            with self.assertRaises(e.Problem):m.manage_apps({'mode':'apply','address':'192.168.31.232','plan':result['plan']})
    def test_plan_rechecks_enabled_state_before_any_change(self):
        d=Device()
        with tempfile.TemporaryDirectory() as td,patch.object(app,'Device',return_value=d):
            m=app.Manager(td);result=m.manage_apps({'mode':'plan','address':d.address,'operation':'disable','packages':['org.example.game']});d.state=1
            with self.assertRaises(e.Problem):m.manage_apps({'mode':'apply','address':d.address,'plan':result['plan']})
            self.assertFalse(any(c[:2]==('pm','disable-user') for c in d.commands));self.assertFalse(m.busy)


class ExtraSafetyTests(unittest.TestCase):
    def test_twins_with_no_serial_get_distinct_android_identities(self):
        class Twin:
            def __init__(self,aid):self.aid=aid
            def prop(self,key):return '' if key=='ro.serialno' else 'identical'
            def shell(self,*args):return self.aid
        self.assertNotEqual(e.Device.identity(Twin('one')),e.Device.identity(Twin('two')))
        with self.assertRaises(e.Problem):e.Device.identity(Twin('null'))
    def test_backup_failure_precedes_every_uninstall(self):
        d=Device();d.installed=lambda *args:(_ for _ in ()).throw(e.Problem('backup failed'))
        with tempfile.TemporaryDirectory() as td,self.assertRaises(e.Problem):a.execute(d,{'identity':'box','api':30},'uninstall_keep_data',['org.example.game'],td)
        self.assertFalse(any(c[:2]==('pm','uninstall') for c in d.commands))
    def test_existing_setup_never_calls_installer(self):
        with tempfile.TemporaryDirectory() as td:
            m=app.Manager(td)
            with patch.object(m,'media_info',return_value={'ready':True,'server':'192.168.31.10'}),patch.object(r,'pc_port',return_value={}),patch.object(m,'device_work') as worker,patch.object(app,'install_app') as install:
                m.work(['192.168.31.232:5555'],[],'existing_setup',False,{'ips':['192.168.31.232']})
            install.assert_not_called();worker.assert_called_once();self.assertFalse(m.busy)
    def test_stale_uac_success_cannot_trigger_tv_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            m=app.Manager(td);e.save_json(Path(td)/'smb-result.json',{'ok':True,'request_id':'old'})
            def smb(*args,**kwargs):m.smb_request_id='new'
            with patch.object(m,'media_info',return_value={'ready':False}),patch.object(m,'smb',side_effect=smb),patch.object(m,'device_work') as worker,patch.object(app.time,'monotonic',side_effect=[0,1,301]),patch.object(app.time,'sleep'):
                m.work(['192.168.31.232:5555'],[],'existing_setup',False,{})
            worker.assert_not_called();self.assertEqual(m.rows['192.168.31.232:5555']['code'],'SMB_WAIT')

class BackupBatchTests(unittest.TestCase):
    def test_all_backups_complete_before_first_package_is_removed(self):
        d=Device();rows=[{'package':p,'installed':True,'protected':False,'disabled':False} for p in ['org.example.one','org.example.two']]
        calls=[]
        def backup(pkg,path,api):
            calls.append(pkg)
            if pkg.endswith('two'):raise e.Problem('second backup failed')
            path.mkdir(parents=True);(path/'base.apk').write_bytes(b'backup-test')
            return {'package':pkg,'version':1,'certificates':['a'*64]}
        d.installed=backup
        with tempfile.TemporaryDirectory() as td,patch.object(a,'inventory',return_value=rows),patch.object(a,'enabled_state',return_value=0):
            with self.assertRaises(e.Problem):a.execute(d,{'identity':'box','api':30},'uninstall_keep_data',[i['package'] for i in rows],td)
        self.assertEqual(len(calls),2)
        self.assertFalse(any(c[:2]==('pm','uninstall') for c in d.commands))

if __name__=='__main__':unittest.main()
