[CmdletBinding()]
param(
  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')]
  [string]$HostName = 'roaring.crazedns.ru',
  [ValidateRange(1,65535)][int]$Port = 5004,
  [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]{0,31}$')]
  [string]$UserName = 'prometei'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'scripts\classify-antidpi-support.py'
$code = [System.IO.File]::ReadAllText($source, [System.Text.Encoding]::UTF8)
# ASCII source also survives Windows PowerShell 5.1 native stdin encoding.
if ($code -match '[^\x00-\x7F]') { throw 'Preflight collector must remain ASCII-compatible UTF-8.' }
Write-Output 'ANTIDPI_PREFLIGHT=read_only (no sudo or server changes)'
$code | & ssh.exe -T -o BatchMode=yes -o ConnectTimeout=8 -o ConnectionAttempts=1 -o StrictHostKeyChecking=yes -p $Port "$UserName@$HostName" '/usr/bin/python3 -B -'
if ($LASTEXITCODE -ne 0) { throw 'Preflight unavailable; engine compatibility remains unknown. No changes were made.' }
