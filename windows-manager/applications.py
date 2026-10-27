"""Reviewed user-app operations. System packages are inventory-only (HomeMode owns them)."""
from pathlib import Path
import re
from engine import Problem, save_json, digest

PKG=re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+')
PROTECTED=re.compile(r'^(?:android|com\.android\.|com\.google\.|com\.xiaomi\.|com\.miui\.|com\.mediatek\.|com\.qualcomm\.|com\.droidlogic\.|com\.amlogic\.|com\.nvidia\.|io\.freemytv$)|cast|airplay|aircast|airscreen|airreceiver|eshare|waxrain|ionitech|miracast|bluetooth|network|wifi|ethernet|hdmi|drm|security|permission|keystone|autofocus|launcher|inputmethod|keyboard|vpn|incy|byedpi|videolan|gtvbox|mxtech|smarttube',re.I)
ACTIONS={'disable','enable','uninstall_keep_data','restore'}
LABELS={'io.freemytv':'Free My TV','org.videolan.vlc':'VLC','net.gtvbox.videoplayer':'Vimu','com.mxtech.videoplayer.ad':'MX Player','llc.itdev.incy':'INCY','org.smarttube.stable':'SmartTube','com.google.android.youtube.tv':'YouTube'}

def packages(text):
    if re.search(r'Error:|Exception|Unknown command|Permission denied',text,re.I):raise Problem('Android не предоставил полный список приложений.',code='APP_INVENTORY')
    return set(re.findall(r'^package:([A-Za-z][A-Za-z0-9_.]+)\s*$',text,re.M))

def inventory(device):
    known=packages(device.shell('pm','list','packages','-u','--user','0'))
    installed=packages(device.shell('pm','list','packages','--user','0'))
    system=packages(device.shell('pm','list','packages','-s','-u','--user','0'))
    third=packages(device.shell('pm','list','packages','-3','-u','--user','0'))
    disabled=packages(device.shell('pm','list','packages','-d','--user','0'))
    # Protect dynamically discovered HOME, accessibility, IME, device administrators too.
    home=device.shell('cmd','package','query-activities','--brief','-a','android.intent.action.MAIN','-c','android.intent.category.HOME',check=False)
    if re.search(r'Error:|Exception|Unknown command|Permission denied',home,re.I):
        raise Problem('Не удалось определить все HOME-компоненты; изменение приложений запрещено.',code='APP_PROTECTION')
    dynamic=set(re.findall(r'\b([A-Za-z][A-Za-z0-9_.]+)/[\w.$]+',home))
    for args in [('settings','get','secure','enabled_accessibility_services'),('settings','get','secure','default_input_method'),('settings','get','secure','enabled_input_methods'),('dumpsys','device_policy')]:
        value=device.shell(*args,check=False)
        if re.search(r'Permission denied|Permission Denial|SecurityException',value,re.I):
            raise Problem('Не удалось проверить критические роли; изменения запрещены.',code='APP_PROTECTION')
        dynamic.update(re.findall(r'\b([A-Za-z][A-Za-z0-9_.]+)/[\w.$]+',value))
    dynamic.update(re.findall(r'cname=([A-Za-z][A-Za-z0-9_.]+)',device.shell('dumpsys','wifi',check=False)))
    rows=[]
    for p in sorted(known|installed):
        is_system=p in system or p not in third
        protected=is_system or bool(PROTECTED.search(p)) or p in dynamic
        rows.append({'package':p,'label':LABELS.get(p,p),'installed':p in installed,'system':is_system,'disabled':p in disabled,'protected':protected,
                     'reason':'Системный или критический компонент: только просмотр' if protected else 'Стороннее приложение; необходимость определяет пользователь',
                     'candidate':any(k in p.lower() for k in ('advert','analytics','telemetry','demo')) and not protected})
    return rows

