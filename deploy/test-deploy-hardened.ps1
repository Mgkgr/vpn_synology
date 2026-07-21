$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'deploy-hardened.ps1'
$source = Get-Content -LiteralPath $scriptPath -Raw

if ($source -match 'nohup sudo -n') {
  throw 'Detached deploy must not invoke sudo -n after the interactive SSH session closes.'
}
if ($source -notmatch 'sudo /bin/sh -c') {
  throw 'Detached deploy must create the nohup child from the authenticated root shell.'
}
if ($source -notmatch 'nohup /bin/sh') {
  throw 'Detached deploy must use nohup for the root deploy wrapper.'
}

Write-Output 'DEPLOY_LAUNCHER_TEST=ok'
