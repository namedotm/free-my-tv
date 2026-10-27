"""Read-only launcher diagnosis and explicit native player hand-off; no private app writes."""
import re
import shlex
import socket
import urllib.parse
from engine import Problem
import media
import profiles

PLAYERS={'vlc':['org.videolan.vlc'],'vimu':['net.gtvbox.videoplayer'],'mx':['com.mxtech.videoplayer.ad','com.mxtech.videoplayer.pro']}

SETUP='com.google.android.tungsten.setupwraith'


def saved_networks(wifi):
    """Saved networks from `dumpsys wifi` (WifiConfigManager section): id, owner, ssid, current.

    Android 11+ prints `ID: n SSID: "..."` and, lines later in the same block, `cname=<owner>`; older builds print
    both on one line. Unknown formats return an empty list rather than a guess."""
    section=re.search(r'Configured networks Begin -+\n(.*?)\n[^\n]*Configured networks End',wifi,re.S)
    body=section[1] if section else wifi
    rows={}
    for block in re.split(r'(?m)^(?=\s*(?:, \d+=)?ID: \d+ )',body):
        ident=re.search(r'^\s*(?:, \d+=)?ID: (\d+)\b',block)
        owner=re.search(r'\bcname=([A-Za-z][A-Za-z0-9_.]*)',block)
        if not ident or not owner:continue
        ssid=re.search(r'SSID: "([^"\n]*)"',block)
        rows.setdefault(int(ident[1]),{'network_id':int(ident[1]),'owner':owner[1].split(':')[0],'ssid':ssid[1] if ssid else '',
                                       'recent':'mostRecentlyConnected=true' in block})
    current=re.search(r'mWifiInfo [^\n]*?Net ID: (-?\d+)',wifi) or re.search(r'Last selected network ID (-?\d+)',wifi)
    for r in rows.values():r['current']=bool(current and int(current[1])==r['network_id'])
    return sorted(rows.values(),key=lambda r:r['network_id'])


def adb_on_wifi(device):
    """True unless Android shows the ADB address on a non-Wi-Fi interface: unknown counts as Wi-Fi (safer)."""
    ip=getattr(device,'address','').split(':')[0]
    try:name=device.interfaces().get(ip,'')
    except (AttributeError,Problem):return True
    return not name or name.startswith('wlan')


def diagnose_home(device,report):
    wifi=device.shell('dumpsys','wifi',check=False)
    owners=sorted(set(o.split(':')[0] for o in re.findall(r'cname=([A-Za-z][A-Za-z0-9_.:]+)',wifi)))
    home=device.shell('cmd','package','resolve-activity','--brief','--user','0','-a','android.intent.action.MAIN','-c','android.intent.category.HOME',check=False)
    components=re.findall(r'^([A-Za-z][A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)$',home,re.M)
    current=components[-1] if components else None
    blockers=[p for p in owners if p==SETUP or current and p==current.split('/')[0]]
    networks=saved_networks(wifi)
    # The saved report keeps IDs and owners only: no SSID, no passwords, no raw dump.
    records=[{'network_id':n['network_id'],'owner':n['owner'],'current':n['current']} for n in networks if n['owner'] in blockers]
    on_wifi=adb_on_wifi(device)
    result={'owned_network_ids':records,'adb_over_wifi':on_wifi,'forget_supported':report.get('api',0)>=30,
            'saved_network_count':len(networks),'installed':'io.freemytv' in report['packages'],'resolved_home':current,'wifi_owner_packages':owners,'wifi_owner_blockers':blockers,
            'wifi_details_stored':False,'networks_forgotten':False,'home_changed':False,
            'explanation':'Это запрет безопасного переключения Home, а не ошибка установки APK. Переустановка Free My TV не меняет владельца сохранённой сети. Защита не обходится.',
            'steps':blocker_steps(records,on_wifi,report.get('api',0))}
    try:
        exported=profiles.export(device);result.update(profile_available=True,profile=exported)
    except Problem as e:result.update(profile_available=False,profile_error=str(e))
    return result


