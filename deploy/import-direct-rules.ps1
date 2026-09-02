[CmdletBinding()]
param(
  [string]$InputPath = '',
  [string[]]$AdditionalRule = @(),
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [ValidateRange(30, 180)]
  [int]$CompletionTimeoutSeconds = 120,
  [string]$SshExecutable = 'ssh.exe',
  [string]$ScpExecutable = 'scp.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $InputPath -and $AdditionalRule.Count -eq 0) {
  throw 'Provide InputPath, AdditionalRule, or both.'
}
if ($InputPath -and -not (Test-Path -LiteralPath $InputPath -PathType Leaf)) {
  throw "DIRECT import source is missing: $InputPath"
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$shellSource = Join-Path $projectRoot 'deploy\scripts\import-direct-rules.sh'
$normalizerSource = Join-Path $projectRoot 'deploy\scripts\normalize-direct-import.py'
$jobSource = Join-Path $projectRoot 'deploy\scripts\run-direct-import-job.sh'
$starterSource = Join-Path $projectRoot 'deploy\scripts\start-direct-import.sh'
foreach ($source in @($shellSource, $normalizerSource, $jobSource, $starterSource)) {
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "DIRECT import helper is missing: $source"
  }
}

$identifier = [guid]::NewGuid().ToString('N')
$temporaryInput = Join-Path ([System.IO.Path]::GetTempPath()) "vpn-dashboard-direct-import-$identifier.txt"
$remoteShell = "/tmp/vpn-dashboard-direct-import-$identifier.sh"
$remoteNormalizer = "/tmp/vpn-dashboard-direct-normalizer-$identifier.py"
$remoteInput = "/tmp/vpn-dashboard-direct-input-$identifier.txt"
$remoteJob = "/tmp/vpn-dashboard-direct-job-$identifier.sh"
$remoteStarter = "/tmp/vpn-dashboard-direct-start-$identifier.sh"
$remoteStatus = "/tmp/vpn-dashboard-direct-status-$identifier.txt"
$remoteLog = "/tmp/vpn-dashboard-direct-log-$identifier.txt"
$sshTarget = "$UserName@$HostName"

try {
  if ($InputPath) {
    Copy-Item -LiteralPath $InputPath -Destination $temporaryInput -Force
  }
  else {
    [System.IO.File]::WriteAllText($temporaryInput, '', [System.Text.UTF8Encoding]::new($false))
  }
  if ($AdditionalRule.Count -gt 0) {
    $append = [Environment]::NewLine + [string]::Join([Environment]::NewLine, $AdditionalRule) + [Environment]::NewLine
    [System.IO.File]::AppendAllText($temporaryInput, $append, [System.Text.UTF8Encoding]::new($false))
  }

  foreach ($item in @(
    @{ Local = $shellSource; Remote = $remoteShell; Description = 'DIRECT import shell' },
    @{ Local = $normalizerSource; Remote = $remoteNormalizer; Description = 'DIRECT import normalizer' },
    @{ Local = $jobSource; Remote = $remoteJob; Description = 'DIRECT import job' },
    @{ Local = $starterSource; Remote = $remoteStarter; Description = 'DIRECT import starter' },
    @{ Local = $temporaryInput; Remote = $remoteInput; Description = 'DIRECT import data' }
  )) {
    & $ScpExecutable -O -P $Port $item.Local "${sshTarget}:$($item.Remote)"
    if ($LASTEXITCODE -ne 0) { throw "$($item.Description) upload failed." }
  }

  $remoteCommand = "chmod 700 '$remoteShell' '$remoteNormalizer' '$remoteJob' '$remoteStarter'; chmod 600 '$remoteInput'; rm -f '$remoteStatus' '$remoteLog' '$remoteLog.pid'; sudo /bin/sh '$remoteStarter' '$remoteJob' '$remoteShell' '$remoteNormalizer' '$remoteInput' '$remoteStatus' '$remoteLog'; status=`$?; if [ `$status -eq 0 ]; then echo DIRECT_IMPORT=started; fi; exit `$status"
  & $SshExecutable -tt -p $Port $sshTarget $remoteCommand
  if ($LASTEXITCODE -ne 0) { throw 'DIRECT import failed. Existing rules were kept if Mihomo reload did not succeed.' }

  $deadline = [DateTime]::UtcNow.AddSeconds($CompletionTimeoutSeconds)
  $completed = $false
  while ([DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
      $statusOutput = & $SshExecutable -o BatchMode=yes -o ConnectTimeout=8 -p $Port $sshTarget "if [ -f '$remoteStatus' ]; then cat '$remoteStatus'; else exit 3; fi" 2>$null
      $statusCode = $LASTEXITCODE
    }
    catch {
      $statusOutput = $null
      $statusCode = 255
    }
    Write-Verbose "DIRECT import status probe: exit=$statusCode; output=$($statusOutput | Out-String)"
    if ($statusCode -eq 0 -and $statusOutput) {
      $text = (($statusOutput | Out-String).Trim()) -replace "`r`n", "`n"
      if ($text -match '(?m)^RESULT=(success|failed)$') {
        Write-Output $text
        $completed = $true
        if ($text -match '(?m)^RESULT=success$') { break }
        throw 'DIRECT import failed. Existing rules were kept if Mihomo reload did not succeed.'
      }
    }
  }
  if (-not $completed) {
    throw 'DIRECT import did not publish a completion state before the timeout. Do not rerun it; use the status diagnostic first.'
  }
}
finally {
  Remove-Item -LiteralPath $temporaryInput -Force -ErrorAction SilentlyContinue
}

Write-Output 'RESULT=success'
