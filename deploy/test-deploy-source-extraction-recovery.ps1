$ErrorActionPreference = 'Stop'

# Regression: Synology can reset SSH after tar has finished. A successful
# verification on a fresh connection must allow the deployment to continue.
$launcher = Join-Path $PSScriptRoot 'deploy-hardened.ps1'
$global:postDeployStatusCalls = 0

function global:git {
  $global:LASTEXITCODE = 0
}

function global:scp {
  $global:LASTEXITCODE = 0
}

function global:ssh {
  $command = [string]$args[-1]
  if ($command -like '*tar -xf*') {
    [Console]::Error.WriteLine('Connection closed by simulated Synology host.')
    $global:LASTEXITCODE = 255
    return
  }
  if ($command -like '*SOURCE_EXTRACTION=verified*') {
    Write-Output 'SOURCE_EXTRACTION=verified'
    $global:LASTEXITCODE = 0
    return
  }
  if ($command -like '*vpn-dashboard-deploy-*') {
    Write-Output 'SUCCESS'
    $global:LASTEXITCODE = 0
    return
  }
  if ($command -like '*vpn-dashboard-status*') {
    $global:postDeployStatusCalls += 1
    if ($global:postDeployStatusCalls -eq 1) {
      [Console]::Error.WriteLine('Connection closed by simulated Synology host.')
      $global:LASTEXITCODE = 255
      return
    }
    Write-Output 'CONTAINER_STATE=running'
    $global:LASTEXITCODE = 0
    return
  }

  $global:LASTEXITCODE = 0
}

try {
  $output = & $launcher -HostName 'simulated-nas' -Port 22 -UserName 'tester' 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "Launcher returned an unexpected exit code: $LASTEXITCODE"
  }
  $rendered = ($output | Out-String)
  if ($rendered -notmatch 'SOURCE_EXTRACTION=verified_after_ssh_reset') {
    throw 'Launcher did not recover after a verified source-extraction SSH reset.'
  }
  if ($rendered -notmatch 'POST_DEPLOY_STATUS=verified_after_ssh_reset') {
    throw 'Launcher did not recover after a verified post-deployment status SSH reset.'
  }
  if ($rendered -notmatch 'RESULT=success') {
    throw 'Launcher did not finish after recovering source extraction.'
  }
} finally {
  Remove-Item -LiteralPath Function:\global:git -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath Function:\global:scp -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath Function:\global:ssh -ErrorAction SilentlyContinue
  Remove-Variable -Name postDeployStatusCalls -Scope Global -ErrorAction SilentlyContinue
}

Write-Output 'DEPLOY_SOURCE_EXTRACTION_RECOVERY_TEST=ok'
