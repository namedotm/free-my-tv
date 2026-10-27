"""Read-only launcher diagnosis and explicit native player hand-off; no private app writes."""
import re
import shlex
import socket
import urllib.parse
from engine import Problem
import media
import profiles

PLAYERS={'vlc':['org.videolan.vlc'],'vimu':['net.gtvbox.videoplayer'],'mx':['com.mxtech.videoplayer.ad','com.mxtech.videoplayer.pro']}

def diagnose_home(device,report):
    wifi=device.shell('dumpsys','wifi',check=False)
    owners=sorted(set(re.findall(r'cname=([A-Za-z][A-Za-z0-9_.]+)',wifi)))
    home=device.shell('cmd','package','resolve-activity','--brief','--user','0','-a','android.intent.action.MAIN','-c','android.intent.category.HOME',check=False)
    components=re.findall(r'^([A-Za-z][A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)$',home,re.M)
    current=components[-1] if components else None
    blockers=[p for p in owners if p=='com.google.android.tungsten.setupwraith' or current and p==current.split('/')[0]]
    records=[]
    # Xiaomi/AOSP WifiConfiguration entries put ID and cname on one configuration
    # line; unknown firmware formats remain unparsed rather than guessed.
    for line in wifi.splitlines():
        ident=re.search(r'\bID:\s*(\d+)\b',line);owner=re.search(r'\bcname=([A-Za-z][A-Za-z0-9_.]+)',line)
        if ident and owner and owner[1] in blockers:
            row={'network_id':int(ident[1]),'owner':owner[1]}
            if row not in records:records.append(row)
    result={'owned_network_ids':records,'installed':'io.freemytv' in report['packages'],'resolved_home':current,'wifi_owner_packages':owners,'wifi_owner_blockers':blockers,
            'wifi_details_stored':False,'networks_forgotten':False,'home_changed':False,
            'explanation':'Это запрет безопасного переключения Home, а не ошибка установки APK. Переустановка Free My TV не меняет владельца сохранённой сети. Защита не обходится.',
            'steps':['Сохраните пароль Wi-Fi у себя. Не вводите его в менеджер или чат.',
                     'На время исправления предпочтительно подключите приставку по Ethernet. Забывание активной Wi-Fi-сети прервёт сетевое ADB.',
                     'На ТВ: Настройки → Сеть и Интернет → сохранённая сеть → Забыть → подключить заново из этих же настроек.',
                     'Повторите диагностику владельцев. Если блокировщик остаётся, есть другие, в том числе недоступные сейчас сохранённые сети. Не выключайте первоначальную настройку и не повторяйте переустановку.',
                     'Для скрытых сетей требуется отдельный просмотр записей на вашем Windows и выбор конкретной сети перед забыванием. Этот менеджер сети не удаляет.',
                     'После устранения всех блокировщиков отключите ADB компьютера перед ручным переключением Home на Android 9/10. Режим меняется только на ТВ.']}
    try:
        exported=profiles.export(device);result.update(profile_available=True,profile=exported)
    except Problem as e:result.update(profile_available=False,profile_error=str(e))
    return result


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
    # ACTION_VIEW is the public Android interface. A successful launch is NOT a saved
    # SMB source or authenticated access. Reject versions without an exported handler.
    args=('android.intent.action.VIEW',uri,pkg)
    resolved=device.shell('cmd','package','resolve-activity','--brief','-a',args[0],'-d',shlex.quote(uri),'-p',pkg,check=False)
    components=re.findall(r'^'+re.escape(pkg)+r'/[A-Za-z0-9_.$]+$',resolved,re.M)
    if not components:return {'status':'manual','package':pkg,'uri':uri,'message':'Эта версия плеера не объявляет обработчик SMB-адреса. Добавьте источник на ТВ по показанным параметрам; скрытая база настроек не изменялась.'}
    out=device.shell('am','start','-W','-a',args[0],'-d',shlex.quote(uri),'-p',pkg,timeout=45,check=False)
    if re.search(r'Error:|Exception|unable to resolve|Permission Denial',out,re.I) or not re.search(r'^Status: ok',out,re.M):
        return {'status':'manual','package':pkg,'uri':uri,'message':'Плеер не подтвердил открытие SMB-адреса. Настройте источник на ТВ; данные не менялись.'}
    return {'status':'manual','package':pkg,'uri':uri,'message':'SMB-адрес передан плееру. На ТВ введите учётные данные и сохраните источник, если плеер предлагает. Запуск Activity не подтверждает чтение папки или сохранение источника.'}