def blocker_steps(records,on_wifi,api):
    if not records:
        return ['Сохранённых сетей, принадлежащих первоначальной настройке, не найдено. Повторите включение Free My TV на ТВ: Free My TV → Главный экран → Free My TV.',
                'Если сообщение повторится, сохраните отчёт и пришлите его текст: формат записей этой прошивки нужно разобрать отдельно.']
    active=[r for r in records if r['current']]
    other=[r for r in records if not r['current']]
    steps=['Сохраните пароль Wi-Fi у себя. Не вводите его в менеджер или чат.']
    if other:
        steps.append('Записи '+', '.join('ID '+str(r['network_id']) for r in other)+' принадлежат первоначальной настройке и НЕ являются текущим подключением. '+
                     ('Их можно забыть в менеджере: Free My TV → «Показать сохранённые сети» → «Забыть эту сеть», сверив название. ADB не оборвётся. Если сеть понадобится, подключитесь к ней заново через Настройки — новым владельцем станут Настройки ТВ.'
                      if api>=30 else 'Эта версия Android не поддерживает забывание сети из ADB: Настройки → Сеть и Интернет → сеть → Забыть (недоступные сети там не видны).'))
    if active:
        steps.append('Текущая сеть (ID '+str(active[0]['network_id'])+') тоже принадлежит первоначальной настройке. '+
                     ('ADB идёт через эту же сеть: менеджер не забудет её (связь оборвётся). Подключите приставку кабелем Ethernet или на ТВ: Настройки → Сеть и Интернет → эта сеть → Забыть → подключиться заново.'
                      if on_wifi else 'ADB идёт не через Wi-Fi: её можно забыть, затем подключиться заново из Настроек ТВ.'))
    steps.append('Повторите диагностику: блокировщиков должно стать 0. Затем на ТВ: Free My TV → Главный экран → Free My TV. Перед этим на Android 9/10 отключите ADB компьютера.')
    steps.append('Первоначальную настройку не отключайте вручную и Free My TV не переустанавливайте: это не поможет.')
    return steps


def forget_plan(device,report):
    """Which saved networks may be forgotten from this computer, with reasons; nothing is changed here."""
    if report['api']<30:
        raise Problem('Android '+report['android']+' не поддерживает «cmd wifi forget-network». Забудьте сеть в Настройках ТВ.',code='WIFI_UNSUPPORTED')
    help_text=device.shell('cmd','wifi','help',check=False)
    if 'forget-network' not in help_text:
        raise Problem('Прошивка не поддерживает забывание сети из ADB.',code='WIFI_UNSUPPORTED')
    wifi=device.shell('dumpsys','wifi',check=False)
    networks=saved_networks(wifi)
    on_wifi=adb_on_wifi(device)
    for n in networks:
        n['blocker']=n['owner']==SETUP
        n['allowed']=n['blocker'] and not (n['current'] and on_wifi)
        n['why']=('Можно забыть: принадлежит первоначальной настройке и не используется для ADB.' if n['allowed'] else
                  'Текущая сеть, через неё идёт ADB: забыть отсюда нельзя.' if n['blocker'] else
                  'Не блокирует Free My TV (владелец: '+n['owner']+'); менеджер её не трогает.')
    return {'networks':networks,'adb_over_wifi':on_wifi}


def forget_network(device,network_id,expected):
    """Forget one reviewed network; re-reads the list first and verifies the result."""
    current={n['network_id']:n for n in forget_plan(device,device_api(device))['networks']}
    n=current.get(network_id)
    if not n or not n['allowed'] or n['owner']!=expected['owner'] or n['ssid']!=expected['ssid']:
        raise Problem('Список сетей изменился или эта сеть не разрешена к забыванию. Обновите диагностику.',code='WIFI_PLAN')
    out=device.shell('cmd','wifi','forget-network',str(network_id),timeout=30,check=False)
    if re.search(r'Exception|Error|failed',out,re.I):
        raise Problem('Android не подтвердил забывание сети.',code='WIFI_FORGET',details=out)
    left={m['network_id'] for m in saved_networks(device.shell('dumpsys','wifi',check=False))}
    if network_id in left:
        raise Problem('Сеть всё ещё в списке сохранённых. Проверьте Настройки ТВ.',code='WIFI_VERIFY')
    return {'network_id':network_id,'forgotten':True,'message':'Сеть ID '+str(network_id)+' забыта и больше не числится сохранённой. Текущее подключение не менялось.'}


