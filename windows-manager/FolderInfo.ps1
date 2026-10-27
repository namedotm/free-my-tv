param([Parameter(Mandatory=$true)][string]$Path64,[string]$RemoteIP)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Path64))
if($path -notmatch '^[A-Za-z]:\\' -and -not $path.StartsWith('\\')){throw 'Укажите полный путь к папке Windows.'}
$kind='local';$remote=$null;$exists=Test-Path -LiteralPath $path -PathType Container
if($path.StartsWith('\\')){$kind='network';$remote=$path}
else {
    $letter=$path.Substring(0,2)
    $disk=Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='"+$letter+"'") -ErrorAction SilentlyContinue
    if($disk -and $disk.DriveType -eq 4){
        $kind='network'
        $remote=$disk.ProviderName.TrimEnd('\')+$path.Substring(2)
    } else {
        $drive=Get-PSDrive -Name $path.Substring(0,1) -ErrorAction SilentlyContinue
        if($drive.DisplayRoot -and $drive.DisplayRoot.StartsWith('\\')){$kind='network';$remote=$drive.DisplayRoot.TrimEnd('\')+$path.Substring(2)}
    }
}
$addresses=@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' } | Select-Object -ExpandProperty IPAddress)
if($RemoteIP){
    $preferred=@(Find-NetRoute -RemoteIPAddress $RemoteIP -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -and $_.IPAddress -in $addresses } | Select-Object -ExpandProperty IPAddress)
    $addresses=@(@($preferred)+@($addresses) | Select-Object -Unique)
}
@{kind=$kind;path=$path;exists=$exists;remote=$remote;computer=$env:COMPUTERNAME;addresses=$addresses} | ConvertTo-Json -Depth 4 -Compress
