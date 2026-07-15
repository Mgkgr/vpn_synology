param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$source = Join-Path $repo 'deploy\scripts\repair-lan3-routing.sh'
$sshTarget = "$UserName@$HostName"
$remoteScript = "/tmp/vpn-gateway-repair-lan3-$([guid]::NewGuid().ToString('N')).sh"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
  throw "Repair script is missing: $source"
}

# Synology SSH may not expose SFTP; -O keeps SCP compatible with DSM.
& scp -O -P $Port $source "${sshTarget}:$remoteScript"
if ($LASTEXITCODE -ne 0) { throw 'Repair script upload failed.' }

# sudo asks once for the NAS password. The remote script always restores its
# own backups if validation or the Mihomo restart fails.
$remoteCommand = "chmod 700 '$remoteScript'; sudo sh '$remoteScript'; rc=`$?; rm -f '$remoteScript'; exit `$rc"
& ssh -t -p $Port $sshTarget $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'LAN routing repair failed. Existing configuration was restored if the remote validation reached that stage.' }

Write-Output 'RESULT=success'
