param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [string]$ProjectDir = '/volume1/docker/vpn-dashboard'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sshTarget = "$UserName@$HostName"

# git archive contains only committed files: dashboard.env, SQLite and backups remain on NAS.
& git -C $repo diff --quiet HEAD
if ($LASTEXITCODE -ne 0) { throw 'Commit or stash local changes before deployment.' }
& git -C $repo archive --format=tar HEAD | & ssh -p $Port $sshTarget "mkdir -p '$ProjectDir' && tar -xf - -C '$ProjectDir'"
if ($LASTEXITCODE -ne 0) { throw 'Source upload failed.' }

# sudo asks once for the NAS password. The project deploy helper is already allow-listed.
& ssh -t -p $Port $sshTarget "sudo sh '$ProjectDir/deploy/scripts/prepare-runtime.sh' && sudo /usr/local/sbin/vpn-dashboard-deploy && sudo /usr/local/sbin/vpn-dashboard-status"
if ($LASTEXITCODE -ne 0) { throw 'Deployment or post-deployment status check failed.' }

Write-Output 'RESULT=success'
