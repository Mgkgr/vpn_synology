[CmdletBinding()]
param(
  [ValidateRange(0, 60)]
  [int]$WatchSeconds = 45,
  [string]$ClientAddress = '10.66.0.4',
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$parsedAddress = $null
if (-not [System.Net.IPAddress]::TryParse($ClientAddress, [ref]$parsedAddress) -or
    $parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    -not $ClientAddress.StartsWith('10.66.')) {
  throw 'ClientAddress must be an IPv4 address from the 10.66.0.0/16 WireGuard range.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'deploy\scripts\diagnose-vpn-instability.sh'
$remote = "/tmp/vpn-instability-$([guid]::NewGuid().ToString('N')).sh"
$sshTarget = "$UserName@$HostName"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw 'VPN instability diagnostic source is missing.'
}

# DSM often does not expose SFTP; legacy SCP is intentional here.
& scp.exe -O -P $Port $source "${sshTarget}:$remote"
if ($LASTEXITCODE -ne 0) { throw 'VPN instability diagnostic upload failed.' }

$remoteCommand = "chmod 700 '$remote'; sudo /bin/sh '$remote' '$WatchSeconds' '$ClientAddress'; status=`$?; rm -f '$remote'; exit `$status"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'VPN instability diagnostic failed.' }

Write-Output 'RESULT=success'
