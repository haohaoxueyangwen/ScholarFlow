function Test-ScholarFlowKnowledgeEndpoint {
    param([Parameter(Mandatory = $true)][string] $Uri)

    try {
        $null = Invoke-RestMethod -Uri $Uri -TimeoutSec 3
        return $true
    }
    catch {
        return $false
    }
}

function Enable-ScholarFlowKnowledgeRuntime {
    param(
        [Parameter(Mandatory = $true)][string] $RepoRoot,
        [ValidateSet("auto", "disabled")][string] $Mode = "auto"
    )

    $scholarFlowEnvironmentNames = @(
        "SCHOLARFLOW_KNOWLEDGE_ENGINE",
        "SCHOLARFLOW_LIGHTRAG_BASE_URL",
        "SCHOLARFLOW_LIGHTRAG_API_KEY",
        "SCHOLARFLOW_LIGHTRAG_WORKSPACE",
        "SCHOLARFLOW_LIGHTRAG_TIMEOUT_SECONDS"
    )
    if ($Mode -eq "disabled") {
        foreach ($name in $scholarFlowEnvironmentNames) {
            [Environment]::SetEnvironmentVariable($name, $null, "Process")
        }
        return [ordered]@{
            status = "disabled"
            embedding_ready = $false
            reranker_ready = $false
            lightrag_ready = $false
            message = "Automatic AI knowledge runtime startup is disabled."
        }
    }

    $stackScript = Join-Path $RepoRoot "scripts\knowledge_stack.ps1"
    $environmentScript = Join-Path $RepoRoot ".tmp\lightrag-native\scholarflow.env.ps1"
    $embeddingReady = Test-ScholarFlowKnowledgeEndpoint "http://127.0.0.1:8081/health"
    $rerankerReady = Test-ScholarFlowKnowledgeEndpoint "http://127.0.0.1:8082/health"
    $lightRagReady = Test-ScholarFlowKnowledgeEndpoint "http://127.0.0.1:9621/health"

    if (-not ($embeddingReady -and $rerankerReady -and $lightRagReady)) {
        if (-not (Test-Path -LiteralPath $stackScript -PathType Leaf)) {
            foreach ($name in $scholarFlowEnvironmentNames) {
                [Environment]::SetEnvironmentVariable($name, $null, "Process")
            }
            return [ordered]@{
                status = "unavailable"
                embedding_ready = $embeddingReady
                reranker_ready = $rerankerReady
                lightrag_ready = $lightRagReady
                message = "The native AI knowledge runtime launcher is unavailable."
            }
        }
        try {
            # Invoke the PowerShell stack script in-process. Piping a nested
            # powershell.exe into Out-Null can leave its anonymous pipe open in
            # long-lived child services, making an otherwise healthy launcher
            # appear to hang until those services stop.
            $null = & $stackScript -Action Start
        }
        catch {
            Write-Warning "AI knowledge runtime could not start; ScholarFlow will continue without semantic retrieval. $($_.Exception.Message)"
        }
        $embeddingReady = Test-ScholarFlowKnowledgeEndpoint "http://127.0.0.1:8081/health"
        $rerankerReady = Test-ScholarFlowKnowledgeEndpoint "http://127.0.0.1:8082/health"
        $lightRagReady = Test-ScholarFlowKnowledgeEndpoint "http://127.0.0.1:9621/health"
    }

    if ($embeddingReady -and $rerankerReady -and $lightRagReady -and (Test-Path -LiteralPath $environmentScript -PathType Leaf)) {
        . $environmentScript
        return [ordered]@{
            status = "ready"
            embedding_ready = $true
            reranker_ready = $true
            lightrag_ready = $true
            engine = "lightrag"
            workspace = $env:SCHOLARFLOW_LIGHTRAG_WORKSPACE
            message = "The local AI knowledge runtime is ready."
        }
    }

    foreach ($name in $scholarFlowEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    return [ordered]@{
        status = "unavailable"
        embedding_ready = $embeddingReady
        reranker_ready = $rerankerReady
        lightrag_ready = $lightRagReady
        message = if (-not (Test-Path -LiteralPath $environmentScript -PathType Leaf)) {
            "The generated ScholarFlow knowledge environment is missing."
        }
        else {
            "The local AI knowledge runtime did not become healthy."
        }
    }
}
