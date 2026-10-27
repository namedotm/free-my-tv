"""Build a portable Windows ZIP from a strict file allowlist. No launcher rebuild."""
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile
from engine import ROOT, download, digest, extract_zip, APK_HASH, Problem, read_json

FILES = ['app.py','engine.py','index.html','style.css','ui.js','SMB.ps1','bootstrap.ps1',
         'START.bat','devices.json','dependencies.json','catalog.json','README-RU.md',
         'RESEARCH-RU.md','tests/test_manager.py','build_bundle.py']


def build():
    dep = read_json(ROOT/'dependencies.json')['python']
    package = download(dep['url'],ROOT/'data/cache/python.nupkg',dep['sha256'])
    artifact = ROOT.parent/'artifacts/FreeMyTV.apk'
    if digest(artifact) != APK_HASH:
        raise Problem('Исходный APK изменился; сборка комплекта отменена. APK не пересобирается.')
    output = ROOT.parent/'artifacts/XiaomiTVManager-Windows.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as out:
        manifest={}
        def add(name,data):
            out.writestr('XiaomiTVManager/'+name,data)
            manifest[name]=hashlib.sha256(data).hexdigest()
        for f in FILES:
            add(f,(ROOT/f).read_bytes())
        add('payload/FreeMyTV.apk',artifact.read_bytes())
        add('LICENSE-FreeMyTV.txt',(ROOT.parent/'LICENSE').read_bytes())
        with zipfile.ZipFile(package) as py:
            for name in py.namelist():
                if name.startswith('tools/') and not name.endswith('/'):
                    relative=name.removeprefix('tools/')
                    if '..' in Path(relative).parts or ':' in relative:
                        raise Problem('Некорректный путь в Python NuGet.')
                    add('vendor/python/'+relative,py.read(name))
                elif name.endswith('.nuspec'):
                    add('vendor/'+name,py.read(name))
        add('INTEGRITY.json',json.dumps(manifest,indent=2).encode())
    with zipfile.ZipFile(output) as z:
        if z.testzip() is not None:raise Problem('Повреждён ZIP.')
        assert not any('/data/' in n or n.endswith(('.keystore','.jks','.pfx')) for n in z.namelist())
    if output.stat().st_size >= 95*1024*1024:raise Problem('ZIP слишком велик для обычного GitHub Git.')
    sha=digest(output)
    (output.parent/'XiaomiTVManager-Windows.zip.sha256').write_text(sha+'  '+output.name+'\n')
    print(f'Built {output.name}: {output.stat().st_size} bytes; SHA-256 {sha}')
    return output

if __name__=='__main__':build()