def device_api(device):
    return {'api':int(device.prop('ro.build.version.sdk') or 0),'android':device.prop('ro.build.version.release')}


def network_profile(device):
    before=profiles.export(device)
    # A partial import keeps shelf and every unrelated preference unchanged.
    top=list(dict.fromkeys(before['settings']['topButtons']+['network']))
    return before,profiles.apply(device,{'schema':1,'kind':'free-my-tv-ui','settings':{'language':'ru','topButtons':top}})


def player_uri(value):
    if not isinstance(value,str) or len(value)>2000:raise Problem('Неверный адрес SMB.',code='MEDIA_URI')
    u=urllib.parse.urlsplit(value)
    if u.scheme!='smb' or u.username or u.password or u.port not in (None,445) or u.query or u.fragment:
        raise Problem('В адресе SMB не допускаются пароль, токен и внешние ссылки.',code='MEDIA_URI')
    # Same LAN restrictions and path traversal checks as the folder helper.
    pieces=[urllib.parse.unquote(p) for p in u.path.split('/') if p]
    media.unc_parts('\\\\'+(u.hostname or '')+'\\'+'\\'.join(pieces))
    if any('\x00' in p or '\\' in p or p in ('.','..') for p in pieces):raise Problem('Неверный путь SMB.',code='MEDIA_URI')
    return value


def pc_port(server):
    try:
        with socket.create_connection((server,445),timeout=3):return {'status':'reachable','message':'Windows подключился к TCP 445. Это не проверка пароля и не тест с ТВ.'}
    except OSError:return {'status':'error','message':'Windows не подключился к TCP 445: проверьте службу общего доступа, Private-профиль сети и созданное правило. Брандмауэр не отключайте.'}


def tv_port(device,server):
    # A short TCP connect only. No SMB password or shell expansion from user input.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',server):raise Problem('Некорректный SMB-сервер.')
    command='toybox nc -w 3 '+shlex.quote(server)+' 445 </dev/null >/dev/null 2>&1; result=$?; echo TVM_TCP_RESULT:$result'
    try:out=device.shell('sh','-c',shlex.quote(command),timeout=10,check=False)
    except Problem:return {'status':'unknown','message':'Не удалось проверить TCP 445 с ТВ; это не подтверждённый доступ к папке.'}
    m=re.search(r'TVM_TCP_RESULT:(\d+)',out)
    if not m or m[1] in ('126','127'):return {'status':'unknown','message':'В прошивке нет подходящего TCP-инструмента. Проверка в плеере обязательна.'}
    return {'status':'reachable' if m[1]=='0' else 'error','message':'TCP 445 доступен с ТВ; пароль/чтение файлов ещё не проверены.' if m[1]=='0' else 'ТВ не подключился к TCP 445. Проверьте маршрут между 192.168.28.x и 192.168.31.x, Private-правило Windows и IP сервера.'}


