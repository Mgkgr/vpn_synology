Set-StrictMode -Version Latest

function Invoke-Hy2MigrationStatusProbe {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory)]
    [string]$SshExecutable,
    [Parameter(Mandatory)]
    [ValidateRange(1, 65535)]
    [int]$Port,
    [Parameter(Mandatory)]
    [string]$SshTarget,
    [Parameter(Mandatory)]
    [string]$RemoteStatus
  )

  $remoteCommand = "if [ -f '$RemoteStatus' ]; then cat '$RemoteStatus'; else exit 3; fi"
  try {
    # Mihomo is intentionally restarted by the migration. Its network namespace
    # can briefly reset the SSH transport, which is not a migration failure.
    $output = @(& $SshExecutable -o BatchMode=yes -o ConnectTimeout=8 -p $Port $SshTarget $remoteCommand 2>$null)
    $exitCode = if ($null -eq $LASTEXITCODE) { 255 } else { [int]$LASTEXITCODE }
    return [pscustomobject]@{
      ExitCode = $exitCode
      Output = (($output | Out-String).Trim())
    }
  }
  catch {
    return [pscustomobject]@{
      ExitCode = 255
      Output = ''
    }
  }
}
