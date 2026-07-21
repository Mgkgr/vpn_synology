$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'deploy-hardened.ps1'
$source = Get-Content -LiteralPath $scriptPath -Raw

if ($source -match 'nohup sudo -n') {
  throw 'Detached deploy must not invoke sudo -n after the interactive SSH session closes.'
}
if ($source -notmatch 'start-dashboard-deploy\.sh') {
  throw 'Deploy must invoke the dedicated root launcher instead of nesting a shell command through SSH.'
}
if ($source -match 'sudo /bin/sh -c') {
  throw 'Deploy must not depend on nested shell quoting through PowerShell and OpenSSH.'
}

Write-Output 'DEPLOY_LAUNCHER_TEST=ok'
