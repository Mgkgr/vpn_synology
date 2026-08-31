$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'replace-hy2-profile.ps1'
$applier = Join-Path $PSScriptRoot 'scripts\replace-hy2-profile.sh'

foreach ($path in @($launcher, $applier)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Required HY2 replacement source is missing: $path"
  }
}

$launcherSource = Get-Content -LiteralPath $launcher -Raw
$applierSource = Get-Content -LiteralPath $applier -Raw

if ($launcherSource -notmatch 'Read-Host -AsSecureString') {
  throw 'The launcher must read HY2 secrets through masked prompts.'
}
if ($launcherSource -notmatch 'finally') {
  throw 'The launcher must remove the temporary secret payload on every path.'
}
if ($launcherSource -notmatch 'scp\.exe -O') {
  throw 'The launcher must use legacy SCP for DSM compatibility.'
}
if ($launcherSource -match "(?im)^\\s*\\$?(password|obfsPassword)\\s*=\\s*'[^']+'") {
  throw 'The launcher must not embed a default connection secret.'
}
if ($launcherSource -match 'generate-mihomo-config') {
  throw 'The launcher must not deploy the legacy full-config generator.'
}
if ($applierSource -match '/bin/sh \"\$TARGET_GENERATOR\"') {
  throw 'The applier must not regenerate the complete Mihomo configuration.'
}
foreach ($required in @('backup_dir=', 'restore()', 'replace_hy2_block()', 'HY2_PORTS', 'ports', '/mihomo -t -d /root/.config/mihomo', 'proxy_delay', 'HY2_PROFILE_UPDATE=success')) {
  if (-not $applierSource.Contains($required)) {
    throw "The HY2 applier must contain: $required"
  }
}
if ($applierSource -match 'cat .*hysteria2\.env|print.*HY2_PASSWORD|echo .*HY2_PASSWORD') {
  throw 'The HY2 applier must never print the secret profile.'
}

$pythonBlocks = [regex]::Matches($applierSource, '(?ms)replace_hy2_block\(\) \{\r?\n  python3 - "\$SOURCE_PROFILE" "\$CONFIG" <<''PY''\r?\n(.*?)\r?\nPY')
if ($pythonBlocks.Count -ne 1) {
  throw 'The HY2 config-block editor must be embedded exactly once.'
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("vpn-hy2-test-$([guid]::NewGuid().ToString('N'))")
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
try {
  $profilePath = Join-Path $fixtureRoot 'hysteria2.env'
  $configPath = Join-Path $fixtureRoot 'config.yaml'
  [IO.File]::WriteAllText($profilePath, @"
HY2_SERVER='example-hy2.test'
HY2_PORT='443'
HY2_PORTS='20000-50000'
HY2_PASSWORD='fixture-password'
HY2_OBFS_PASSWORD='fixture-obfs'
HY2_SNI='example-hy2.test'
"@, [Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText($configPath, @"
proxies:
  - name: WG-IMP
    type: wireguard
  - name: HY2-NL
    type: hysteria2
    server: "old.example"
    port: 443
    password: "old"
proxy-groups:
  - name: VPS-FALLBACK
    type: fallback
    interval: 30
rules:
  - RULE-SET,managed-direct,DIRECT
  - MATCH,VPS-FALLBACK
"@, [Text.UTF8Encoding]::new($false))
  $profileWsl = "/mnt/$($profilePath.Substring(0, 1).ToLower())$($profilePath.Substring(2).Replace('\', '/'))"
  $configWsl = "/mnt/$($configPath.Substring(0, 1).ToLower())$($configPath.Substring(2).Replace('\', '/'))"
  $pythonBlocks[0].Groups[1].Value | & wsl.exe python3 - $profileWsl $configWsl
  if ($LASTEXITCODE -ne 0) { throw 'The HY2 config-block editor rejected a valid profile.' }
  $updated = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8)
  foreach ($required in @('ports: "20000-50000"', 'interval: 30', 'RULE-SET,managed-direct,DIRECT')) {
    if (-not $updated.Contains($required)) { throw "The HY2 config-block editor lost: $required" }
  }
}
finally {
  if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
  }
}

Write-Output 'HY2_REPLACEMENT_TEST=ok'
