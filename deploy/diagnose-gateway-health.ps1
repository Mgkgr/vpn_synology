[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'deploy\scripts\diagnose-gateway-health.sh'
$remote = "/tmp/vpn-gateway-health-$([guid]::NewGuid().ToString('N')).sh"
$sshTarget = "$UserName@$HostName"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "Gateway health diagnostic source is missing: $source"
}

& scp.exe -O -P $Port $source "${sshTarget}:$remote"
if ($LASTEXITCODE -ne 0) { throw 'Gateway health diagnostic upload failed.' }

$remoteCommand = "chmod 700 '$remote'; sudo /bin/sh '$remote'; status=`$?; rm -f '$remote'; exit `$status"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Gateway health diagnostic failed.' }

Write-Output 'RESULT=success'
