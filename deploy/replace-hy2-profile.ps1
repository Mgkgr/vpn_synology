[CmdletBinding()]
param(
  [string]$HostName = 'roaring.crazedns.ru',
  [int]$Port = 5004,
  [string]$UserName = 'prometei',
  [switch]$ImportClipboard,
  [switch]$PrepareProfileOnly,
  [string]$ProfileStorePath = (Join-Path $env:LOCALAPPDATA 'vpn-gateway\hy2-usa-profile.clixml'),
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

function Get-UriQueryValue([string]$Query, [string]$Name) {
  foreach ($part in $Query.TrimStart('?').Split('&')) {
    if (-not $part) { continue }
    $pair = $part.Split('=', 2)
    if ([Uri]::UnescapeDataString($pair[0]) -ne $Name) { continue }
    if ($pair.Count -lt 2) { return '' }
    return [Uri]::UnescapeDataString($pair[1])
  }
  return $null
}

function Get-Hy2ProfileFromClipboard {
  $clipboard = Get-Clipboard -Raw -ErrorAction Stop
  $text = [string]$clipboard
  $match = [regex]::Match($text, '(?i)hy2://[^\s]+')
  if ($match.Success) {
    $candidate = [System.Net.WebUtility]::HtmlDecode($match.Value).Trim() -replace '\\@', '@'
    try { $uri = [Uri]$candidate } catch { throw 'Clipboard Hysteria2 URI is invalid.' }
    if ($uri.Scheme -ne 'hy2' -or -not $uri.Host -or -not $uri.UserInfo -or $uri.Port -lt 1) {
      throw 'Clipboard Hysteria2 URI is incomplete.'
    }
    $obfs = Get-UriQueryValue $uri.Query 'obfs-password'
    $sni = Get-UriQueryValue $uri.Query 'sni'
    $portRange = (Get-UriQueryValue $uri.Query 'mport') -replace ':', '-'
    if ((Get-UriQueryValue $uri.Query 'obfs') -ne 'salamander' -or -not $obfs -or -not $portRange) {
      throw 'Clipboard Hysteria2 URI is missing Salamander or port-hopping settings.'
    }
    return [pscustomobject]@{
      Version = 1
      Server = $uri.Host
      ServerPort = $uri.Port
      PortRange = $portRange
      Sni = if ($sni) { $sni } else { $uri.Host }
      Password = ConvertTo-SecureString ([Uri]::UnescapeDataString($uri.UserInfo)) -AsPlainText -Force
      ObfsPassword = ConvertTo-SecureString $obfs -AsPlainText -Force
    }
  }

  $normalized = $text -replace '\\_', '_'
  $start = $normalized.IndexOf('{')
  $end = $normalized.LastIndexOf('}')
  if ($start -lt 0 -or $end -le $start) { throw 'Clipboard does not contain a Hysteria2 URI or OpenWRT outbound.' }
  try { $outbound = $normalized.Substring($start, $end - $start + 1) | ConvertFrom-Json -ErrorAction Stop } catch { throw 'Clipboard OpenWRT outbound is invalid.' }
  $ports = @($outbound.server_ports)
  $portRange = if ($ports.Count -eq 1) { ([string]$ports[0]) -replace ':', '-' } else { '' }
  if ($outbound.type -ne 'hysteria2' -or -not $outbound.server -or -not $outbound.password -or -not $outbound.tls.server_name -or $outbound.obfs.type -ne 'salamander' -or -not $outbound.obfs.password -or -not $portRange) {
    throw 'Clipboard OpenWRT outbound is incomplete.'
  }
  [pscustomobject]@{
    Version = 1
    Server = [string]$outbound.server
    ServerPort = [int]$outbound.server_port
    PortRange = $portRange
    Sni = [string]$outbound.tls.server_name
    Password = ConvertTo-SecureString ([string]$outbound.password) -AsPlainText -Force
    ObfsPassword = ConvertTo-SecureString ([string]$outbound.obfs.password) -AsPlainText -Force
  }
}

function Set-ProfileStoreAcl([string]$Path) {
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
  $acl = New-Object Security.AccessControl.FileSecurity
  $acl.SetOwner($sid)
  $acl.SetAccessRuleProtection($true, $false)
  $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow)))
  [IO.File]::SetAccessControl($Path, $acl)
}

function Save-Hy2Profile([object]$Profile, [string]$Path) {
  $directory = Split-Path -Parent $Path
  if ([string]::IsNullOrWhiteSpace($directory)) { throw 'Profile store path must include a directory.' }
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
  Export-Clixml -LiteralPath $Path -InputObject $Profile -Force
  Set-ProfileStoreAcl $Path
}

function Read-Hy2Profile([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw 'Encrypted Hysteria2 profile is absent. Copy the supplied hy2 URI and run this command once with -ImportClipboard.'
  }
  $profile = Import-Clixml -LiteralPath $Path
  foreach ($property in @('Version', 'Server', 'ServerPort', 'PortRange', 'Sni', 'Password', 'ObfsPassword')) {
    if ($null -eq $profile.$property) { throw 'Encrypted Hysteria2 profile is incomplete.' }
  }
  if ($profile.Version -ne 1 -or $profile.Password -isnot [Security.SecureString] -or $profile.ObfsPassword -isnot [Security.SecureString]) {
    throw 'Encrypted Hysteria2 profile is invalid.'
  }
  return $profile
}

