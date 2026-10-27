param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$req = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
$statePath = Join-Path $req.data 'smb-state.json'
$resultPath = Join-Path $req.data 'smb-result.json'
function Save-State($s) {
    $s | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath ($statePath+'.tmp') -Encoding UTF8
    Move-Item -LiteralPath ($statePath+'.tmp') -Destination $statePath -Force
}
function Result($ok, $message) {
    @{ok=$ok;message=$message;time=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    $icon = if($ok){[Windows.Forms.MessageBoxIcon]::Information}else{[Windows.Forms.MessageBoxIcon]::Error}
    [void][Windows.Forms.MessageBox]::Show($message, 'Xiaomi TV Manager', 'OK', $icon)
}
function Restore-Owned($s) {
    if($s.id -notmatch '^[a-f0-9]{8}$' -or $s.account -ne ('TVMRead_'+$s.id) -or $s.share -ne ('TVMovies_'+$s.id) -or $s.rule -ne ('XiaomiTVManager_'+$s.id)) { throw 'Неверный журнал владения. Автоматическое восстановление отменено.' }
    $share=Get-SmbShare -Name $s.share -ErrorAction SilentlyContinue
    if($share) {
        if($share.Path -ne $s.path){throw 'Путь общего ресурса изменился. Ресурс сохранён; нужна ручная проверка.'}
        Remove-SmbShare -Name $s.share -Force
    }
    $rule=Get-NetFirewallRule -Name $s.rule -ErrorAction SilentlyContinue
    if($rule) {
        if($rule.Group -ne ('XiaomiTVManager:'+$s.id)){throw 'Правило брандмауэра изменилось. Оно сохранено.'}
        Remove-NetFirewallRule -Name $s.rule
    }
    if($s.sid) {
        if(Test-Path -LiteralPath $s.path) {
            $acl=Get-Acl -LiteralPath $s.path
            $sid=[Security.Principal.SecurityIdentifier]::new($s.sid)
            $ace=[Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow')
            # Remove only this installer's exact grant. Preserve other and later ACL changes.
            $acl.RemoveAccessRuleSpecific($ace)
            Set-Acl -LiteralPath $s.path -AclObject $acl
        }
        $user=Get-LocalUser -Name $s.account -ErrorAction SilentlyContinue
        if($user) {
            if($user.SID.Value -ne $s.sid){throw 'Учётная запись заменена другой. Она не будет удалена.'}
            Remove-LocalUser -SID $user.SID
        }
    } elseif(Get-LocalUser -Name $s.account -ErrorAction SilentlyContinue) {
        throw 'Не записан SID созданной учётной записи. Автоматическое удаление отменено.'
    }
    $s.restored=$true
    Save-State $s
}
try {
    $principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Нужны права администратора Windows. Подтвердите стандартный запрос UAC.'}
    if($req.restore) {
        if(-not(Test-Path -LiteralPath $statePath)){Result $true 'Менеджер не создавал SMB-ресурсы. Восстанавливать нечего.'; exit}
        $s=Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if($s.restored){Result $true 'Созданные менеджером ресурсы уже восстановлены.';exit}
        if([Windows.Forms.MessageBox]::Show('Удалить только созданные менеджером общий ресурс, правило брандмауэра, разрешение чтения и его учётную запись? Файлы фильмов не удаляются.', 'Восстановление', 'YesNo', 'Question', 'Button2') -ne 'Yes'){exit}
        Restore-Owned $s
        Result $true 'Ресурсы менеджера удалены. Исходные общие папки и фильмы не изменены.'
        exit
    }
    if(Test-Path -LiteralPath $statePath) {
        $old=Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if(-not $old.restored) {
            Result $true ('Уже есть ресурс '+$old.share+'. Пароль не хранится в менеджере. Для другой папки или забытого пароля сначала используйте «Восстановить настройки SMB», затем создайте ресурс заново.')
            exit
        }
    }
    $item=Get-Item -LiteralPath $req.folder -Force
    if(-not $item.PSIsContainer -or $item.FullName.StartsWith('\\') -or $item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)){throw 'Выберите обычную локальную папку, не сетевой путь и не ссылку.'}
    $path=$item.FullName.TrimEnd('\')
    $root=[IO.Path]::GetPathRoot($path)
    if($path.Length -le $root.TrimEnd('\').Length){throw 'Корень диска нельзя публиковать. Создайте отдельную папку с фильмами.'}
    foreach($protected in @($env:WINDIR,$env:ProgramFiles,${env:ProgramFiles(x86)},$env:ProgramData)) {
        if($protected -and ($path -eq $protected -or $path.StartsWith($protected+'\',[StringComparison]::OrdinalIgnoreCase))){throw 'Системную папку нельзя использовать для медиатеки.'}
    }
    # Reject junctions in ancestors too, not only the final folder.
    $ancestor=$item
    while($ancestor) {
        if($ancestor.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)){throw 'Путь проходит через ссылку или junction. Выберите обычную папку.'}
        $ancestor=$ancestor.Parent
    }
    if((Get-Volume -DriveLetter $path.Substring(0,1)).FileSystem -ne 'NTFS'){throw 'Для контролируемых разрешений требуется локальная папка на NTFS.'}
    $ips=@($req.ips)
    if($ips.Count -lt 1 -or $ips.Count -gt 3){throw 'Укажите от одного до трёх IP.'}
    foreach($ip in $ips) {
        $addr=$null
        if(-not[Net.IPAddress]::TryParse($ip,[ref]$addr) -or $addr.AddressFamily -ne 'InterNetwork'){throw 'Неверный IPv4 телевизора.'}
        $b=$addr.GetAddressBytes()
        if(-not($b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or ($b[0] -eq 192 -and $b[1] -eq 168))){throw 'Разрешены только адреса домашней сети.'}
    }
    if(-not(Get-NetConnectionProfile | Where-Object NetworkCategory -eq Private)){throw 'Нет сети с профилем «Частная». В параметрах Windows выберите «Частная сеть» для своей доверенной домашней сети, затем повторите. Менеджер сам профиль не меняет.'}
    $smb=Get-SmbServerConfiguration
    if($smb.EnableSMB1Protocol){throw 'На компьютере уже включён SMB1. Менеджер его не использует. Отключите SMB1 в компонентах Windows и повторите.'}
    if(-not $smb.EnableSMB2Protocol){throw 'SMB2/SMB3 выключен на компьютере. Настройка пропущена; глобальные параметры сервера не изменялись.'}
    $id=[Guid]::NewGuid().ToString('N').Substring(0,8)
    $addresses=@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' } | Select-Object -ExpandProperty IPAddress)
    $s=[ordered]@{server=$env:COMPUTERNAME;addresses=$addresses;id=$id;account=('TVMRead_'+$id);share=('TVMovies_'+$id);rule=('XiaomiTVManager_'+$id);path=$path;sid=$null;restored=$false;sddl=(Get-Acl -LiteralPath $path).Sddl;ips=$ips}
    $f=New-Object Windows.Forms.Form
    $f.Text='SMB: только чтение';$f.Width=620;$f.Height=370;$f.StartPosition='CenterScreen'
    $label=New-Object Windows.Forms.Label;$label.SetBounds(18,18,565,110)
    $label.Text="Папка: $path`r`nУчётная запись: $($s.account)`r`nНовое правило SMB для IP: $($ips -join ', ')`r`nПридумайте и сохраните пароль (не менее 14 символов). Он понадобится в плеерах."
    $f.Controls.Add($label)
    $p=New-Object Windows.Forms.TextBox;$p.SetBounds(18,140,560,28);$p.UseSystemPasswordChar=$true;$f.Controls.Add($p)
    $p2=New-Object Windows.Forms.TextBox;$p2.SetBounds(18,190,560,28);$p2.UseSystemPasswordChar=$true;$f.Controls.Add($p2)
    $hint=New-Object Windows.Forms.Label;$hint.SetBounds(18,170,560,20);$hint.Text='Повторите пароль';$f.Controls.Add($hint)
    $ok=New-Object Windows.Forms.Button;$ok.SetBounds(18,240,270,38);$ok.Text='Создать защищённую папку';$ok.DialogResult='OK';$f.Controls.Add($ok)
    $cancel=New-Object Windows.Forms.Button;$cancel.SetBounds(310,240,270,38);$cancel.Text='Отмена';$cancel.DialogResult='Cancel';$f.Controls.Add($cancel)
    $f.CancelButton=$cancel
    do {
        if($f.ShowDialog() -ne 'OK'){exit}
        $valid=$p.Text.Length -ge 14 -and $p.Text -ceq $p2.Text
        if(-not $valid){[void][Windows.Forms.MessageBox]::Show('Пароли должны совпадать и содержать не менее 14 символов.')}
    } until($valid)
    $password=ConvertTo-SecureString $p.Text -AsPlainText -Force
    $p.Clear();$p2.Clear();$f.Dispose()
    # Persist the ownership plan before changing resources. Never store the password.
    Save-State $s
    try {
        $u=New-LocalUser -Name $s.account -Password $password -Description ('XiaomiTVManager:'+ $id) -UserMayNotChangePassword
        $s.sid=$u.SID.Value; Save-State $s
        # Standard Users group by SID works on Russian Windows too; never Administrators.
        Add-LocalGroupMember -SID ([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')) -Member $u
        $password.Dispose();$password=$null
        $acl=Get-Acl -LiteralPath $path
        $ace=[Security.AccessControl.FileSystemAccessRule]::new($u.SID,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow')
        $acl.AddAccessRule($ace);Set-Acl -LiteralPath $path -AclObject $acl
        $qualified=$env:COMPUTERNAME+'\'+$s.account
        New-SmbShare -Name $s.share -Path $path -ReadAccess $qualified -FolderEnumerationMode AccessBased -CachingMode None | Out-Null
        # Verify share ACL: no write grants, no guest access added.
        $rights=@(Get-SmbShareAccess -Name $s.share)
        if($rights.Count -ne 1 -or $rights[0].AccessRight -ne 'Read' -or $rights[0].AccountName -ne $qualified){throw 'Не подтвердились права только на чтение.'}
        New-NetFirewallRule -Name $s.rule -DisplayName ('Xiaomi TV Manager '+$id) -Group ('XiaomiTVManager:'+ $id) -Direction Inbound -Action Allow -Protocol TCP -LocalPort 445 -RemoteAddress $ips -Profile Private -EdgeTraversalPolicy Block | Out-Null
        Save-State $s
    } catch {
        try { Restore-Owned $s } catch { }
        throw 'Создание SMB не завершено. Выполнена попытка отмены созданных ресурсов. Проверьте data/smb-state.json и используйте восстановление. Проверьте политику паролей и права администратора.'
    }
    $addresses=@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress)
    $info="Готово. SMB2/3, только чтение.`r`nСервер: $env:COMPUTERNAME (IP: $($addresses -join ', '))`r`nРесурс: $($s.share)`r`nПользователь: $qualified`r`nПароль: введённый вами, менеджер его не хранит.`r`nВ VLC: Локальная сеть → SMB → сервер → ресурс. В Vimu добавьте SMB-источник вручную. Не включайте переадресацию TCP 445 на роутере."
    Result $true $info
} catch {
    Result $false $_.Exception.Message
    exit 1
} finally {
    if($password){$password.Dispose()}
}
