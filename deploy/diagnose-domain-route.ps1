[CmdletBinding()]
param(
  [Parameter(Mandatory)]
  [ValidatePattern('^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$')]
  [string]$Domain,
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [ValidateRange(0, 60)]
  [int]$WatchSeconds = 45,
  [string]$SourceAddress = '10.66.0.4'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($SourceAddress -and $SourceAddress -notmatch '^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$') {
  throw 'SourceAddress must be an IPv4 address, for example 10.66.0.4.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'deploy\scripts\diagnose-domain-route.sh'
$remote = "/tmp/vpn-domain-route-$([guid]::NewGuid().ToString('N')).sh"
$sshTarget = "$UserName@$HostName"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "Domain-route diagnostic is missing: $source"
}

& scp.exe -O -P $Port $source "${sshTarget}:$remote"
if ($LASTEXITCODE -ne 0) { throw 'Domain-route diagnostic upload failed.' }

$normalizedDomain = $Domain.TrimEnd('.').ToLowerInvariant()
$remoteCommand = "chmod 700 '$remote'; sudo /bin/sh '$remote' '$normalizedDomain' '$WatchSeconds' '$SourceAddress'; status=`$?; rm -f '$remote'; exit `$status"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Domain-route diagnostic failed.' }

Write-Output 'RESULT=success'
