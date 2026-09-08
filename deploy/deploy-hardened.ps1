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
  if ($LASTEXITCODE -ne 0) {
    # DSM may reset the SSH channel after tar has already completed. Verify on
    # a fresh connection that tar removed its source archive and the deployed
    # project has its required runtime script before continuing.
    $verification = & ssh -T -p $Port $sshTarget "test ! -e '$remoteArchive' && test -f '$ProjectDir/deploy/scripts/prepare-runtime.sh' && printf SOURCE_EXTRACTION=verified || printf SOURCE_EXTRACTION=unverified"
    $verified = $LASTEXITCODE -eq 0 -and (($verification | ForEach-Object { $_.ToString().Trim() }) -contains 'SOURCE_EXTRACTION=verified')
    if (-not $verified) { throw 'Source extraction failed.' }
    Write-Output 'SOURCE_EXTRACTION=verified_after_ssh_reset'
  } else {
    Write-Output 'SOURCE_EXTRACTION=ready'
  }
} finally {
  Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}

# DSM terminates child processes detached from an SSH session. Run the root
# wrapper in the authenticated foreground session instead; it writes a status
# marker and keeps the previous container available after a failed build.
Write-Output 'DEPLOYMENT=running (the SSH session will remain open until Docker finishes)'
& ssh -t -p $Port $sshTarget "sudo sh '$ProjectDir/deploy/scripts/prepare-runtime.sh' && sudo /bin/sh '$ProjectDir/deploy/scripts/run-dashboard-deploy.sh' '$runId'"
$deployExitCode = $LASTEXITCODE

# A transient SSH reset can happen after the remote process finishes. The
# root wrapper's status marker is therefore authoritative, not SSH's exit code.
$deadline = (Get-Date).AddMinutes(2)
$result = 'RUNNING'
while ((Get-Date) -lt $deadline) {
  $statusOutput = & ssh -T -p $Port $sshTarget "test -f '$remoteStatus' && cat '$remoteStatus' || echo RUNNING"
  if ($LASTEXITCODE -ne 0 -or -not $statusOutput) {
    Start-Sleep -Seconds 3
    continue
  }
  $result = (($statusOutput | Select-Object -Last 1).ToString()).Trim()
  if ($result -in @('SUCCESS', 'FAILED')) { break }
  Start-Sleep -Seconds 3
}

if ($result -ne 'SUCCESS') {
  & ssh -T -p $Port $sshTarget "test -f '$remoteLog' && tail -n 80 '$remoteLog' || true"
  throw "Deployment did not complete successfully (SSH exit code: $deployExitCode); the prior dashboard container was started when available."
}
& ssh -t -p $Port $sshTarget "sudo /usr/local/sbin/vpn-dashboard-status"
if ($LASTEXITCODE -ne 0) { throw 'Deployment completed, but post-deployment status check failed.' }

Write-Output 'RESULT=success'
