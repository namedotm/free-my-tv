$ErrorActionPreference='Stop'
try {
    Set-Location -LiteralPath $PSScriptRoot
    if(-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64'){throw 'Этот комплект предназначен для Windows 10/11 x64. Для Windows ARM требуется отдельная проверка.'}
    $exe=Join-Path $PSScriptRoot 'vendor\python\python.exe'
    if(-not(Test-Path -LiteralPath $exe)) {
        Write-Host 'Подготовка переносимого Python из официального пакета Python Software Foundation...'
        $deps=Get-Content -LiteralPath 'dependencies.json' -Raw -Encoding UTF8 | ConvertFrom-Json
        New-Item -ItemType Directory -Path 'runtime' -Force | Out-Null
        $zip=Join-Path $PSScriptRoot 'runtime\python.zip'
        [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $deps.python.url -OutFile $zip
        if((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $deps.python.sha256){Remove-Item -LiteralPath $zip;throw 'SHA-256 Python не совпал; запуск отменён.'}
        $unpack=Join-Path $PSScriptRoot 'runtime\python-unpack'
        Expand-Archive -LiteralPath $zip -DestinationPath $unpack -Force
        New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'vendor') -Force | Out-Null
        Move-Item -LiteralPath (Join-Path $unpack 'tools') -Destination (Join-Path $PSScriptRoot 'vendor\python')
        Remove-Item -LiteralPath $zip
    }
    & $exe -E -s (Join-Path $PSScriptRoot 'app.py')
    if($LASTEXITCODE -ne 0){throw 'Приложение завершилось с ошибкой. Проверьте права записи в распакованную папку.'}
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Если Windows блокирует скачанный сценарий: закройте окно, откройте свойства скачанного ZIP, отметьте «Разблокировать», затем распакуйте ZIP заново. Политику организации не обходите.'
    Read-Host 'Нажмите Enter для закрытия'
    exit 1
}
