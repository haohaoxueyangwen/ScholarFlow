param(
    [string] $RepoRoot,
    [string] $PythonPath,
    [string] $DataDir,
    [int] $ApiPort,
    [int] $WebPort
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
$api = Start-Process -FilePath $PythonPath -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -ArgumentList @(
    "-m", "scholarflow.workbench_api", "--data-dir", ('"' + $DataDir + '"'), "--host", "127.0.0.1", "--port", $ApiPort
)
try { Wait-Process -Id $api.Id }
finally { if (-not $api.HasExited) { Stop-Process -Id $api.Id -Force } }
