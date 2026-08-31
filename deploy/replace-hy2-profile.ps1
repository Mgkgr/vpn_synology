[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [string]$Server = 'dev.failusha.digital',
  [int]$ServerPort = 443,
  [string]$PortRange = '20000-50000'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-PlainText([Security.SecureString]$Value) {
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
  }
  finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
  }
}

function ConvertTo-PosixAssignment([string]$Name, [string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Contains("'") -or $Value.Contains("`r") -or $Value.Contains("`n")) {
    throw "$Name must be a single non-empty line without apostrophes."
  }
  return "$Name='$Value'"
}

if ($Server -notmatch '^[A-Za-z0-9.-]+$') { throw 'Server contains unsupported characters.' }
if ($ServerPort -lt 1 -or $ServerPort -gt 65535) { throw 'ServerPort must be between 1 and 65535.' }
if ($PortRange -notmatch '^\d{1,5}-\d{1,5}$') { throw 'PortRange must use the form first-last.' }
$rangeParts = $PortRange -split '-'
if ([int]$rangeParts[0] -lt 1 -or [int]$rangeParts[1] -gt 65535 -or [int]$rangeParts[0] -gt [int]$rangeParts[1]) {
  throw 'PortRange must be a valid ascending UDP port range.'
}

$passwordSecure = Read-Host -AsSecureString -Prompt 'Пароль Hysteria2'
$obfsPasswordSecure = Read-Host -AsSecureString -Prompt 'Пароль obfs Salamander'
$password = $null
$obfsPassword = $null
$temporaryProfile = $null

try {
  $password = ConvertTo-PlainText $passwordSecure
  $obfsPassword = ConvertTo-PlainText $obfsPasswordSecure
  if ([string]::IsNullOrWhiteSpace($password) -or [string]::IsNullOrWhiteSpace($obfsPassword)) {
    throw 'Оба секретных значения обязательны.'
  }

  $sourceApplier = Join-Path $PSScriptRoot 'scripts\replace-hy2-profile.sh'
  foreach ($path in @($sourceApplier)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required source is missing: $path" }
  }

  $profileLines = @(
    (ConvertTo-PosixAssignment 'HY2_SERVER' $Server),
    (ConvertTo-PosixAssignment 'HY2_PORT' $ServerPort.ToString()),
    (ConvertTo-PosixAssignment 'HY2_PORTS' $PortRange),
    (ConvertTo-PosixAssignment 'HY2_PASSWORD' $password),
    (ConvertTo-PosixAssignment 'HY2_OBFS_PASSWORD' $obfsPassword),
    (ConvertTo-PosixAssignment 'HY2_SNI' $Server)
  )
  $temporaryProfile = Join-Path ([IO.Path]::GetTempPath()) ("vpn-hy2-$([guid]::NewGuid().ToString('N')).env")
  [IO.File]::WriteAllText($temporaryProfile, (($profileLines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))

  $runId = [guid]::NewGuid().ToString('N')
  $sshTarget = "$UserName@$HostName"
  $remoteApplier = "/tmp/vpn-hy2-replace-$runId.sh"
  $remoteProfile = "/tmp/vpn-hy2-profile-$runId.env"

  # DSM SSH commonly lacks the SFTP subsystem; legacy SCP is intentional.
  foreach ($item in @(
    @{ Local = $sourceApplier; Remote = $remoteApplier; Description = 'HY2 replacement script' },
    @{ Local = $temporaryProfile; Remote = $remoteProfile; Description = 'HY2 input profile' }
  )) {
    & scp.exe -O -P $Port $item.Local "${sshTarget}:$($item.Remote)"
    if ($LASTEXITCODE -ne 0) { throw "$($item.Description) upload failed." }
  }

  $remoteCommand = "chmod 700 '$remoteApplier'; chmod 600 '$remoteProfile'; sudo /bin/sh '$remoteApplier' '$remoteProfile'; status=`$?; rm -f '$remoteApplier' '$remoteProfile'; exit `$status"
  & ssh.exe -tt -p $Port $sshTarget $remoteCommand
  if ($LASTEXITCODE -ne 0) {
    throw 'HY2 replacement failed. The preceding profile was restored if validation or the authenticated delay test failed.'
  }

  Write-Output 'RESULT=success'
}
finally {
  if ($temporaryProfile -and (Test-Path -LiteralPath $temporaryProfile)) {
    [IO.File]::Delete($temporaryProfile)
  }
  $password = $null
  $obfsPassword = $null
  $passwordSecure = $null
  $obfsPasswordSecure = $null
}
