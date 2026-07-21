param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [string]$ProjectDir = '/volume1/docker/vpn-dashboard'
)

$ErrorActionPreference = 'Stop'
$sshTarget = "$UserName@$HostName"
$collector = "$ProjectDir/deploy/scripts/collect-host-health.sh"
$snapshot = "$ProjectDir/deploy/data/host-health.json"

# The collector is deliberately run as root only on DSM. It reads /proc, df
# and five fixed container states, then writes a non-secret JSON snapshot for
# the unprivileged dashboard user. The Task Scheduler configuration remains a
# DSM-owned setting and is described in docs/operations.md.
& ssh -t -p $Port $sshTarget "test -f '$collector' && sudo /bin/sh '$collector'"
if ($LASTEXITCODE -ne 0) {
  throw 'Initial DS923+ health snapshot failed. Check the sudo password and the Container Manager installation.'
}

$metadata = & ssh -T -p $Port $sshTarget "stat -c 'SNAPSHOT_MTIME=%y`nSNAPSHOT_SIZE=%s`nSNAPSHOT_MODE=%a' '$snapshot'"
if ($LASTEXITCODE -ne 0) {
  throw 'The collector completed, but the host-health snapshot was not found.'
}

$metadata
Write-Output 'RESULT=success'