def handoff(device,player,uri):
    uri=player_uri(uri)
    if player not in PLAYERS:raise Problem('Выберите VLC, Vimu или MX.',code='MEDIA_PLAYER')
    installed=device.packages();pkg=next((p for p in PLAYERS[player] if p in installed),None)
    if not pkg:return {'status':'skipped','message':'Плеер не установлен; новые APK не устанавливались.'}
    # ACTION_VIEW is the public Android interface. Checked on the owner's TVs (VLC 3.7.1, Vimu 10.70, MX 3.x):
    # every player declares smb:// only together with a video MIME type, and none accepts a folder. So a folder
    # address cannot be handed over; a file can. A launch is NOT a saved source or an authenticated read.
    if not is_media_file(uri):
        return {'status':'manual','package':pkg,'uri':uri,'message':'Ни VLC, ни Vimu, ни MX не принимают из Android адрес ПАПКИ (проверено на ваших ТВ). Добавьте папку в плеере по показанным параметрам или нажмите «Проверить воспроизведение» — менеджер откроет в плеере конкретный файл.'}
    resolved=device.shell('cmd','package','resolve-activity','--brief','-a','android.intent.action.VIEW','-d',shlex.quote(uri),'-t','video/*','-p',pkg,check=False)
    components=re.findall(r'^\s*('+re.escape(pkg)+r'/[A-Za-z0-9_.$]+)\s*$',resolved,re.M)
    if not components:return {'status':'manual','package':pkg,'uri':uri,'message':'Эта версия плеера не объявляет обработчик SMB-файла. Добавьте источник на ТВ по показанным параметрам; скрытая база настроек не изменялась.'}
    out=device.shell('am','start','-W','-a','android.intent.action.VIEW','-d',shlex.quote(uri),'-t','video/*','-n',shlex.quote(components[-1]),timeout=45,check=False)
    if re.search(r'Error:|Exception|unable to resolve|Permission Denial',out,re.I) or not re.search(r'^Status: ok',out,re.M):
        return {'status':'manual','package':pkg,'uri':uri,'message':'Плеер не подтвердил открытие SMB-адреса. Настройте источник на ТВ; данные не менялись.'}
    return {'status':'manual','package':pkg,'uri':uri,'component':components[-1],'message':'SMB-адрес файла передан плееру. На ТВ введите учётные данные, если плеер спросит. Запуск Activity не подтверждает чтение файла или сохранение источника.'}


VIDEO=('.mkv','.mp4','.avi','.mov','.m4v','.ts','.m2ts','.webm','.wmv','.mpg','.mpeg','.flv','.3gp')


def is_media_file(uri):
    return urllib.parse.unquote(urllib.parse.urlsplit(uri).path).lower().endswith(VIDEO)


def media_sessions(text):
    """[{package, state, position}] from `dumpsys media_session` (Android 9: state=3; 14: state=PLAYING(3))."""
    sessions=[]
    for block in re.split(r'\n(?=\s+package=)',text or ''):
        pkg=re.search(r'^\s+package=([\w.]+)',block,re.M)
        st=re.search(r'state=PlaybackState \{state=(?:[A-Z_]+\()?(\d+)\)?, position=(-?\d+)',block)
        if pkg and st:sessions.append({'package':pkg[1],'state':int(st[1]),'position':int(st[2])})
    return sessions


def playing(device,pkg):
    return next((s for s in media_sessions(device.shell('dumpsys','media_session',check=False)) if s['package']==pkg and s['state']==3),None)


def play_test(device,player,uri,wait=150,poll=5,sleep=None):
    """Open one SMB video file in the player, then wait for Android to report that player PLAYING with an
    advancing position: that proves the TV read the file with valid credentials. No password passes through here."""
    import time
    sleep=sleep or time.sleep
    opened=handoff(device,player,uri)
    if 'component' not in opened:return {**opened,'playback_verified':False}
    pkg=opened['package'];first=None
    for _ in range(max(1,wait//poll)):
        sleep(poll)
        s=playing(device,pkg)
        if s and first and s['position']>first['position']:
            device.shell('input','keyevent','KEYCODE_MEDIA_PAUSE',check=False)
            return {'status':'done','package':pkg,'uri':uri,'playback_verified':True,
                    'message':'Воспроизведение подтверждено: Android сообщил, что плеер играет файл из общей папки и позиция растёт. Значит, ТВ прочитал файл с правильными учётными данными. Воспроизведение поставлено на паузу.'}
        first=s or first
    return {'status':'manual','package':pkg,'uri':uri,'playback_verified':False,
            'message':'За отведённое время Android не сообщил о воспроизведении. Посмотрите на экран ТВ: возможно, плеер ждёт логин и пароль SMB (введите их и отметьте «Запомнить»), затем повторите проверку.'}
