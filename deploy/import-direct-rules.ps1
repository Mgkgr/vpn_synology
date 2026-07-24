[CmdletBinding()]
param(
  [Parameter(Mandatory)]
  [ValidateNotNullOrEmpty()]
  [string]$InputPath,
  [string[]]$AdditionalRule = @(),
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $InputPath -PathType Leaf)) {
  throw "DIRECT import source is missing: $InputPath"
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$shellSource = Join-Path $projectRoot 'deploy\scripts\import-direct-rules.sh'
$normalizerSource = Join-Path $projectRoot 'deploy\scripts\normalize-direct-import.py'
foreach ($source in @($shellSource, $normalizerSource)) {
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "DIRECT import helper is missing: $source"
  }
}

$identifier = [guid]::NewGuid().ToString('N')
$temporaryInput = Join-Path ([System.IO.Path]::GetTempPath()) "vpn-dashboard-direct-import-$identifier.txt"
$remoteShell = "/tmp/vpn-dashboard-direct-import-$identifier.sh"
$remoteNormalizer = "/tmp/vpn-dashboard-direct-normalizer-$identifier.py"
$remoteInput = "/tmp/vpn-dashboard-direct-input-$identifier.txt"
$sshTarget = "$UserName@$HostName"

try {
  Copy-Item -LiteralPath $InputPath -Destination $temporaryInput -Force
  if ($AdditionalRule.Count -gt 0) {
    $append = [Environment]::NewLine + [string]::Join([Environment]::NewLine, $AdditionalRule) + [Environment]::NewLine
    [System.IO.File]::AppendAllText($temporaryInput, $append, [System.Text.UTF8Encoding]::new($false))
  }

  & scp.exe -O -P $Port $shellSource "${sshTarget}:$remoteShell"
  if ($LASTEXITCODE -ne 0) { throw 'DIRECT import shell upload failed.' }
  & scp.exe -O -P $Port $normalizerSource "${sshTarget}:$remoteNormalizer"
  if ($LASTEXITCODE -ne 0) { throw 'DIRECT import normalizer upload failed.' }
  & scp.exe -O -P $Port $temporaryInput "${sshTarget}:$remoteInput"
  if ($LASTEXITCODE -ne 0) { throw 'DIRECT import data upload failed.' }

  $remoteCommand = "chmod 700 '$remoteShell' '$remoteNormalizer' '$remoteInput'; sudo /bin/sh '$remoteShell' '$remoteNormalizer' '$remoteInput'; status=`$?; rm -f '$remoteShell' '$remoteNormalizer' '$remoteInput'; exit `$status"
  & ssh.exe -tt -p $Port $sshTarget $remoteCommand
  if ($LASTEXITCODE -ne 0) { throw 'DIRECT import failed. Existing rules were kept if Mihomo reload did not succeed.' }
}
finally {
  Remove-Item -LiteralPath $temporaryInput -Force -ErrorAction SilentlyContinue
}

Write-Output 'RESULT=success'