def validate_selection(rows,action,names):
    if action not in ACTIONS or not isinstance(names,list) or not 1<=len(names)<=12 or any(not isinstance(p,str) for p in names) or len(names)!=len(set(names)):
        raise Problem('Выберите от 1 до 12 приложений и действие.',code='APP_SELECTION')
    lookup={r['package']:r for r in rows};result=[]
    for p in names:
        if not isinstance(p,str) or not PKG.fullmatch(p) or p not in lookup:raise Problem('Пакет отсутствует в свежем списке.',code='APP_SELECTION')
        r=lookup[p]
        if r['protected']:raise Problem('Пакет защищён: '+p,code='APP_PROTECTED')
        if action in ('disable','enable','uninstall_keep_data') and not r['installed']:raise Problem('Приложение уже отсутствует: '+p,code='APP_STATE')
        if action=='disable' and r['disabled'] or action=='enable' and not r['disabled']:raise Problem('Состояние уже изменилось; обновите список.',code='APP_STATE')
        if action=='restore' and r['installed']:raise Problem('Приложение уже установлено.',code='APP_STATE')
        result.append(r)
    return result

def enabled_state(device,pkg):
    text=device.shell('dumpsys','package',pkg)
    m=re.search(r'User 0:.*?\benabled=(\d+)',text)
    if not m or int(m[1]) not in (0,1,2,3,4):raise Problem('Не определено исходное enabled-state.',code='APP_STATE')
    uid=re.search(r'\buserId=(\d+)',text)
    flags=re.search(r'\b(?:pkgFlags|flags)=\[([^\]\n]*)\]',text)
    if not uid or int(uid[1])<10000 or not flags or re.search(r'BIND_(?:VPN_SERVICE|ACCESSIBILITY_SERVICE|INPUT_METHOD|DEVICE_ADMIN)|android.net.VpnService|MediaRouteProviderService|MediaRoute2ProviderService',text):
        raise Problem('Критический или неизвестный тип приложения: изменение запрещено.',code='APP_PROTECTED')
    # Check flags and the per-user installation again independently of list classification.
    if not re.search(r'User 0:.*?\binstalled=true',text) or re.search(r'\b(?:pkgFlags|flags)=\[[^\]\n]*\bSYSTEM\b',text):
        raise Problem('Пакет системный или больше не установлен.',code='APP_PROTECTED')
    return int(m[1])

def execute(device,report,action,names,folder):
    rows=inventory(device);chosen=validate_selection(rows,action,names)
    # Validate all packages before the first change. Backups for uninstalls also precede changes.
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    states={r['package']:enabled_state(device,r['package']) for r in chosen} if action!='restore' else {}
    entries=[]
    for r in chosen:
        p=r['package'];entry={'package':p,'before_enabled':states.get(p),'action':action,'status':'planned'}
        if action=='uninstall_keep_data':
            backup=folder/'backups'/p
            info=device.installed(p,backup,report['api'])
            if not info:raise Problem('Не удалось сохранить APK до удаления.',code='APP_BACKUP')
            entry['parts']=[{'file':str(f.relative_to(folder)),'sha256':digest(f)} for f in sorted(backup.glob('*.apk'))]
            if not entry['parts']:raise Problem('Резервная копия APK пуста.',code='APP_BACKUP')
            entry['metadata']=info
        entries.append(entry)
    journal={'identity':report['identity'],'address':device.address,'entries':entries,'cache_cleared':False,'data_cleared':False,'system_apks_deleted':False}
    save_json(folder/'app-changes.json',journal)
    for e in entries:
        if report.get('identity_kind') and device.identity()!=report['identity']:
            raise Problem('Устройство сменилось после подготовки. Изменения остановлены.',code='APP_IDENTITY')
        p=e['package'];e['status']='pending';save_json(folder/'app-changes.json',journal)
        try:
            if action=='restore':
                out=device.shell('cmd','package','install-existing','--user','0',p)
            elif action=='uninstall_keep_data':
                out=device.shell('pm','uninstall','-k','--user','0',p)
                if not re.search(r'^Success\s*$',out,re.M):raise Problem('Android не подтвердил удаление APK.',details=out)
            else:
                out=device.shell('pm','disable-user' if action=='disable' else 'enable','--user','0',p)
            current={r['package']:r for r in inventory(device)}
            ok=(p not in current or not current[p]['installed']) if action=='uninstall_keep_data' else (p in current and current[p]['installed'] and (action=='restore' or current[p]['disabled']==(action=='disable')))
            if not ok:raise Problem('Android не подтвердил новое состояние.',code='APP_VERIFY')
            e.update(status='done',message='Состояние подтверждено на ТВ.')
        except Problem as ex:e.update(status='error',message=str(ex),code=ex.code,details=ex.details)
        save_json(folder/'app-changes.json',journal)
    return journal


