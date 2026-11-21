param(
    [Alias('Home')] [string] $AgentHome = (Join-Path $env:LOCALAPPDATA 'ScholarFlow'),
    [string] $CacheRoot = '',
    [switch] $WithWeb,
    [switch] $WithKnowledge
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$AgentHome = [IO.Path]::GetFullPath($AgentHome).TrimEnd('\')
if (-not $CacheRoot) { $CacheRoot = Join-Path $AgentHome 'cache' }
$CacheRoot = [IO.Path]::GetFullPath($CacheRoot).TrimEnd('\')
$env:SCHOLARFLOW_RUNTIME_DIR = Join-Path $AgentHome 'runtime-state'
if ($root -match '[%"\r\n]' -or $AgentHome -match '[%"\r\n]') { throw 'Installation paths must not contain percent signs, quotes or line breaks.' }
foreach ($start in @($root, $AgentHome, $CacheRoot)) {
    $part = $start
    while ($part) {
        if ((Test-Path -LiteralPath $part) -and ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Installation paths cannot traverse symbolic links or junctions.' }
        $parent = Split-Path -Parent $part
        if (-not $parent -or $parent -eq $part) { break }
        $part = $parent
    }
}
if (Test-Path -LiteralPath (Join-Path $AgentHome 'runtime.json')) { throw 'An installed runtime already exists. It was not changed; use a new Home for an upgrade candidate.' }
if (Test-Path -LiteralPath (Join-Path $AgentHome 'bin\scholarflow.cmd')) { throw 'A command shim already exists; it was not overwritten.' }
$piDir = Join-Path $root 'packages\scholarflow-pi'
$webDir = Join-Path $root 'apps\research-workbench'
if (Test-Path -LiteralPath (Join-Path $piDir 'node_modules')) { throw 'Pi dependencies already exist. Install only from a clean source snapshot; nothing was removed.' }
if ($WithWeb -and (Test-Path -LiteralPath (Join-Path $webDir 'node_modules'))) { throw 'Web dependencies already exist. Install only from a clean source snapshot; nothing was removed.' }
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
. (Join-Path $PSScriptRoot 'node_runtime.ps1')
$bootstrap = (Resolve-ScholarFlowPython -RepoRoot $root -MinimumVersion ([version]'3.11.0')).Path
$node = Resolve-ScholarFlowNode -MinimumVersion ([version]'22.19.0')
$env:PATH = "$(Split-Path -Parent $node);$env:PATH"
$npm = Join-Path (Split-Path -Parent $node) 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path -LiteralPath $npm -PathType Leaf)) { throw 'The selected Node installation has no npm CLI.' }
$envRoot = Join-Path $AgentHome ('runtime\candidate-' + [guid]::NewGuid().ToString('N'))
$env:SCHOLARFLOW_NODE = $node
$env:PIP_CACHE_DIR = Join-Path $CacheRoot 'pip'
$env:npm_config_cache = Join-Path $CacheRoot 'npm'
$env:PYTHONNOUSERSITE = '1'
[IO.Directory]::CreateDirectory($AgentHome) | Out-Null
$npmVersion = (& $node $npm --version).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect npm version.' }
if ($npmVersion -ne '11.6.2') {
    $npmRoot = Join-Path $AgentHome 'runtime\npm-11.6.2'
    if (Test-Path -LiteralPath $npmRoot) { throw 'A pre-existing npm bootstrap directory was not changed; use a fresh Home.' }
    & $node $npm install --prefix $npmRoot 'npm@11.6.2' --no-save --no-package-lock --ignore-scripts --no-audit --no-fund --registry 'https://registry.npmjs.org/'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned npm 11.6.2; no runtime was activated.' }
    $npm = Join-Path $npmRoot 'node_modules\npm\bin\npm-cli.js'
    if (-not (Test-Path -LiteralPath $npm -PathType Leaf) -or (& $node $npm --version).Trim() -ne '11.6.2') { throw 'Pinned npm bootstrap could not be verified.' }
}
& $bootstrap -m venv $envRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Python environment; no runtime was activated.' }
$python = Join-Path $envRoot 'Scripts\python.exe'
& $python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements-scholar.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed; no runtime was activated.' }
if ($WithKnowledge) {
    & $python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements-knowledge.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Knowledge dependencies failed to install; no runtime was activated.' }
}
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are inconsistent; no runtime was activated.' }
& $node $npm ci --prefix $piDir --include=dev --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'Pinned Pi dependency installation failed; no runtime was activated.' }
if ($WithWeb) {
    # Browser client does not require the Electron desktop binary.
    $env:ELECTRON_SKIP_BINARY_DOWNLOAD = '1'
    & $node $npm ci --prefix $webDir --include=dev --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Optional web dependency installation failed; no runtime was activated.' }
}
$bin = Join-Path $AgentHome 'bin'
[IO.Directory]::CreateDirectory($bin) | Out-Null
$shim = Join-Path $bin 'scholarflow.cmd'
if (Test-Path -LiteralPath $shim) { throw 'A command shim already exists; it was not overwritten.' }
$content = "@echo off`r`nset `"SCHOLARFLOW_HOME=$AgentHome`"`r`ncall `"$root\ScholarFlow-Agent.bat`" %*`r`nexit /b %ERRORLEVEL%`r`n"
[IO.File]::WriteAllText($shim, $content, [Text.UTF8Encoding]::new($false))
$record = @{ schema = 1; python = $python; node = $node; source_root = $root; web_dependencies = [bool]$WithWeb; knowledge_dependencies = [bool]$WithKnowledge } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $AgentHome 'runtime.json'), $record, [Text.UTF8Encoding]::new($false))
Write-Host "Installed CLI: $shim"
Write-Host 'No system PATH, model credentials, research data, or existing services were changed.'
