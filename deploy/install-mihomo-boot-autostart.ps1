[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'deploy\scripts\mihomo-boot-autostart.sh'
$installer = Join-Path $projectRoot 'deploy\scripts\install-mihomo-boot-autostart.sh'
$runId = [guid]::NewGuid().ToString('N')
$remoteSource = "/tmp/vpn-gateway-mihomo-boot-$runId.sh"
$remoteInstaller = "/tmp/vpn-gateway-install-mihomo-boot-$runId.sh"
$sshTarget = "$UserName@$HostName"

foreach ($path in @($source, $installer)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Required source is missing: $path"
  }
}

& scp.exe -O -P $Port $source "${sshTarget}:$remoteSource"
if ($LASTEXITCODE -ne 0) { throw 'Mihomo boot script upload failed.' }
& scp.exe -O -P $Port $installer "${sshTarget}:$remoteInstaller"
if ($LASTEXITCODE -ne 0) { throw 'Mihomo boot installer upload failed.' }

$remoteCommand = "chmod 700 '$remoteInstaller'; sudo /bin/sh '$remoteInstaller' '$remoteSource'; status=`$?; rm -f '$remoteSource' '$remoteInstaller'; exit `$status"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Mihomo boot-task script installation failed. Existing gateway containers were not changed.' }

Write-Output 'RESULT=success'
