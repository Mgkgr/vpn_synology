[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [ValidateRange(0, 60)]
  [int]$WatchSeconds = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'deploy\scripts\diagnose-kinopoisk-routing.sh'
$remote = "/tmp/vpn-dashboard-kinopoisk-$([guid]::NewGuid().ToString('N')).sh"
$target = "$UserName@$HostName`:$remote"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "Kinopoisk diagnostic script is missing: $source"
}

& scp.exe -O -P $Port $source $target
if ($LASTEXITCODE -ne 0) { throw 'Kinopoisk diagnostic upload failed.' }

$remoteCommand = "chmod 700 '$remote'; sudo /bin/sh '$remote' '$WatchSeconds'; status=`$?; rm -f '$remote'; exit `$status"
& ssh.exe -tt -p $Port "$UserName@$HostName" $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Kinopoisk routing diagnostic failed.' }

Write-Output 'RESULT=success'
