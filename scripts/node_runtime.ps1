function Resolve-ScholarFlowNode {
  param([version] $MinimumVersion = [version]"22.19.0")

  # Check every PATH match: the first installed Node may be too old while a
  # compatible version is available later. An explicit override takes priority.
  $candidates = @($env:SCHOLARFLOW_NODE)
  $candidates += @(Get-Command node.exe -All -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
  foreach ($candidate in $candidates | Where-Object { $_ } | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try {
      $observed = (& $candidate --version).Trim()
      if ($LASTEXITCODE -eq 0 -and [version]($observed.TrimStart('v')) -ge $MinimumVersion) {
        return $candidate
      }
    } catch { }
  }
  throw "Node.js >= $MinimumVersion is required; set SCHOLARFLOW_NODE to a compatible node.exe."
}
