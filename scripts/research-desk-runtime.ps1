param(
  [int] $ApiPort = 8787,
  [int] $WebPort = 3000,
  [string] $DataDir = "",
  [string] $WorkspaceHome = "",
  [switch] $NoBrowser,
  [switch] $ProbeOnly,
  [switch] $StopOwned
)

# This launcher owns only its recorded candidate pair. It never starts the old
# Workbench, the knowledge stack, a model server, or stops an unknown process.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$webRoot = Join-Path $root "apps\research-workbench"
if ($ApiPort -lt 1 -or $ApiPort -gt 65535 -or $WebPort -lt 1 -or $WebPort -gt 65535 -or $ApiPort -eq $WebPort) { throw "Choose two distinct valid ports." }
if (-not $DataDir) { $DataDir = Join-Path $root ".local\research-library\facts" }
if (-not $WorkspaceHome) { $WorkspaceHome = Join-Path $root ".local\research-workspaces" }
$DataDir = [IO.Path]::GetFullPath($DataDir).TrimEnd('\')
$WorkspaceHome = [IO.Path]::GetFullPath($WorkspaceHome).TrimEnd('\')
if ($DataDir -match '["\r\n]' -or $WorkspaceHome -match '["\r\n]') { throw "Invalid data path." }
$apiUrl = "http://127.0.0.1:$ApiPort"
$webUrl = "http://127.0.0.1:$WebPort"
$registry = Join-Path $root ".tmp\research-desk-instances"
$recordPath = Join-Path $registry "$ApiPort-$WebPort.json"

function Test-Port([int] $Port) {
  $client = [Net.Sockets.TcpClient]::new()
  try {
    $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
    if (-not $pending.AsyncWaitHandle.WaitOne(700)) { return $false }
    $client.EndConnect($pending)
    return $true
  } catch { return $false } finally { $client.Dispose() }
}

function Test-RecordedProcess([int] $Id, [string] $Path, [long] $StartTicks) {
  try {
    $process = Get-Process -Id $Id -ErrorAction Stop
    return ($process.Path -and $process.Path -ieq $Path -and $process.StartTime.ToUniversalTime().Ticks -eq $StartTicks)
  } catch { return $false }
}

function Test-PortOwner([int] $Port, [int] $ExpectedId) {
  # netstat is read-only and remains usable where Get-NetTCPConnection requires elevation.
  $lines = & netstat.exe -ano -p tcp 2>$null
  if ($LASTEXITCODE -ne 0) { return $false }
  $owners = @()
  foreach ($line in $lines) {
    if ($line -match '^\s*TCP\s+(?<local>\S+)\s+\S+\s+LISTENING\s+(?<owner>\d+)\s*$') {
      if ($Matches.local -in @("127.0.0.1:$Port", "0.0.0.0:$Port")) { $owners += [int]$Matches.owner }
    }
  }
  $owners = @($owners | Select-Object -Unique)
  return ($owners.Count -eq 1 -and $owners[0] -eq $ExpectedId)
}

function Test-WebAssets {
  try {
    $page = Invoke-WebRequest -Uri "$webUrl/desk" -UseBasicParsing -TimeoutSec 5
    if ($page.StatusCode -ne 200) { return $false }
    $references = [regex]::Matches($page.Content, '(?i)(?:src|href)=["''](?<url>/[^"''\s>]+\.(?:js|css)(?:\?[^"''\s>]*)?)["'']')
    if ($references.Count -eq 0) { return $false }
    foreach ($reference in $references) {
      $asset = Invoke-WebRequest -Uri ($webUrl + $reference.Groups['url'].Value) -UseBasicParsing -TimeoutSec 5
      if ($asset.StatusCode -ne 200) { return $false }
    }
    return $true
  } catch { return $false }
}

function Get-InstanceState {
  $apiOpen = Test-Port $ApiPort
  $webOpen = Test-Port $WebPort
  if (-not $apiOpen -and -not $webOpen) { return @{ status = "free"; reason = "Both ports are available." } }
  if (-not $apiOpen -or -not $webOpen -or -not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
    return @{ status = "conflict"; reason = "A requested port is in use by an unverified process. No process was stopped." }
  }
  try {
    $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($record.root -ine $root -or $record.data_dir -ine $DataDir -or $record.workspace_home -ine $WorkspaceHome -or
        [int]$record.api_port -ne $ApiPort -or [int]$record.web_port -ne $WebPort -or
        -not (Test-RecordedProcess $record.api_pid $record.api_exe $record.api_start_ticks) -or
        -not (Test-RecordedProcess $record.web_pid $record.web_exe $record.web_start_ticks) -or
        -not (Test-PortOwner $ApiPort $record.api_pid) -or -not (Test-PortOwner $WebPort $record.web_pid)) {
      return @{ status = "conflict"; reason = "The recorded process or its data scope cannot be verified. No process was stopped." }
    }
    $api = Invoke-RestMethod -Uri "$apiUrl/health" -TimeoutSec 3
    $web = Invoke-RestMethod -Uri "$webUrl/__scholarflow_health" -TimeoutSec 3
    if ($api.service -ne "scholarflow-research" -or $web.service -ne "scholarflow-research-web" -or
        $api.instance_id -ne $record.instance_id -or $web.instance_id -ne $record.instance_id -or
        $web.build_id -ne $record.build_id -or -not (Test-WebAssets)) {
      return @{ status = "conflict"; reason = "The recorded instance or its web assets are not healthy. No process was stopped." }
    }
    return @{ status = "ready"; reason = "Verified research desk."; record = $record }
  } catch { return @{ status = "conflict"; reason = "Cannot verify the running research desk. No process was stopped." } }
}

$state = Get-InstanceState
if ($ProbeOnly) {
  @{ status = $state.status; reason = $state.reason } | ConvertTo-Json -Compress
  if ($state.status -eq "conflict") { exit 1 }
  exit 0
}
if ($StopOwned) {
  if ($state.status -ne "ready") { throw "Cannot safely stop the instance: $($state.reason)" }
  $state = Get-InstanceState
  if ($state.status -ne "ready") { throw "The instance changed before it could be stopped." }
  Stop-Process -Id ([int]$state.record.web_pid) -ErrorAction Stop
  Stop-Process -Id ([int]$state.record.api_pid) -ErrorAction Stop
  Write-Host "Stopped only the verified ScholarFlow research desk pair."
  exit 0
}
if ($state.status -eq "conflict") { throw $state.reason }

. (Join-Path $PSScriptRoot "python_runtime.ps1")
$python = (Resolve-ScholarFlowPython -RepoRoot $root -MinimumVersion ([version]"3.11.0")).Path
. (Join-Path $PSScriptRoot "node_runtime.ps1")
$node = Resolve-ScholarFlowNode -MinimumVersion ([version]"22.19.0")
$env:SCHOLARFLOW_NODE = $node
$env:NEXT_PUBLIC_SCHOLARFLOW_RESEARCH_URL = $apiUrl
$sourceStamp = (& $node (Join-Path $webRoot "scripts\build-stamp.mjs") --fingerprint).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceStamp -notmatch '^[a-f0-9]{64}$') { throw "Cannot calculate the research web build fingerprint." }
if ($state.status -eq "ready") {
  if ($state.record.build_id -ne $sourceStamp) { throw "A verified research desk is running an older build. Close it using this script with -StopOwned, then launch again." }
  if (-not $NoBrowser) { Start-Process "$webUrl/desk" }
  Write-Host "Research desk already running: $webUrl/desk"
  exit 0
}
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) { throw "Research data directory does not exist: $DataDir" }
if (-not (Test-Path -LiteralPath (Join-Path $webRoot "node_modules\vinext\dist\cli.js") -PathType Leaf)) { throw "Research web dependencies are missing. Install them before starting." }

