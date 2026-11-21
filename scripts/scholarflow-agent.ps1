param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $AgentArgs)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $env:SCHOLARFLOW_HOME) { $env:SCHOLARFLOW_HOME = Join-Path $env:LOCALAPPDATA 'ScholarFlow' }
$env:SCHOLARFLOW_RUNTIME_DIR = Join-Path $env:SCHOLARFLOW_HOME 'runtime-state'
$runtime = Join-Path $env:SCHOLARFLOW_HOME 'runtime.json'
if (Test-Path -LiteralPath $runtime -PathType Leaf) {
    $record = Get-Content -LiteralPath $runtime -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($record.schema -ne 1 -or -not (Test-Path -LiteralPath $record.python -PathType Leaf)) { throw 'Installed Python runtime is unavailable; reinstall without deleting your data.' }
    $python = $record.python
    if (-not $env:SCHOLARFLOW_NODE -and $record.node -and (Test-Path -LiteralPath $record.node -PathType Leaf)) { $env:SCHOLARFLOW_NODE = $record.node }
} else {
    . (Join-Path $PSScriptRoot 'python_runtime.ps1')
    $python = (Resolve-ScholarFlowPython -RepoRoot $root -MinimumVersion ([version]'3.11.0')).Path
}
& $python -s -B (Join-Path $PSScriptRoot 'scholarflow-agent.py') @AgentArgs
exit $LASTEXITCODE
