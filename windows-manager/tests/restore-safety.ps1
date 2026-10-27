# Execute the actual rollback function with simulated Windows resources, not on the host.
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '..\SMB.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'SMB.ps1 syntax error'}
$definition=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Restore-Owned'},$true)
. ([scriptblock]::Create($definition.Extent.Text))
function Save-State($s){$script:saved=$true}
function Get-SmbShare {param($Name,$ErrorAction) $script:share}
function Remove-SmbShare {param($Name,[switch]$Force) $script:removedShare++}
function Get-NetFirewallRule {param($Name,$ErrorAction) $script:rule}
function Remove-NetFirewallRule {param($Name) $script:removedRule++}
function Get-LocalUser {param($Name,$ErrorAction) $script:user}
function Remove-LocalUser {param($SID) $script:removedUser++}
function Reset-Fixture {
    $script:share=$null;$script:rule=$null;$script:user=$null
    $script:removedShare=0;$script:removedRule=0;$script:removedUser=0;$script:saved=$false
    return [pscustomobject]@{id='1234abcd';account='TVMRead_1234abcd';share='TVMovies_1234abcd';rule='XiaomiTVManager_1234abcd';path=(Join-Path $env:TEMP ('TVM-absent-'+[guid]::NewGuid().ToString('N')));sid=$null;restored=$false}
}
function Expect-Blocked($s) {
    $blocked=$false
    try{Restore-Owned $s}catch{$blocked=$true}
    if(-not $blocked){throw 'Unsafe rollback accepted'}
    if($script:removedShare -or $script:removedRule -or $script:removedUser){throw 'Foreign resource was removed'}
}
$s=Reset-Fixture;Restore-Owned $s
if(-not $s.restored -or -not $script:saved){throw 'Idempotent rollback failed'}
$s=Reset-Fixture;$s.account='Administrator';Expect-Blocked $s
$s=Reset-Fixture;$script:share=[pscustomobject]@{Path='C:\another-share'};Expect-Blocked $s
$s=Reset-Fixture;$script:rule=[pscustomobject]@{Group='ForeignGroup'};Expect-Blocked $s
$s=Reset-Fixture;$s.sid='S-1-5-21-1-2-3-1001';$script:user=[pscustomobject]@{SID=[pscustomobject]@{Value='S-1-5-21-1-2-3-1002'}};Expect-Blocked $s
$s=Reset-Fixture;$script:share=[pscustomobject]@{Path=$s.path};$script:rule=[pscustomobject]@{Group='XiaomiTVManager:1234abcd'};Restore-Owned $s
if($script:removedShare -ne 1 -or $script:removedRule -ne 1 -or $script:removedUser -ne 0){throw 'Owned resources not restored correctly'}
Write-Host '6 rollback safety cases passed using simulated resources; no host SMB settings changed.'
