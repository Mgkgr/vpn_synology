[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [ValidateRange(0, 60)]
  [int]$WatchSeconds = 30,
  [string]$SourceAddress = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($SourceAddress -and $SourceAddress -notmatch '^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$') {
  throw 'SourceAddress must be an IPv4 address, for example 10.66.0.4.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'deploy\scripts\diagnose-lan3-routing.sh'
$remote = "/tmp/vpn-gateway-lan3-diagnostic-$([guid]::NewGuid().ToString('N')).sh"
$sshTarget = "$UserName@$HostName"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "LAN3 diagnostic source is missing: $source"
}

& scp.exe -O -P $Port $source "${sshTarget}:$remote"
if ($LASTEXITCODE -ne 0) { throw 'LAN3 diagnostic upload failed.' }

$remoteCommand = "chmod 700 '$remote'; sudo /bin/sh '$remote' '$WatchSeconds' '$SourceAddress'; status=`$?; rm -f '$remote'; exit `$status"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'LAN3 routing diagnostic failed.' }

Write-Output 'RESULT=success'
