[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [string]$Server = 'dev.failusha.digital',
  [int]$ServerPort = 443,
  [string]$PortRange = '20000-50000',
  [ValidateRange(30, 180)]
  [int]$CompletionTimeoutSeconds = 120
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
  $sourceRenamer = Join-Path $PSScriptRoot 'scripts\rename-hy2-usa.py'
  foreach ($path in @($sourceApplier, $sourceRenamer)) {
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
  $remoteRenamer = "/tmp/vpn-hy2-rename-$runId.py"
  $remoteProfile = "/tmp/vpn-hy2-profile-$runId.env"
  $remoteStatus = "/tmp/vpn-hy2-status-$runId.txt"
  $remoteLog = "/tmp/vpn-hy2-log-$runId.txt"

  # DSM SSH commonly lacks the SFTP subsystem; legacy SCP is intentional.
  foreach ($item in @(
    @{ Local = $sourceApplier; Remote = $remoteApplier; Description = 'HY2 replacement script' },
    @{ Local = $sourceRenamer; Remote = $remoteRenamer; Description = 'HY2 migration helper' },
    @{ Local = $temporaryProfile; Remote = $remoteProfile; Description = 'HY2 input profile' }
  )) {
    & scp.exe -O -P $Port $item.Local "${sshTarget}:$($item.Remote)"
    if ($LASTEXITCODE -ne 0) { throw "$($item.Description) upload failed." }
  }

  $detachedCommand = "umask 077; nohup /bin/sh '$remoteApplier' '$remoteProfile' '$remoteRenamer' '$remoteStatus' '$remoteLog' > '$remoteLog' 2>&1 & echo `\$! > '$remoteLog.pid'"
  $remoteCommand = "chmod 700 '$remoteApplier'; chmod 700 '$remoteRenamer'; chmod 600 '$remoteProfile'; rm -f '$remoteStatus' '$remoteLog' '$remoteLog.pid'; sudo /bin/sh -c `"$detachedCommand`"; status=`$?; if [ `$status -eq 0 ]; then echo HY2_APPLY=started; fi; exit `$status"
  & ssh.exe -tt -p $Port $sshTarget $remoteCommand
  if ($LASTEXITCODE -ne 0) {
    throw 'HY2 migration could not be started.'
  }

  $deadline = [DateTime]::UtcNow.AddSeconds($CompletionTimeoutSeconds)
  $completed = $false
  while ([DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds 2
    $statusOutput = & ssh.exe -p $Port $sshTarget "if [ -f '$remoteStatus' ]; then cat '$remoteStatus'; else exit 3; fi" 2>&1
    $statusCode = $LASTEXITCODE
    if ($statusCode -eq 0 -and $statusOutput) {
      $text = ($statusOutput | Out-String).Trim()
      if ($text -match '(?m)^RESULT=(success|failed)$') {
        Write-Output $text
        $completed = $true
        if ($text -match '(?m)^RESULT=success$') { break }
        throw 'HY2 migration failed. The previous profile and configuration were restored.'
      }
    }
  }
  if (-not $completed) {
    throw 'HY2 migration did not publish a completion state before the timeout. Do not rerun it; use the status diagnostic first.'
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