def rollback(device,report,journal,folder):
    if journal.get('identity')!=report['identity']:raise Problem('Журнал принадлежит другому устройству.',code='APP_IDENTITY')
    results=[]
    for e in reversed(journal['entries']):
        if e['status']=='planned' or e.get('restored'):continue
        if report.get('identity_kind') and device.identity()!=report['identity']:
            raise Problem('Устройство сменилось во время отката.',code='APP_IDENTITY')
        p=e['package'];rows=inventory(device);lookup={r['package']:r for r in rows}
        if not PKG.fullmatch(p) or PROTECTED.search(p) or (p in lookup and lookup[p]['protected']):raise Problem('Восстановление защищённого пакета запрещено.',code='APP_PROTECTED')
        if e['action']=='uninstall_keep_data':
            if p in lookup and lookup[p]['installed']:raise Problem('Приложение уже установлено заново; автоматическая замена отменена.',code='APP_STATE')
            files=[]
            for part in e['parts']:
                f=(Path(folder)/part['file']).resolve()
                if not f.is_relative_to(Path(folder).resolve()) or digest(f)!=part['sha256']:raise Problem('Резервная копия APK повреждена.',code='APP_BACKUP')
                info=device.tools.inspect(f,report['api'])
                if info['package']!=p or info['certificates']!=e['metadata']['certificates'] or info['version']!=e['metadata']['version']:raise Problem('Версия/подпись копии не совпала.',code='APP_BACKUP')
                files.append(str(f))
            if not files:raise Problem('Резервная копия пуста.',code='APP_BACKUP')
            out=device.adb('install-multiple' if len(files)>1 else 'install','-r',*files,timeout=240)
            if not re.search(r'^Success\s*$',out,re.M):raise Problem('Android не подтвердил восстановление APK.',details=out)
        elif e['action'] in ('disable','enable'):
            if p not in lookup or not lookup[p]['installed']:raise Problem('Приложение больше не установлено.',code='APP_STATE')
            expected_disabled=e['action']=='disable'
            if enabled_state(device,p)==e['before_enabled']:
                e['restored']=True;save_json(Path(folder)/'app-changes.json',journal);results.append(p);continue
            if lookup[p]['disabled']!=expected_disabled or enabled_state(device,p)!=(3 if expected_disabled else 1):raise Problem('Состояние изменено после операции; откат отменён.',code='APP_STATE')
        else:raise Problem('Установка ранее удалённого приложения отменяется пользователем через настройки ТВ.',code='APP_MANUAL')
        state=e.get('before_enabled',0)
        cmd={0:'default-state',1:'enable',2:'disable',3:'disable-user',4:'disable-until-used'}.get(state)
        if not cmd:raise Problem('Неверное исходное состояние.',code='APP_STATE')
        device.shell('pm',cmd,'--user','0',p)
        if enabled_state(device,p)!=state:raise Problem('Откат enabled-state не подтверждён.',code='APP_VERIFY')
        e['restored']=True;save_json(Path(folder)/'app-changes.json',journal);results.append(p)
    return results


def metrics(before,after):
    def memory(m):
        v=re.search(r'^MemAvailable:\s+(\d+) kB',m.get('memory',''),re.M)
        return int(v[1])*1024 if v else None
    return {'before_free_bytes':before['free_bytes'],'after_free_bytes':after['free_bytes'],
            'storage_delta_bytes':after['free_bytes']-before['free_bytes'],
            'before_available_ram':memory(before['measures']),'after_available_ram':memory(after['measures']),
            'before_processes':before['measures'].get('process_count'),'after_processes':after['measures'].get('process_count'),
            'note':'Свободное место /data и доступная ОЗУ различаются. Отключение не удаляет APK. Сохранённые данные и кэш не очищались. Изменение ОЗУ не доказывает ускорения.'}
