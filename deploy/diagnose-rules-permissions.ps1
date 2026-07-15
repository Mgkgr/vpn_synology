[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'deploy\scripts\diagnose-rules-permissions.sh'
$remote = "/tmp/vpn-dashboard-rules-$([guid]::NewGuid().ToString('N')).sh"
$target = "$UserName@$HostName`:$remote"

& scp.exe -O -P $Port $source $target
if ($LASTEXITCODE -ne 0) { throw 'Could not upload the rules diagnostic script to the NAS.' }

& ssh.exe -tt -p $Port "$UserName@$HostName" "sudo /bin/sh '$remote'; status=`$?; rm -f '$remote'; exit `$status"
if ($LASTEXITCODE -ne 0) { throw 'Rules-permission diagnostic failed on the NAS.' }
