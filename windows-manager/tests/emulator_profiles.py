"""Real Android emulator checks: app profile round trip and rejection of an unrelated app UID.
Run only against a disposable emulator. Does not connect to any LAN TV.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import profiles


def command(args,timeout=120,env=None):
    result=subprocess.run([str(x) for x in args],capture_output=True,text=True,timeout=timeout,env=env)
    if result.returncode:raise RuntimeError('Emulator test tool failed: '+Path(str(args[0])).name+'\n'+result.stdout+'\n'+result.stderr)
    return result.stdout.strip()


class Emulator:
    def shell(self,*args,**kwargs):return command(['adb','shell',*args])


def probe_other_uid(sdk):
    bt=sdk/'build-tools/34.0.0';jar=sdk/'platforms/android-34/android.jar'
    with tempfile.TemporaryDirectory() as tmp:
        tmp=Path(tmp);(tmp/'classes').mkdir();(tmp/'dex').mkdir()
        manifest=tmp/'AndroidManifest.xml'
        manifest.write_text('''<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="io.freemytv.profileprobe" android:versionCode="1" android:versionName="1"><queries><provider android:authorities="io.freemytv.profile"/></queries><application android:allowBackup="false"/><instrumentation android:name=".Probe" android:targetPackage="io.freemytv.profileprobe"/></manifest>''')
        src=tmp/'Probe.java'
        src.write_text('''package io.freemytv.profileprobe;
public class Probe extends android.app.Instrumentation {
 @Override public void onCreate(android.os.Bundle b){super.onCreate(b);start();}
 @Override public void onStart(){
  boolean denied=false;
  try { getContext().getContentResolver().query(android.net.Uri.parse("content://io.freemytv.profile/export"),null,null,null,null); }
  catch(SecurityException expected){denied=true;}
  android.os.Bundle result=new android.os.Bundle();result.putString("result",denied?"ACCESS_DENIED_OK":"UNEXPECTED_ACCESS");
  finish(denied?-1:0,result);
 }
}''')
        command([bt/'aapt2','link','-o',tmp/'base.apk','-I',jar,'--manifest',manifest,'--min-sdk-version','26','--target-sdk-version','33'])
        command(['javac','-source','8','-target','8','-bootclasspath',jar,'-d',tmp/'classes',src])
        command([bt/'d8','--min-api','26','--lib',jar,'--output',tmp/'dex',tmp/'classes/io/freemytv/profileprobe/Probe.class'])
        command(['jar','uf',tmp/'base.apk','-C',tmp/'dex','classes.dex'])
        command([bt/'zipalign','-f','4',tmp/'base.apk',tmp/'aligned.apk'])
        env=dict(os.environ);env['TVM_PROBE_PASS']=secrets.token_urlsafe(24)
        command(['keytool','-genkeypair','-keystore',tmp/'probe.keystore','-alias','probe','-keyalg','RSA','-validity','2','-storepass:env','TVM_PROBE_PASS','-keypass:env','TVM_PROBE_PASS','-dname','CN=Disposable profile probe'],env=env)
        command([bt/'apksigner','sign','--ks',tmp/'probe.keystore','--ks-pass','env:TVM_PROBE_PASS','--key-pass','env:TVM_PROBE_PASS','--out',tmp/'probe.apk',tmp/'aligned.apk'],env=env)
        command(['adb','install','-r',tmp/'probe.apk'])
        output=command(['adb','shell','am','instrument','-w','io.freemytv.profileprobe/.Probe'])
        assert 'ACCESS_DENIED_OK' in output,output
        print('Unrelated Android app UID correctly denied access.')


def main():
    serials=command(['adb','devices']).splitlines()[1:]
    assert len([s for s in serials if s.strip()])==1 and any(s.startswith('emulator-') for s in serials),'Disposable emulator only'
    d=Emulator()
    def current_home():
        # --brief also includes resolver metadata; compare the actual component.
        return d.shell('cmd','package','resolve-activity','--brief','--user','0','-a','android.intent.action.MAIN','-c','android.intent.category.HOME').splitlines()[-1].strip()
    # A fresh emulator has no user-confirmed HOME preference. Android may show its
    # resolver after a second launcher is first installed; that is OS chooser behavior.
    # Establish the existing-v3 installation, then compare its actual resolved HOME
    # across the in-place update and all profile operations without assigning a role.
    initial_home=current_home()
    assert '/' in initial_home,initial_home
    old_artifact=Path(__file__).resolve().parents[2]/'artifacts/FreeMyTV.apk'
    command(['adb','install','-r',old_artifact])
    home=current_home()
    assert home==initial_home or home=='android/com.android.internal.app.ResolverActivity',('Unexpected first-install HOME',initial_home,home)
    print('Android HOME after first install:',home)
    access=d.shell('settings','get','secure','enabled_accessibility_services')
    artifact=Path(__file__).resolve().parents[2]/'artifacts/FreeMyTV-profiles.apk'
    command(['adb','install','-r',artifact])
    installed_home=current_home()
    assert home==installed_home,('In-place APK update changed the chosen Home',home,installed_home)
    before=profiles.export(d)
    profile={'schema':1,'kind':'free-my-tv-ui','settings':{'language':'ru','theme':'dark','sounds':False,'shelf':['io.freemytv'],'topButtons':['network','settings']}}
    actual=profiles.apply(d,profile)
    assert all(actual['settings'][k]==v for k,v in profile['settings'].items())
    changed={'schema':1,'kind':'free-my-tv-ui','settings':{'theme':'light'}}
    profiles.apply(d,changed)
    # Server-side validation: bypass Python validator to check the actual provider rejects it.
    bad={'schema':1,'kind':'free-my-tv-ui','settings':{'nowPlaying':True}}
    encoded=base64.b64encode(json.dumps(bad).encode()).decode()
    output=d.shell('content','call','--uri',profiles.URI,'--method','apply','--arg',encoded)
    assert 'ok=false' in output,output
    assert profiles.export(d)['settings']['theme']=='light'
    restored=profiles.restore(d)
    assert restored==before,(restored,before)
    assert profiles.restore(d)==before,'Restore must be idempotent'
    restored_home=current_home()
    assert home==restored_home,('UI profile changed the chosen Home',home,restored_home)
    assert access==d.shell('settings','get','secure','enabled_accessibility_services'),'Accessibility changed'
    probe_other_uid(Path(os.environ['ANDROID_HOME']))
    print('Real Android profile export/apply/validation/restore passed; Home and accessibility unchanged.')

if __name__=='__main__':main()
