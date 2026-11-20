param(
  [int] $ApiPort = 8787,
  [int] $WebPort = 3000,
  [string] $DataDir = "",
  [string] $WorkspaceHome = "",
  [switch] $NoBrowser,
  [switch] $ProbeOnly,
  [switch] $StopOwned
)

# Keep the existing desktop entry stable; the implementation owns only its
# verified research API/web pair and never starts the legacy Workbench stack.
& (Join-Path $PSScriptRoot "research-desk-runtime.ps1") @PSBoundParameters
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