if ($ImportClipboard) {
  Save-Hy2Profile (Get-Hy2ProfileFromClipboard) $ProfileStorePath
}

$storedProfile = Read-Hy2Profile $ProfileStorePath
$Server = [string]$storedProfile.Server
$ServerPort = [int]$storedProfile.ServerPort
$PortRange = [string]$storedProfile.PortRange
$Sni = [string]$storedProfile.Sni
if ($Server -notmatch '^[A-Za-z0-9.-]+$') { throw 'Encrypted profile server is invalid.' }
if ($Sni -notmatch '^[A-Za-z0-9.-]+$') { throw 'Encrypted profile SNI is invalid.' }
if ($ServerPort -lt 1 -or $ServerPort -gt 65535) { throw 'Encrypted profile port is invalid.' }
if ($PortRange -notmatch '^\d{1,5}-\d{1,5}$') { throw 'Encrypted profile port range is invalid.' }
$rangeParts = $PortRange -split '-'
if ([int]$rangeParts[0] -lt 1 -or [int]$rangeParts[1] -gt 65535 -or [int]$rangeParts[0] -gt [int]$rangeParts[1]) {
  throw 'Encrypted profile port range is invalid.'
}
if ($PrepareProfileOnly) {
  Write-Output 'HY2_PROFILE_STORE=ready'
  return
}

$passwordSecure = $storedProfile.Password
$obfsPasswordSecure = $storedProfile.ObfsPassword
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
  $sourceStarter = Join-Path $PSScriptRoot 'scripts\start-hy2-profile-replace.sh'
  $sourceStatusPolling = Join-Path $PSScriptRoot 'scripts\hy2-status-polling.ps1'
  foreach ($path in @($sourceApplier, $sourceRenamer, $sourceStarter, $sourceStatusPolling)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required source is missing: $path" }
  }
  . $sourceStatusPolling

  $profileLines = @(
    (ConvertTo-PosixAssignment 'HY2_SERVER' $Server),
    (ConvertTo-PosixAssignment 'HY2_PORT' $ServerPort.ToString()),
    (ConvertTo-PosixAssignment 'HY2_PORTS' $PortRange),
    (ConvertTo-PosixAssignment 'HY2_PASSWORD' $password),
    (ConvertTo-PosixAssignment 'HY2_OBFS_PASSWORD' $obfsPassword),
    (ConvertTo-PosixAssignment 'HY2_SNI' $Sni)
  )
  $temporaryProfile = Join-Path ([IO.Path]::GetTempPath()) ("vpn-hy2-$([guid]::NewGuid().ToString('N')).env")
  [IO.File]::WriteAllText($temporaryProfile, (($profileLines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))

  $runId = [guid]::NewGuid().ToString('N')
  $sshTarget = "$UserName@$HostName"
  $remoteApplier = "/tmp/vpn-hy2-replace-$runId.sh"
  $remoteRenamer = "/tmp/vpn-hy2-rename-$runId.py"
  $remoteStarter = "/tmp/vpn-hy2-start-$runId.sh"
  $remoteProfile = "/tmp/vpn-hy2-profile-$runId.env"
  $remoteStatus = "/tmp/vpn-hy2-status-$runId.txt"
  $remoteLog = "/tmp/vpn-hy2-log-$runId.txt"

  # DSM SSH commonly lacks the SFTP subsystem; legacy SCP is intentional.
  foreach ($item in @(
    @{ Local = $sourceApplier; Remote = $remoteApplier; Description = 'HY2 replacement script' },
    @{ Local = $sourceRenamer; Remote = $remoteRenamer; Description = 'HY2 migration helper' },
    @{ Local = $sourceStarter; Remote = $remoteStarter; Description = 'HY2 migration starter' },
    @{ Local = $temporaryProfile; Remote = $remoteProfile; Description = 'HY2 input profile' }
  )) {
    & scp.exe -O -P $Port $item.Local "${sshTarget}:$($item.Remote)"
    if ($LASTEXITCODE -ne 0) { throw "$($item.Description) upload failed." }
  }

  $remoteCommand = "chmod 700 '$remoteApplier'; chmod 700 '$remoteRenamer'; chmod 700 '$remoteStarter'; chmod 600 '$remoteProfile'; rm -f '$remoteStatus' '$remoteLog' '$remoteLog.pid'; sudo /bin/sh '$remoteStarter' '$remoteApplier' '$remoteProfile' '$remoteRenamer' '$remoteStatus' '$remoteLog'; status=`$?; if [ `$status -eq 0 ]; then echo HY2_APPLY=started; fi; exit `$status"
  & ssh.exe -tt -p $Port $sshTarget $remoteCommand
  if ($LASTEXITCODE -ne 0) {
    throw 'HY2 migration could not be started.'
  }

  $deadline = [DateTime]::UtcNow.AddSeconds($CompletionTimeoutSeconds)
  $completed = $false
  while ([DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds 2
    $statusProbe = Invoke-Hy2MigrationStatusProbe -SshExecutable 'ssh.exe' -Port $Port -SshTarget $sshTarget -RemoteStatus $remoteStatus
    $statusOutput = $statusProbe.Output
    $statusCode = $statusProbe.ExitCode
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
  $storedProfile = $null
}
