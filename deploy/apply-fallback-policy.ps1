param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$source = Join-Path $repo 'deploy\scripts\apply-fallback-policy.sh'
$sshTarget = "$UserName@$HostName"
$remoteScript = "/tmp/vpn-gateway-fallback-$([guid]::NewGuid().ToString('N')).sh"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "Fallback policy script is missing: $source"
}

# DSM SSH commonly lacks the SFTP subsystem; legacy SCP is intentional.
& scp.exe -O -P $Port $source "${sshTarget}:$remoteScript"
if ($LASTEXITCODE -ne 0) { throw 'Fallback policy upload failed.' }

$remoteCommand = "chmod 700 '$remoteScript'; sudo /bin/sh '$remoteScript'; rc=`$?; rm -f '$remoteScript'; exit `$rc"
& ssh.exe -tt -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Fallback policy was not applied. Mihomo configuration was restored if validation reached that stage.' }

Write-Output 'RESULT=success'
