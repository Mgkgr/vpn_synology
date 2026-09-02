$ErrorActionPreference = 'Stop'

$launcher = Join-Path $PSScriptRoot 'import-direct-rules.ps1'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
  throw "DIRECT importer launcher is missing: $launcher"
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("vpn-direct-import-test-$([guid]::NewGuid().ToString('N'))")
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
try {
  $fakeScp = Join-Path $fixtureRoot 'fake-scp.cmd'
  $fakeSsh = Join-Path $fixtureRoot 'fake-ssh.cmd'
  $counter = Join-Path $fixtureRoot 'ssh-counter.txt'
  $trace = Join-Path $fixtureRoot 'ssh-arguments.txt'
  [IO.File]::WriteAllText($fakeScp, "@echo off`r`nexit /b 0`r`n", [Text.ASCIIEncoding]::new())
  $fakeSshBody = @'
@echo off
setlocal EnableDelayedExpansion
set "counter=__COUNTER__"
set "trace=__TRACE__"
>>"%trace%" echo %*
if not exist "%counter%" (
  >"%counter%" echo started
  echo DIRECT_IMPORT=started
  exit /b 0
)
set /p stage=<"%counter%"
if "!stage!"=="started" (
  >"%counter%" echo retried
  echo simulated connection reset 1>&2
  exit /b 255
)
echo RESULT=success
echo DIRECT_IMPORT_ADDED=1
exit /b 0
'@.Replace('__COUNTER__', $counter).Replace('__TRACE__', $trace)
  [IO.File]::WriteAllText($fakeSsh, $fakeSshBody.Replace("`n", "`r`n"), [Text.ASCIIEncoding]::new())

  $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher `
    -AdditionalRule 'DOMAIN-SUFFIX,auth.permkrai.ru,DIRECT' `
    -SshExecutable $fakeSsh `
    -ScpExecutable $fakeScp `
    -CompletionTimeoutSeconds 30
  if ($LASTEXITCODE -ne 0) {
    throw "The launcher did not tolerate a transient SSH reset: $($output | Out-String)"
  }
  $text = ($output | Out-String) -replace "`r`n", "`n"
  if ($text -notmatch '(?m)^RESULT=success$' -or $text -notmatch '(?m)^DIRECT_IMPORT_ADDED=1$') {
    throw "The launcher did not publish the completed direct-import status: $text"
  }
  $traceText = Get-Content -Raw -LiteralPath $trace
  foreach ($required in @('vpn-dashboard-direct-start-', 'vpn-dashboard-direct-job-', 'vpn-dashboard-direct-status-')) {
    if (-not $traceText.Contains($required)) {
      throw "The launcher did not hand off the import through the detached status workflow: $required"
    }
  }
}
finally {
  if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
  }
}

Write-Output 'DIRECT_IMPORT_LAUNCHER_TEST=ok'
