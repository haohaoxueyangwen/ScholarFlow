function Test-ScholarFlowPythonCandidate {
    param(
        [string] $Path,
        [version] $MinimumVersion,
        [string] $PreferredEnvironment = "scholar",
        [string] $Source = "candidate"
    )

    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $resolved = (Resolve-Path -LiteralPath $Path).Path
        $version = [version]((& $resolved -s -c "import platform; print(platform.python_version())").Trim())
        if ($version -lt $MinimumVersion) {
            return $null
        }
        $prefixName = Split-Path -Leaf (Split-Path -Parent $resolved)
        return [ordered]@{
            Path = $resolved
            Version = $version
            Environment = if ($prefixName -ieq $PreferredEnvironment) { $PreferredEnvironment } else { $prefixName }
            Preferred = $prefixName -ieq $PreferredEnvironment
            Source = $Source
        }
    }
    catch {
        return $null
    }
}

function Save-ScholarFlowPythonRuntime {
    param(
        [string] $RepoRoot,
        [System.Collections.IDictionary] $Runtime
    )

    $runtimeRoot = if ($env:SCHOLARFLOW_RUNTIME_DIR) { Join-Path $env:SCHOLARFLOW_RUNTIME_DIR 'workbench-runtime' } else { Join-Path $RepoRoot ".tmp\workbench-runtime" }
    $target = Join-Path $runtimeRoot "python-runtime.json"
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $payload = [ordered]@{
        schema_version = 1
        path = $Runtime.Path
        version = $Runtime.Version.ToString()
        environment = $Runtime.Environment
        preferred = [bool]$Runtime.Preferred
        resolved_at = [DateTime]::UtcNow.ToString("o")
    }
    [IO.File]::WriteAllText(
        $target,
        ($payload | ConvertTo-Json -Depth 3),
        [Text.UTF8Encoding]::new($false)
    )
}

function Resolve-ScholarFlowPython {
    param(
        [Parameter(Mandatory = $true)][string] $RepoRoot,
        [version] $MinimumVersion = [version]"3.11.0",
        [string] $PreferredEnvironment = "scholar"
    )

    $explicit = Test-ScholarFlowPythonCandidate `
        -Path $env:SCHOLARFLOW_PYTHON `
        -MinimumVersion $MinimumVersion `
        -PreferredEnvironment $PreferredEnvironment `
        -Source "SCHOLARFLOW_PYTHON"
    if ($explicit) {
        Save-ScholarFlowPythonRuntime -RepoRoot $RepoRoot -Runtime $explicit
        return $explicit
    }

    $cachePath = if ($env:SCHOLARFLOW_RUNTIME_DIR) { Join-Path $env:SCHOLARFLOW_RUNTIME_DIR 'workbench-runtime\python-runtime.json' } else { Join-Path $RepoRoot ".tmp\workbench-runtime\python-runtime.json" }
    if (Test-Path -LiteralPath $cachePath -PathType Leaf) {
        try {
            $cache = Get-Content -LiteralPath $cachePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $cached = Test-ScholarFlowPythonCandidate `
                -Path ([string]$cache.path) `
                -MinimumVersion $MinimumVersion `
                -PreferredEnvironment $PreferredEnvironment `
                -Source "runtime-cache"
            if ($cached -and $cached.Preferred) {
                return $cached
            }
        }
        catch {
            # Ignore stale or malformed local runtime state.
        }
    }

    $preferredCandidates = @()
    if ($env:CONDA_PREFIX -and (Split-Path -Leaf $env:CONDA_PREFIX) -ieq $PreferredEnvironment) {
        $preferredCandidates += Join-Path $env:CONDA_PREFIX "python.exe"
    }
    $preferredCandidates += Join-Path $HOME ".conda\envs\$PreferredEnvironment\python.exe"
    $preferredCandidates += Join-Path $env:LOCALAPPDATA "conda\conda\envs\$PreferredEnvironment\python.exe"

    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if ($conda) {
        try {
            $condaState = (& $conda.Source env list --json 2>$null) | ConvertFrom-Json
            $preferredCandidates += @($condaState.envs) |
                Where-Object { (Split-Path -Leaf ([string]$_)) -ieq $PreferredEnvironment } |
                ForEach-Object { Join-Path ([string]$_) "python.exe" }
        }
        catch {
            # PATH and explicit candidates remain available without Conda.
        }
    }

    foreach ($candidate in $preferredCandidates | Where-Object { $_ } | Select-Object -Unique) {
        $runtime = Test-ScholarFlowPythonCandidate `
            -Path $candidate `
            -MinimumVersion $MinimumVersion `
            -PreferredEnvironment $PreferredEnvironment `
            -Source "conda:$PreferredEnvironment"
        if ($runtime) {
            Save-ScholarFlowPythonRuntime -RepoRoot $RepoRoot -Runtime $runtime
            return $runtime
        }
    }

    $fallbackCandidates = @(
        Get-Command python -All -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Source
    )
    $fallbackCandidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    $fallbackCandidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
    foreach ($candidate in $fallbackCandidates | Where-Object { $_ } | Select-Object -Unique) {
        $runtime = Test-ScholarFlowPythonCandidate `
            -Path $candidate `
            -MinimumVersion $MinimumVersion `
            -PreferredEnvironment $PreferredEnvironment `
            -Source "compatible-fallback"
        if ($runtime) {
            Save-ScholarFlowPythonRuntime -RepoRoot $RepoRoot -Runtime $runtime
            return $runtime
        }
    }

    throw "ScholarFlow requires Python >= $MinimumVersion. Bootstrap Conda environment '$PreferredEnvironment' or set SCHOLARFLOW_PYTHON."
}
