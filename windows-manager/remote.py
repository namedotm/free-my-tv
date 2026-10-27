"""Home-network remote control over the TV's own ADB: connect, launch, remote keys, screenshot.

No persistent agent, no auto-connect, no port forwarding: every call is one user click on this computer.
"""
import base64
from pathlib import Path
import re
import shlex
import subprocess
import time

from engine import Problem, run, target, command_problem

PKG = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+')
# Remote-control keys only; nothing that types text, wipes or reboots.
KEYS = {'up': 'KEYCODE_DPAD_UP', 'down': 'KEYCODE_DPAD_DOWN', 'left': 'KEYCODE_DPAD_LEFT', 'right': 'KEYCODE_DPAD_RIGHT',
        'ok': 'KEYCODE_DPAD_CENTER', 'back': 'KEYCODE_BACK', 'home': 'KEYCODE_HOME', 'menu': 'KEYCODE_MENU',
        'play': 'KEYCODE_MEDIA_PLAY_PAUSE', 'stop': 'KEYCODE_MEDIA_STOP', 'next': 'KEYCODE_MEDIA_NEXT',
        'prev': 'KEYCODE_MEDIA_PREVIOUS', 'vol_up': 'KEYCODE_VOLUME_UP', 'vol_down': 'KEYCODE_VOLUME_DOWN',
        'mute': 'KEYCODE_VOLUME_MUTE', 'settings': 'KEYCODE_SETTINGS', 'apps': 'KEYCODE_ALL_APPS'}
SCREENSHOT_BYTES = 12 * 1024 * 1024
ADB_STATES = {
    'device': ('ready', 'Подключено и разрешено'),
    'unauthorized': ('action', 'Ждёт разрешения: на экране ТВ подтвердите «Разрешить отладку» и отметьте «Всегда разрешать с этого компьютера»'),
    'offline': ('error', 'ADB offline: ТВ спит, перезагружается или сменил сеть'),
    'authorizing': ('action', 'ТВ проверяет ключ компьютера; подождите несколько секунд'),
    'missing': ('error', 'Нет соединения'),
}


def devices(adb):
    """`adb devices` as {address: state}."""
    listing = run([adb, 'devices'], check=False)
    return {a: s for a, s in re.findall(r'^(\S+)\s+(device|unauthorized|offline|authorizing|no permissions)\b', listing, re.M)}


def state(adb, address):
    address = target(address)
    value = devices(adb).get(address, 'missing')
    kind, text = ADB_STATES.get(value, ('error', value))
    return {'address': address, 'adb_state': value, 'kind': kind, 'message': text}


def connect(adb, address):
    address = target(address)
    output = run([adb, 'connect', address], timeout=20, check=False)
    time.sleep(.5)
    result = state(adb, address)
    if result['adb_state'] == 'missing':
        reason = 'ТВ не отвечает на этом адресе.'
        if re.search(r'refused', output, re.I):
            reason = 'ТВ отказал в соединении: на нём выключена отладка по сети или ADB слушает другой порт.'
        elif re.search(r'timed out|No route|unreachable', output, re.I):
            reason = 'Нет маршрута до ТВ: он выключен, в другой сети или его IP изменился. Роутер менеджер не настраивает.'
        result.update(kind='error', message=reason, details=output)
    return result


def disconnect(adb, address):
    address = target(address)
    output = run([adb, 'disconnect', address], timeout=15, check=False)
    return {'address': address, 'adb_state': 'missing', 'kind': 'idle',
            'message': 'Компьютер отключён от ADB этого ТВ. Разрешение отладки на ТВ сохраняется; повторное подключение — по кнопке.',
            'details': output}


def launchable(device):
    """Apps with a TV (LEANBACK) or phone launcher entry: package -> activity."""
    found = {}
    for category in ('android.intent.category.LEANBACK_LAUNCHER', 'android.intent.category.LAUNCHER'):
        text = device.shell('cmd', 'package', 'query-activities', '--brief', '-a', 'android.intent.action.MAIN', '-c', category, check=False)
        for pkg, activity in re.findall(r'^\s*([A-Za-z][\w.]*)/([\w.$]+)\s*$', text, re.M):
            found.setdefault(pkg, pkg + '/' + activity)
    return found


def launch(device, package):
    if not isinstance(package, str) or not PKG.fullmatch(package):
        raise Problem('Неверное имя пакета.', code='REMOTE_PKG')
    component = launchable(device).get(package)
    if not component:
        raise Problem('У приложения нет значка запуска на ТВ; откройте его системную карточку.', code='REMOTE_NO_LAUNCHER')
    out = device.shell('am', 'start', '-W', '-n', shlex.quote(component), timeout=60, check=False)
    if re.search(r'Error:|Exception|Permission Denial', out) or not re.search(r'^Status: ok', out, re.M):
        raise Problem('Android не подтвердил запуск приложения.', code='REMOTE_LAUNCH', details=out)
    total = re.search(r'^TotalTime: (\d+)', out, re.M)
    return {'message': f'Открыто на ТВ: {package}' + (f' ({total[1]} мс).' if total else '.'), 'component': component}


def key(device, name):
    code = KEYS.get(name)
    if not code:
        raise Problem('Неизвестная кнопка пульта.', code='REMOTE_KEY')
    out = device.shell('input', 'keyevent', code, timeout=15, check=False)
    if re.search(r'Error|Exception|not found', out, re.I):
        raise Problem('ТВ не принял нажатие.', code='REMOTE_KEY', details=out)
    return {'message': 'Нажата кнопка: ' + name}


def screenshot(device, folder):
    """PNG via `screencap -p`; protected (DRM) video shows black, which is the system's intended behaviour."""
    try:
        r = subprocess.run([str(device.tools.adb), '-s', device.address, 'exec-out', 'screencap', '-p'], capture_output=True,
                           timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise Problem('ТВ не прислал снимок экрана за 30 секунд.', code='TIMEOUT') from None
    data = r.stdout
    if r.returncode or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise command_problem(r.stderr.decode('utf-8', 'replace') or 'screencap failed', 'снимок экрана', r.returncode)
    if len(data) > SCREENSHOT_BYTES:
        raise Problem('Снимок слишком большой.', code='REMOTE_SHOT')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (time.strftime('%Y%m%d-%H%M%S') + '.png')
    path.write_bytes(data)
    return {'message': 'Снимок экрана сохранён: ' + str(path.name) + '. Защищённое видео (DRM) на снимке будет чёрным — это защита Android, а не ошибка.',
            'image': 'data:image/png;base64,' + base64.b64encode(data).decode(), 'file': path.name}
