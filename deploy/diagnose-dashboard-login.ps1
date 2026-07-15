param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$source = Join-Path $repo 'deploy\scripts\diagnose-dashboard-login.sh'
$sshTarget = "$UserName@$HostName"
$remoteScript = "/tmp/vpn-dashboard-login-diagnostic-$([guid]::NewGuid().ToString('N')).sh"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "Diagnostic script is missing: $source"
}

& scp -O -P $Port $source "${sshTarget}:$remoteScript"
if ($LASTEXITCODE -ne 0) { throw 'Diagnostic script upload failed.' }

$remoteCommand = "chmod 700 '$remoteScript'; sudo sh '$remoteScript'; rc=`$?; rm -f '$remoteScript'; exit `$rc"
& ssh -t -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Dashboard login diagnostic failed.' }

Write-Output 'RESULT=success'
