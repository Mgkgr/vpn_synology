$ErrorActionPreference = 'Stop'

$launcher = Join-Path $PSScriptRoot 'replace-hy2-profile.ps1'
$applier = Join-Path $PSScriptRoot 'scripts\replace-hy2-profile.sh'
$renamer = Join-Path $PSScriptRoot 'scripts\rename-hy2-usa.py'
foreach ($path in @($launcher, $applier, $renamer)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Required HY2 replacement source is missing: $path"
  }
}

$launcherSource = Get-Content -LiteralPath $launcher -Raw
$applierSource = Get-Content -LiteralPath $applier -Raw
if ($launcherSource -notmatch 'Read-Host -AsSecureString|finally|File]::Delete') {
  throw 'The launcher must use masked prompts and remove the temporary secret payload.'
}
if ($launcherSource -notmatch 'scp\.exe -O') {
  throw 'The launcher must use DSM-compatible legacy SCP.'
}
if ($launcherSource -notmatch 'rename-hy2-usa.py') {
  throw 'The launcher must upload the DSM-compatible renamer.'
}
if (-not $launcherSource.Contains("'`$remoteStatus' '`$remoteLog'")) {
  throw 'The launcher must pass separate completion-status and private-log arguments to the applier.'
}
if ($launcherSource -notmatch 'HY2_APPLY=started|HY2 migration did not publish a completion state') {
  throw 'The launcher must wait for a published detached completion state.'
}
if ($launcherSource -match '(?im)^\s*\$?(password|obfsPassword)\s*=\s*''[^'']+''') {
  throw 'The launcher must not embed a default connection secret.'
}
foreach ($required in @(
  'rename-hy2-usa.py', 'managed-hy2-nl.txt', 'managed-hy2-usa.txt',
  'HY2-NL', 'HY2-USA', '/mihomo -t -d /root/.config/mihomo',
  'proxy_delay', 'HY2_PROFILE_UPDATE=success'
)) {
  if (-not $applierSource.Contains($required)) {
    throw "The HY2 applier must contain: $required"
  }
}
if ($applierSource -match 'cat .*hysteria2\.env|print.*HY2_PASSWORD|echo .*HY2_PASSWORD') {
  throw 'The HY2 applier must never print the secret profile.'
}
if ($applierSource -notmatch 'PRIVATE_LOG=\$\{5:\?missing private log file\}') {
  throw 'The HY2 applier must keep the public completion status and private log in separate arguments.'
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("vpn-hy2-test-$([guid]::NewGuid().ToString('N'))")
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
try {
  $profilePath = Join-Path $fixtureRoot 'hysteria2.env'
  $configPath = Join-Path $fixtureRoot 'config.yaml'
  $rulesPath = Join-Path $fixtureRoot 'rules'
  $newline = [Environment]::NewLine
  [IO.Directory]::CreateDirectory($rulesPath) | Out-Null
  [IO.File]::WriteAllText((Join-Path $rulesPath 'managed-hy2-nl.txt'), ('GEOSITE,openai' + $newline), [Text.UTF8Encoding]::new($false))
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
    proxies:
      - WG-IMP
      - HY2-NL
    interval: 30
rule-providers:
  managed-hy2-nl:
    type: file
    path: ./rules/managed-hy2-nl.txt
rules:
  - RULE-SET,managed-hy2-nl,HY2-NL
  - RULE-SET,managed-direct,DIRECT
  - MATCH,VPS-FALLBACK
"@, [Text.UTF8Encoding]::new($false))
  & python $renamer $profilePath $configPath $rulesPath
  if ($LASTEXITCODE -ne 0) { throw 'The HY2 renamer rejected a valid legacy profile.' }
  $updated = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8)
  foreach ($required in @(
    'name: HY2-USA', 'ports: "20000-50000"', '- HY2-USA',
    'managed-hy2-usa:', './rules/managed-hy2-usa.txt',
    'RULE-SET,managed-hy2-usa,HY2-USA', 'interval: 30',
    'RULE-SET,managed-direct,DIRECT'
  )) {
    if (-not $updated.Contains($required)) { throw "The HY2 renamer lost: $required" }
  }
  if ($updated.Contains('HY2-NL') -or $updated.Contains('managed-hy2-nl')) {
    throw 'The HY2 renamer left a legacy active reference.'
  }
  $renamedRules = Join-Path $rulesPath 'managed-hy2-usa.txt'
  if (-not (Test-Path -LiteralPath $renamedRules) -or (Test-Path -LiteralPath (Join-Path $rulesPath 'managed-hy2-nl.txt'))) {
    throw 'The managed HY2 rule file was not renamed atomically.'
  }
  if ([IO.File]::ReadAllText($renamedRules, [Text.Encoding]::UTF8) -ne ('GEOSITE,openai' + $newline)) {
    throw 'The managed HY2 rule file content changed during rename.'
  }
}
finally {
  if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
  }
}

Write-Output 'HY2_REPLACEMENT_TEST=ok'
