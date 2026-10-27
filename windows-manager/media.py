"""Resolve a Windows folder to player fields; never include passwords in SMB URLs."""
import base64
import ipaddress
import json
import ntpath
import os
from pathlib import Path
import re
import urllib.parse
from engine import ROOT, Problem, run, target


def unc_parts(unc):
    if not isinstance(unc,str) or not unc.startswith('\\\\'):
        raise Problem('Windows не сообщил исходный UNC-путь сетевого диска.',code='SMB_MAPPING')
    pieces=[p for p in unc[2:].split('\\') if p]
    if len(pieces)<2 or not re.fullmatch(r'[A-Za-z0-9_.-]+',pieces[0]) or any(p in ('.','..') for p in pieces):
        raise Problem('Некорректный исходный SMB-путь.',code='SMB_MAPPING')
    try:
        ip=ipaddress.ip_address(pieces[0])
        if not (ip.version==4 and any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))):
            raise Problem('SMB допускается только в домашней сети, не через публичный IP.',code='SMB_PUBLIC')
    except ValueError:pass  # Existing local DNS server names are shown for user verification.
    return pieces[0],pieces[1],'/'.join(pieces[2:])


def plan(info,state=None):
    if not info.get('exists'):
        raise Problem('Папка недоступна текущему пользователю Windows. Подключите диск W: и проверьте W:\\.intake в Проводнике.',code='FOLDER_MISSING')
    result={'kind':info['kind'],'windows_path':info['path'],'password_stored':False}
    if info['kind']=='network':
        server,share,subfolder=unc_parts(info.get('remote'))
        user='Учётная запись только на чтение на исходном NAS/SMB-сервере'
        result['note']='W: — уже сетевой диск. Повторная публикация через этот компьютер не нужна. Подключайте плееры прямо к исходному SMB-серверу; права только на чтение задаются на нём.'
    else:
        if not state or state.get('restored') or ntpath.normcase(ntpath.normpath(state.get('path',''))) != ntpath.normcase(ntpath.normpath(info['path'])):
            return {**result,'ready':False,'note':'Это локальная папка. Нажмите «Настроить общую папку», затем снова «Как подключить в плеерах». В ТВ вводится SMB-сервер и имя ресурса, а не W:\\.intake.'}
        addresses=info.get('addresses') or state.get('addresses') or []
        server=addresses[0] if addresses else state.get('server') or info['computer']
        share=state['share'];subfolder='';user=(state.get('server') or info['computer'])+'\\'+state['account']
        result['server_options']=addresses
        result['note']='Общий ресурс указывает прямо на выбранную папку: добавлять .intake в путь плеера повторно не нужно. Выберите IP ПК, доступный обоим сегментам сети; компьютер должен быть включён.'
    path='/'.join(urllib.parse.quote(p,safe='') for p in [share]+([p for p in subfolder.split('/') if p] if subfolder else []))
    result.update(ready=True,server=server,share=share,subfolder=subfolder,username=user,port=445,uri='smb://'+server+'/'+path,
                  password_hint='Введите пароль этой SMB-учётной записи непосредственно в плеере; не оставляйте поля пустыми.',
                  vlc='VLC → Локальная сеть → Добавить сервер/новый сервер → SMB. Укажите сервер, ресурс и подпапку из полей ниже. Либо найдите сервер в Локальной сети. Введите пользователя и пароль.',
                  vimu='Vimu → Источники/Добавить источник → SMB (Windows share). Сервер, ресурс и подпапка — из полей ниже; пользователь и пароль — в плеере. Названия меню зависят от версии.',
                  mx='Если в вашей версии MX Player есть SMB2/3 — добавьте те же параметры. Если нет: X-plore из официального магазина → LAN → Добавить сервер → SMB → папка → Открыть с помощью MX Player. SMB1 не включать.')
    return result


def inspect(path, remote_ip=None):
    if os.name!='nt':raise Problem('Проверка W:\\.intake доступна на вашем Windows-компьютере.',code='WINDOWS_ONLY')
    if not isinstance(path,str) or len(path)>1000 or not (re.match(r'^[A-Za-z]:\\',path) or path.startswith('\\\\')):
        raise Problem('Укажите полный путь Windows, например W:\\.intake.',code='FOLDER_PATH')
    encoded=base64.b64encode(path.encode()).decode()
    args=['powershell.exe','-NoProfile','-File',ROOT/'FolderInfo.ps1','-Path64',encoded]
    if remote_ip:args+=['-RemoteIP',target(remote_ip).split(':')[0]]
    output=run(args)
    try:return json.loads(output.lstrip('\ufeff'))
    except Exception:raise Problem('Не удалось прочитать тип диска Windows.',code='FOLDER_INSPECT') from None
