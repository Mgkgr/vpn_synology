[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $projectRoot 'deploy\gateway\compose.yaml'
$applier = Join-Path $projectRoot 'deploy\scripts\apply-gateway-healthchecks.sh'
$runId = [guid]::NewGuid().ToString('N')
$remoteCompose = "/tmp/vpn-gateway-healthchecks-$runId.yaml"
$remoteApplier = "/tmp/vpn-gateway-healthchecks-$runId.sh"
$sshTarget = "$UserName@$HostName"

foreach ($path in @($compose, $applier)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Required source is missing: $path"
  }
}

& scp.exe -O -P $Port $compose "${sshTarget}:$remoteCompose"
if ($LASTEXITCODE -ne 0) { throw 'Gateway compose upload failed.' }
& scp.exe -O -P $Port $applier "${sshTarget}:$remoteApplier"
if ($LASTEXITCODE -ne 0) { throw 'Healthcheck applier upload failed.' }

$remoteCommand = "chmod 700 '$remoteApplier'; sudo /bin/sh '$remoteApplier' '$remoteCompose'; status=`$?; rm -f '$remoteCompose' '$remoteApplier'; exit `$status"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) {
  throw 'Healthcheck deployment failed. The previous compose was restored when a post-change check failed.'
}

Write-Output 'RESULT=success'
