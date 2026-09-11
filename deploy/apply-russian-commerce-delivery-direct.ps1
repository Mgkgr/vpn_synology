[CmdletBinding()]
param(
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

$projectRoot = Split-Path -Parent $PSScriptRoot
$importer = Join-Path $projectRoot 'deploy\import-direct-rules.ps1'
$rules = Join-Path $projectRoot 'deploy\rules\russian-commerce-delivery-direct.txt'
foreach ($path in @($importer, $rules)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Russian-commerce DIRECT helper is missing: $path"
  }
}

& $importer `
  -InputPath $rules `
  -HostName $HostName `
  -Port $Port `
  -UserName $UserName `
  -CompletionTimeoutSeconds $CompletionTimeoutSeconds `
  -SshExecutable $SshExecutable `
  -ScpExecutable $ScpExecutable
if ($LASTEXITCODE -ne 0) {
  throw 'Russian commerce and delivery DIRECT import failed. Existing rules were kept if Mihomo reload did not succeed.'
}

Write-Output 'RUSSIAN_COMMERCE_DELIVERY_DIRECT=success'
