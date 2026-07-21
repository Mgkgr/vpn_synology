$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'deploy-hardened.ps1'
$source = Get-Content -LiteralPath $scriptPath -Raw

if ($source -notmatch 'run-dashboard-deploy\.sh') {
  throw 'Deploy must invoke the status-recording root wrapper.'
}
if ($source -match 'start-dashboard-deploy\.sh') {
  throw 'Deploy must not use the detached launcher on DSM.'
}
if ($source -notmatch 'DEPLOYMENT=running') {
  throw 'Deploy must explain that the SSH session stays open during the build.'
}
if ($source -notmatch '\$deployExitCode') {
  throw 'Deploy must retain the SSH exit code only as diagnostic context.'
}
if ($source -notmatch 'remote process finishes') {
  throw 'Deployment success must be based on the remote status marker.'
}
if ($source -match 'Deployment preflight or detached start failed') {
  throw 'Deployment failure must be based on the remote status marker, not only an SSH exit code.'
}

Write-Output 'DEPLOY_LAUNCHER_TEST=ok'