$buildParent = Join-Path $root ".tmp\research-desk-builds"
$buildRoot = $null
if (Test-Path -LiteralPath $buildParent -PathType Container) {
  foreach ($candidate in Get-ChildItem -LiteralPath $buildParent -Directory | Sort-Object LastWriteTimeUtc -Descending) {
    if ($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
    $receipt = Join-Path $candidate.FullName "dist\workbench-build.json"
    $checker = Join-Path $candidate.FullName "scripts\build-stamp.mjs"
    if (-not (Test-Path -LiteralPath $receipt -PathType Leaf) -or -not (Test-Path -LiteralPath $checker -PathType Leaf)) { continue }
    try {
      $candidateRecord = Get-Content -LiteralPath $receipt -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($candidateRecord.fingerprint -ne $sourceStamp -or -not $candidateRecord.output_fingerprint) { continue }
      & $node $checker --check
      if ($LASTEXITCODE -eq 0) { $buildRoot = $candidate.FullName; break }
    } catch { continue }
  }
}
if ($buildRoot) {
  Write-Host "Using a verified research desk build."
} else {
  $buildRoot = Join-Path $buildParent ([guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
  foreach ($name in @("app", "worker", "public", "tooling", "scripts", "db", "drizzle", "package.json", "package-lock.json", "vite.config.ts", "next.config.ts", "tsconfig.json", "postcss.config.mjs")) {
    $source = Join-Path $webRoot $name
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $buildRoot $name) -Recurse }
  }
  New-Item -ItemType Directory -Path (Join-Path $buildRoot ".openai") -Force | Out-Null
  Copy-Item -LiteralPath (Join-Path $webRoot ".openai\hosting.json") -Destination (Join-Path $buildRoot ".openai\hosting.json")
  # Vite writes node_modules/.vite-temp; keep that directory in the candidate.
  $candidateModules = Join-Path $buildRoot "node_modules"
  New-Item -ItemType Directory -Path $candidateModules -Force | Out-Null
  foreach ($dependency in Get-ChildItem -LiteralPath (Join-Path $webRoot "node_modules") -Directory -Force) {
    if ($dependency.Name.StartsWith('.') -and $dependency.Name -ne '.bin') { continue }
    New-Item -ItemType Junction -Path (Join-Path $candidateModules $dependency.Name) -Target $dependency.FullName | Out-Null
  }
  $stageStamp = (& $node (Join-Path $buildRoot "scripts\build-stamp.mjs") --fingerprint).Trim()
  if ($stageStamp -ne $sourceStamp) { throw "Web source changed while preparing the isolated build; retry." }
  Write-Host "Building an isolated research desk candidate..."
  Push-Location -LiteralPath $buildRoot
  try {
    & $node (Join-Path $webRoot "node_modules\vinext\dist\cli.js") build
    if ($LASTEXITCODE -ne 0) { throw "Research web build failed." }
    & $node (Join-Path $buildRoot "scripts\build-stamp.mjs")
    if ($LASTEXITCODE -ne 0) { throw "Research web build stamp failed." }
  } finally { Pop-Location }
  & $node (Join-Path $buildRoot "scripts\build-stamp.mjs") --check
  if ($LASTEXITCODE -ne 0) { throw "Research web candidate is incomplete or failed integrity verification." }
}
$currentStamp = (& $node (Join-Path $webRoot "scripts\build-stamp.mjs") --fingerprint).Trim()
if ($currentStamp -ne $sourceStamp) { throw "Web source changed while preparing the candidate; retry." }

New-Item -ItemType Directory -Path $registry -Force | Out-Null
$instance = [guid]::NewGuid().ToString('N')
$apiProcess = $null
$webProcess = $null
try {
  $apiArguments = @('-s', '-B', '-m', 'scholarflow.research_http', '--port', "$ApiPort", '--data-dir', ('"' + $DataDir + '"'), '--home', ('"' + $WorkspaceHome + '"'), '--web-origin', $webUrl, '--web-origin', "http://localhost:$WebPort", '--instance-id', $instance)
  $apiProcess = Start-Process -FilePath $python -ArgumentList $apiArguments -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $registry "$instance-api.out.log") -RedirectStandardError (Join-Path $registry "$instance-api.err.log")
  $webArguments = @(('"' + (Join-Path $buildRoot 'scripts\serve-local.mjs') + '"'), '--port', "$WebPort", '--instance-id', $instance, '--build-id', $sourceStamp)
  $webProcess = Start-Process -FilePath $node -ArgumentList $webArguments -WorkingDirectory $buildRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $registry "$instance-web.out.log") -RedirectStandardError (Join-Path $registry "$instance-web.err.log")
  $ready = $false
  for ($attempt = 0; $attempt -lt 120; $attempt++) {
    if ($apiProcess.HasExited -or $webProcess.HasExited) { throw "Research desk candidate exited; inspect logs in $registry." }
    try {
      $api = Invoke-RestMethod -Uri "$apiUrl/health" -TimeoutSec 2
      $web = Invoke-RestMethod -Uri "$webUrl/__scholarflow_health" -TimeoutSec 2
      if ($api.service -eq 'scholarflow-research' -and $web.service -eq 'scholarflow-research-web' -and $api.instance_id -eq $instance -and $web.instance_id -eq $instance -and $web.build_id -eq $sourceStamp -and (Test-WebAssets)) { $ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
  }
  if (-not $ready) { throw "Research desk candidate did not become healthy; inspect logs in $registry." }
  $apiProcess.Refresh(); $webProcess.Refresh()
  $record = [ordered]@{ root = $root; data_dir = $DataDir; workspace_home = $WorkspaceHome; api_port = $ApiPort; web_port = $WebPort; instance_id = $instance; build_id = $sourceStamp; build_root = $buildRoot; api_pid = $apiProcess.Id; api_exe = $apiProcess.Path; api_start_ticks = $apiProcess.StartTime.ToUniversalTime().Ticks; web_pid = $webProcess.Id; web_exe = $webProcess.Path; web_start_ticks = $webProcess.StartTime.ToUniversalTime().Ticks }
  [IO.File]::WriteAllText($recordPath, ($record | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
  $verified = Get-InstanceState
  if ($verified.status -ne 'ready') { throw "Started candidate could not be verified: $($verified.reason)" }
  Write-Host "Research desk ready: $webUrl/desk"
  if (-not $NoBrowser) { Start-Process "$webUrl/desk" }
} catch {
  foreach ($process in @($webProcess, $apiProcess)) {
    if ($process) { try { $process.Refresh(); if (-not $process.HasExited) { $process.Kill() } } catch { } }
  }
  throw
}
