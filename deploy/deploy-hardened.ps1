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

# git archive contains only committed files: dashboard.env, SQLite and backups remain on NAS.
& git -C $repo diff --quiet HEAD
if ($LASTEXITCODE -ne 0) { throw 'Commit or stash local changes before deployment.' }
try {
  & git -C $repo archive --format=tar --output=$archive HEAD
  if ($LASTEXITCODE -ne 0) { throw 'Could not create source archive.' }
  & scp -P $Port $archive "${sshTarget}:$remoteArchive"
  if ($LASTEXITCODE -ne 0) { throw 'Source upload failed.' }
  & ssh -p $Port $sshTarget "mkdir -p '$ProjectDir' && tar -xf '$remoteArchive' -C '$ProjectDir' && rm -f '$remoteArchive'"
  if ($LASTEXITCODE -ne 0) { throw 'Source extraction failed.' }
} finally {
  Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}

# sudo asks once for the NAS password. The project deploy helper is already allow-listed.
& ssh -t -p $Port $sshTarget "sudo sh '$ProjectDir/deploy/scripts/prepare-runtime.sh' && sudo /usr/local/sbin/vpn-dashboard-deploy && sudo /usr/local/sbin/vpn-dashboard-status"
if ($LASTEXITCODE -ne 0) { throw 'Deployment or post-deployment status check failed.' }

Write-Output 'RESULT=success'
