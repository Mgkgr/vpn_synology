[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$shellSource = Join-Path $projectRoot 'deploy\scripts\enable-domain-sniffing.sh'
$pythonSource = Join-Path $projectRoot 'deploy\scripts\configure-domain-sniffing.py'
$identifier = [guid]::NewGuid().ToString('N')
$remoteShell = "/tmp/vpn-gateway-enable-sniffer-$identifier.sh"
$remotePython = "/tmp/vpn-gateway-configure-sniffer-$identifier.py"
$sshTarget = "$UserName@$HostName"

foreach ($source in @($shellSource, $pythonSource)) {
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Sniffer setup source is missing: $source"
  }
}

& scp.exe -O -P $Port $shellSource "${sshTarget}:$remoteShell"
if ($LASTEXITCODE -ne 0) { throw 'Domain-sniffer setup script upload failed.' }
& scp.exe -O -P $Port $pythonSource "${sshTarget}:$remotePython"
if ($LASTEXITCODE -ne 0) { throw 'Domain-sniffer configurator upload failed.' }

$remoteCommand = "chmod 700 '$remoteShell'; sudo /bin/sh '$remoteShell' '$remotePython'; status=`$?; rm -f '$remoteShell' '$remotePython'; exit `$status"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Domain-sniffer setup failed. The Mihomo configuration was restored if validation reached that stage.' }

Write-Output 'RESULT=success'
