"""Versioned non-secret Free My TV UI profile; no app-private ADB reads."""
import base64
import copy
import json
import math
import re
from engine import Problem

URI = 'content://io.freemytv.profile'
BOOLS = {'sounds','weather','quotes','photos','saverWatch'}
TEXT = {'language','theme','temperature','forecast','city','latitude','longitude'}
KEYS = BOOLS | TEXT | {'cards','shelf','topButtons'}
TOP = ['live','inputs','network','settings']
PKG = re.compile(r'[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+')
DEFAULT = {'schema':1,'kind':'free-my-tv-ui','settings':{'language':'ru','theme':'auto','temperature':'C','forecast':'hours','shelf':[],'topButtons':TOP.copy()}}


def validate(profile):
    if not isinstance(profile,dict) or set(profile)-{'schema','kind','settings','max_shelf'}:
        raise Problem('Профиль содержит неподдерживаемые поля; секреты и системные настройки не импортируются.',code='PROFILE_SCHEMA')
    if type(profile.get('schema')) is not int or profile['schema'] != 1 or profile.get('kind') != 'free-my-tv-ui':
        raise Problem('Неподдерживаемая схема профиля.',code='PROFILE_SCHEMA')
    s=profile.get('settings')
    if not isinstance(s,dict) or set(s)-KEYS:
        raise Problem('В профиле допустимы только настройки интерфейса и избранное.',code='PROFILE_FIELDS')
    for key,value in s.items():
        if key in BOOLS:
            valid=type(value) is bool
        elif key=='cards':
            valid=type(value) is int and 3<=value<=6
        elif key in ('shelf','topButtons'):
            valid=isinstance(value,list) and len(value)<=(64 if key=='shelf' else 4) and all(isinstance(v,str) for v in value)
            if valid:
                valid=len(set(value))==len(value) and all(bool(PKG.fullmatch(v)) if key=='shelf' else v in TOP for v in value)
        else:
            valid=isinstance(value,str) and len(value)<=100
            if valid and key=='language':valid=bool(re.fullmatch(r'auto|[a-z]{2}(-[A-Za-z]{2})?',value))
            if valid and key=='theme':valid=value in ('auto','light','dark')
            if valid and key=='temperature':valid=value in ('auto','C','F')
            if valid and key=='forecast':valid=value in ('hours','days')
            if valid and key in ('latitude','longitude') and value:
                try: valid=math.isfinite(float(value)) and abs(float(value))<=(90 if key=='latitude' else 180)
                except ValueError:valid=False
        if not valid:raise Problem('Недопустимое значение поля профиля: '+key,code='PROFILE_VALUE')
    cap=profile.get('max_shelf',64)
    if type(cap) is not int or not 1<=cap<=64:raise Problem('Недопустимый лимит избранного.',code='PROFILE_CAPACITY')
    encoded=json.dumps(profile,ensure_ascii=False,allow_nan=False).encode()
    if len(encoded)>24000:raise Problem('Профиль слишком большой.',code='PROFILE_SIZE')
    return copy.deepcopy(profile)


def decode(output):
    match=re.search(r'\bpayload=([A-Za-z0-9+/]+={0,2})',output)
    if not match or len(match[1])>32768:
        raise Problem('Эта версия Free My TV не предоставляет экспорт UI-профиля. Для источника доступен ручной профиль; на целевой приставке нужна профильная сборка с совместимой подписью. Закрытые данные не читались.',code='PROFILE_UNAVAILABLE')
    try:return validate(json.loads(base64.b64decode(match[1],validate=True).decode('utf-8')))
    except Problem:raise
    except Exception:raise Problem('Ответ экспорта профиля повреждён.',code='PROFILE_RESPONSE') from None


def export(device):
    return decode(device.shell('content','query','--uri',URI+'/export',check=False))


def apply(device,profile):
    profile=validate(profile)
    arg=base64.b64encode(json.dumps(profile,ensure_ascii=False,allow_nan=False).encode()).decode()
    output=device.shell('content','call','--uri',URI,'--method','apply','--arg',arg)
    if 'ok=true' not in output:raise Problem('Free My TV не подтвердил применение профиля. Проверьте версию приложения; Home не менялся.',code='PROFILE_APPLY')
    actual=decode(output)
    if any(actual['settings'].get(k)!=v for k,v in profile['settings'].items()):
        raise Problem('Не все поля профиля подтвердились. Исходный профиль сохранён для восстановления.',code='PROFILE_VERIFY')
    return actual


def restore(device):
    output=device.shell('content','call','--uri',URI,'--method','restore')
    if 'ok=true' not in output:raise Problem('Восстановление UI-профиля не подтверждено.',code='PROFILE_RESTORE')
    return decode(output)


def merge(source,target,installed,incy_package='llc.itdev.incy'):
    source=validate(source);target=validate(target)
    result=copy.deepcopy(target);result['settings'].update(source['settings'])
    candidates=source['settings'].get('shelf',[])+target['settings'].get('shelf',[])+[incy_package,'com.google.android.youtube.tv','org.smarttube.stable']
    shelf=[];missing=[]
    for pkg in candidates:
        collection=shelf if pkg in installed else missing
        if pkg not in collection:collection.append(pkg)
    cap=target.get('max_shelf',64)
    if len(shelf)>cap:
        raise Problem(f'На этом устройстве лимит избранного {cap}, а после объединения получилось {len(shelf)}. Сократите список вручную; прежние элементы не удалялись.',code='PROFILE_CAPACITY')
    result['settings']['shelf']=shelf
    top=list(dict.fromkeys(result['settings'].get('topButtons',[])+['network']))
    result['settings']['topButtons']=top
    return validate(result),missing
