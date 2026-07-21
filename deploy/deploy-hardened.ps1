param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [string]$ProjectDir = '/volume1/docker/vpn-dashboard'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sshTarget = "$UserName@$HostName"
$archive = Join-Path ([System.IO.Path]::GetTempPath()) ("vpn-dashboard-{0}.tar" -f [guid]::NewGuid().ToString('N'))
$remoteArchive = "/tmp/$(Split-Path -Leaf $archive)"
$runId = [guid]::NewGuid().ToString('N')
$remoteStatus = "/tmp/vpn-dashboard-deploy-$runId.status"
$remoteLog = "/tmp/vpn-dashboard-deploy-$runId.log"

# git archive contains only committed files: dashboard.env, SQLite and backups remain on NAS.
& git -C $repo diff --quiet HEAD
if ($LASTEXITCODE -ne 0) { throw 'Commit or stash local changes before deployment.' }
try {
  & git -C $repo archive --format=tar --output=$archive HEAD
  if ($LASTEXITCODE -ne 0) { throw 'Could not create source archive.' }
  # Synology's SSH service may not expose the SFTP subsystem required by
  # modern OpenSSH scp; -O selects the compatible legacy SCP protocol.
  & scp -O -P $Port $archive "${sshTarget}:$remoteArchive"
  if ($LASTEXITCODE -ne 0) { throw 'Source upload failed.' }
  & ssh -p $Port $sshTarget "mkdir -p '$ProjectDir' && tar -xf '$remoteArchive' -C '$ProjectDir' && rm -f '$remoteArchive'"
  if ($LASTEXITCODE -ne 0) { throw 'Source extraction failed.' }
} finally {
  Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}

# The detached child is created by the authenticated root shell itself. A
# second `sudo -n` after SSH closes would not inherit DSM's TTY-bound timestamp.
& ssh -t -p $Port $sshTarget "sudo sh '$ProjectDir/deploy/scripts/prepare-runtime.sh' && sudo /bin/sh -c `"nohup /bin/sh '$ProjectDir/deploy/scripts/run-dashboard-deploy.sh' '$runId' >/dev/null 2>&1 &`" && echo DEPLOY_RUN_ID=$runId"
if ($LASTEXITCODE -ne 0) { throw 'Deployment preflight or detached start failed.' }

$deadline = (Get-Date).AddMinutes(15)
$result = 'RUNNING'
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 5
  $result = ((& ssh -p $Port $sshTarget "test -f '$remoteStatus' && cat '$remoteStatus' || echo RUNNING" | Select-Object -Last 1).ToString()).Trim()
  if ($LASTEXITCODE -ne 0) { continue }
  if ($result -in @('SUCCESS', 'FAILED')) { break }
}

if ($result -ne 'SUCCESS') {
  & ssh -p $Port $sshTarget "test -f '$remoteLog' && tail -n 80 '$remoteLog' || true"
  throw 'Deployment did not complete successfully; the prior dashboard container was started when available.'
}
& ssh -t -p $Port $sshTarget "sudo /usr/local/sbin/vpn-dashboard-status"
if ($LASTEXITCODE -ne 0) { throw 'Deployment completed, but post-deployment status check failed.' }

Write-Output 'RESULT=success'
