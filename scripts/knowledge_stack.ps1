[CmdletBinding()]
param(
    [ValidateSet("Bootstrap", "Start", "Status", "Stop")]
    [string]$Action = "Status"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runtime = Join-Path $RepoRoot ".tmp\lightrag-native"
$LlamaRelease = "b9631"
$LlamaArchive = Join-Path $Runtime "llama-$LlamaRelease-win-cpu.zip"
$LlamaArchiveSha256 = "E9E8207067FDA14779CC2FB93B247EBC6B957AE9E91A93AC0E3D02327AD9041F"
$LlamaUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaRelease/llama-$LlamaRelease-bin-win-cpu-x64.zip"
$LlamaExe = Join-Path $Runtime "llama\llama-server.exe"
$EmbeddingModel = Join-Path $Runtime "models\Qwen3-Embedding-0.6B-Q8_0.gguf"
$EmbeddingModelSha256 = "06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439"
$EmbeddingModelUrl = "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf?download=true"
$RerankerModel = Join-Path $Runtime "models\BGE-Reranker-v2-M3-Q4_K_M.gguf"
$RerankerModelSha256 = "CC1D06602269713F710569A5EB371BEFF4EDD0A1E988299BCC46821392B8CCA5"
$RerankerModelUrl = "https://huggingface.co/Geofront/BGE-Reranker-v2-M3-GGUF/resolve/main/BGE-Reranker-v2-M3-Q4_K_M.gguf?download=true"
$LightRagExe = Join-Path $Runtime "venv\Scripts\lightrag-server.exe"
$ApiKeyPath = Join-Path $Runtime "lightrag-api-key.txt"
$Logs = Join-Path $Runtime "logs"

function Repair-ProcessEnvironment {
    # Some hosts inject both Path and PATH. Windows PowerShell Start-Process
    # copies them into a case-insensitive dictionary and fails before launch.
    $variables = [Environment]::GetEnvironmentVariables()
    $pathEntries = @(
        $variables.GetEnumerator() |
            Where-Object { [string]$_.Key -ieq "Path" } |
            ForEach-Object { [string]$_.Value }
    )
    if ($pathEntries.Count -gt 1) {
        $pathValue = $pathEntries |
            Sort-Object Length -Descending |
            Select-Object -First 1
        [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
        [Environment]::SetEnvironmentVariable("Path", $null, "Process")
        [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
    }
}

function Test-HttpEndpoint {
    param([string]$Uri, [hashtable]$Headers = @{})
    try {
        $null = Invoke-RestMethod -Uri $Uri -Headers $Headers -TimeoutSec 3
        return $true
    }
    catch {
        return $false
    }
}

function Assert-FileHash {
    param([string]$Path, [string]$Expected)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required file is missing: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ne $Expected) {
        throw "SHA-256 mismatch for $Path"
    }
}

function Initialize-ApiKey {
    if (-not (Test-Path -LiteralPath $ApiKeyPath)) {
        $key = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
        [IO.File]::WriteAllText($ApiKeyPath, $key)
    }
}

function Get-DeepSeekKey {
    if ($env:DEEPSEEK_API_KEY) {
        return $env:DEEPSEEK_API_KEY.Trim()
    }
    $authPath = Join-Path $HOME ".pi\agent\auth.json"
    if (Test-Path -LiteralPath $authPath) {
        $auth = Get-Content -LiteralPath $authPath -Raw | ConvertFrom-Json
        if ($auth.deepseek.key) {
            return ([string]$auth.deepseek.key).Trim()
        }
    }
    throw "No DeepSeek API key found. Set DEEPSEEK_API_KEY or configure DeepSeek in Pi."
}

function Write-ScholarFlowEnvironment {
    $apiKey = [IO.File]::ReadAllText($ApiKeyPath).Trim()
    $content = @"
`$env:SCHOLARFLOW_KNOWLEDGE_ENGINE = "lightrag"
`$env:SCHOLARFLOW_LIGHTRAG_BASE_URL = "http://127.0.0.1:9621"
`$env:SCHOLARFLOW_LIGHTRAG_API_KEY = "$apiKey"
`$env:SCHOLARFLOW_LIGHTRAG_WORKSPACE = "scholarflow_acceptance"
`$env:SCHOLARFLOW_LIGHTRAG_TIMEOUT_SECONDS = "120"
"@
    [IO.File]::WriteAllText(
        (Join-Path $Runtime "scholarflow.env.ps1"),
        $content,
        [Text.UTF8Encoding]::new($false)
    )
}

function Invoke-Bootstrap {
    New-Item -ItemType Directory -Force -Path $Runtime, $Logs, (Join-Path $Runtime "models"), (Join-Path $Runtime "data\rag_storage"), (Join-Path $Runtime "data\inputs") | Out-Null

    if (-not (Test-Path -LiteralPath $LlamaArchive) -or (Get-FileHash -LiteralPath $LlamaArchive -Algorithm SHA256).Hash -ne $LlamaArchiveSha256) {
        & curl.exe -L --fail --retry 3 --output $LlamaArchive $LlamaUrl
        if ($LASTEXITCODE -ne 0) { throw "Failed to download llama.cpp." }
    }
    Assert-FileHash $LlamaArchive $LlamaArchiveSha256
    if (-not (Test-Path -LiteralPath $LlamaExe)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Runtime "llama") | Out-Null
        Expand-Archive -LiteralPath $LlamaArchive -DestinationPath (Join-Path $Runtime "llama") -Force
    }

    if (-not (Test-Path -LiteralPath $EmbeddingModel) -or (Get-FileHash -LiteralPath $EmbeddingModel -Algorithm SHA256).Hash -ne $EmbeddingModelSha256) {
        & curl.exe -L --fail --retry 3 --continue-at - --output $EmbeddingModel $EmbeddingModelUrl
        if ($LASTEXITCODE -ne 0) { throw "Failed to download Qwen3 embedding model." }
    }
    Assert-FileHash $EmbeddingModel $EmbeddingModelSha256

    if (-not (Test-Path -LiteralPath $RerankerModel) -or (Get-FileHash -LiteralPath $RerankerModel -Algorithm SHA256).Hash -ne $RerankerModelSha256) {
        & curl.exe -L --fail --retry 3 --continue-at - --output $RerankerModel $RerankerModelUrl
        if ($LASTEXITCODE -ne 0) { throw "Failed to download BGE reranker model." }
    }
    Assert-FileHash $RerankerModel $RerankerModelSha256

    if (-not (Test-Path -LiteralPath $LightRagExe)) {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source venv (Join-Path $Runtime "venv") --python python
            if ($LASTEXITCODE -ne 0) { throw "Failed to create the LightRAG virtual environment." }
            & $uv.Source pip install --python (Join-Path $Runtime "venv\Scripts\python.exe") "lightrag-hku[api]==1.5.4"
        }
        else {
            & python -m venv (Join-Path $Runtime "venv")
            & (Join-Path $Runtime "venv\Scripts\python.exe") -m pip install "lightrag-hku[api]==1.5.4"
        }
        if ($LASTEXITCODE -ne 0) { throw "Failed to install LightRAG 1.5.4." }
    }

    Initialize-ApiKey
    if (-not (Test-Path -LiteralPath (Join-Path $Runtime ".env"))) {
        [IO.File]::WriteAllText((Join-Path $Runtime ".env"), "# Values are injected by knowledge_stack.ps1.`n")
    }
    Write-ScholarFlowEnvironment
}

function Invoke-Start {
    Invoke-Bootstrap
    Repair-ProcessEnvironment
    $deepSeekKey = Get-DeepSeekKey
    $apiKey = [IO.File]::ReadAllText($ApiKeyPath).Trim()

    if (-not (Test-HttpEndpoint "http://127.0.0.1:8081/health")) {
        if (Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue) {
            throw "Port 8081 is occupied by a service that is not a healthy embedding endpoint."
        }
        Start-Process -FilePath $LlamaExe `
            -ArgumentList @("-m", $EmbeddingModel, "--embedding", "--pooling", "last", "--host", "127.0.0.1", "--port", "8081", "-c", "8192", "-b", "512") `
            -WorkingDirectory (Split-Path $LlamaExe) `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "embedding.out.log") `
            -RedirectStandardError (Join-Path $Logs "embedding.err.log") | Out-Null
        $deadline = (Get-Date).AddSeconds(120)
        while (-not (Test-HttpEndpoint "http://127.0.0.1:8081/health")) {
            if ((Get-Date) -ge $deadline) { throw "Embedding service did not become healthy." }
            Start-Sleep -Seconds 2
        }
    }

    if (-not (Test-HttpEndpoint "http://127.0.0.1:8082/health")) {
        if (Get-NetTCPConnection -LocalPort 8082 -State Listen -ErrorAction SilentlyContinue) {
            throw "Port 8082 is occupied by a service that is not a healthy reranker endpoint."
        }
        Start-Process -FilePath $LlamaExe `
            -ArgumentList @("-m", $RerankerModel, "--reranking", "--host", "127.0.0.1", "--port", "8082", "-c", "4096", "-b", "512") `
            -WorkingDirectory (Split-Path $LlamaExe) `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "reranker.out.log") `
            -RedirectStandardError (Join-Path $Logs "reranker.err.log") | Out-Null
        $deadline = (Get-Date).AddSeconds(120)
        while (-not (Test-HttpEndpoint "http://127.0.0.1:8082/health")) {
            if ((Get-Date) -ge $deadline) { throw "Reranker service did not become healthy." }
            Start-Sleep -Seconds 2
        }
    }

    if (-not (Test-HttpEndpoint "http://127.0.0.1:9621/health")) {
        if (Get-NetTCPConnection -LocalPort 9621 -State Listen -ErrorAction SilentlyContinue) {
            throw "Port 9621 is occupied by a service that is not LightRAG."
        }
        $env:LLM_BINDING = "openai"
        $env:LLM_MODEL = "deepseek-v4-flash"
        $env:LLM_BINDING_HOST = "https://api.deepseek.com/v1"
        $env:LLM_BINDING_API_KEY = $deepSeekKey
        $env:EMBEDDING_BINDING = "openai"
        $env:EMBEDDING_MODEL = "qwen3-embedding-0.6b"
        $env:EMBEDDING_DIM = "1024"
        $env:EMBEDDING_BINDING_HOST = "http://127.0.0.1:8081/v1"
        $env:EMBEDDING_BINDING_API_KEY = "local-no-key"
        # The local CPU embedding server is reliable with small serialized
        # batches. LightRAG's generic defaults can overload it on real papers.
        $env:EMBEDDING_BATCH_NUM = "4"
        $env:EMBEDDING_FUNC_MAX_ASYNC = "1"
        $env:EMBEDDING_TIMEOUT = "300"
        # llama.cpp exposes a Cohere-compatible endpoint, so LightRAG can use
        # a local multilingual cross-encoder without another cloud key.
        $env:RERANK_BINDING = "cohere"
        $env:RERANK_MODEL = "bge-reranker-v2-m3"
        $env:RERANK_BINDING_HOST = "http://127.0.0.1:8082/v1/rerank"
        $env:RERANK_BINDING_API_KEY = "local-no-key"
        $env:RERANK_BY_DEFAULT = "true"
        $env:MAX_ASYNC_RERANK = "1"
        $env:RERANK_TIMEOUT = "120"
        $env:MIN_RERANK_SCORE = "0.0"
        $env:LIGHTRAG_API_KEY = $apiKey
        $env:TOKEN_SECRET = $apiKey
        $env:SUMMARY_LANGUAGE = "Chinese"
        $env:ENTITY_EXTRACTION_USE_JSON = "true"
        $env:PYTHONUTF8 = "1"
        $env:PYTHONIOENCODING = "utf-8"
        Start-Process -FilePath $LightRagExe `
            -ArgumentList @("--host", "127.0.0.1", "--port", "9621", "--workspace", "scholarflow_acceptance", "--working-dir", (Join-Path $Runtime "data\rag_storage"), "--input-dir", (Join-Path $Runtime "data\inputs"), "--workers", "1", "--max-async", "2", "--timeout", "300", "--log-level", "INFO") `
            -WorkingDirectory $Runtime `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "lightrag.out.log") `
            -RedirectStandardError (Join-Path $Logs "lightrag.err.log") | Out-Null
        $deadline = (Get-Date).AddSeconds(180)
        while (-not (Test-HttpEndpoint "http://127.0.0.1:9621/health")) {
            if ((Get-Date) -ge $deadline) { throw "LightRAG did not become healthy." }
            Start-Sleep -Seconds 3
        }
    }
    Write-ScholarFlowEnvironment
}

function Invoke-Stop {
    $escapedRuntime = [regex]::Escape($Runtime)
    $targets = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match $escapedRuntime -and
        $_.Name -in @("llama-server.exe", "lightrag-server.exe", "python.exe")
    }
    $ids = @($targets | ForEach-Object { [int]$_.ProcessId })
    if ($ids.Count -gt 0) {
        Stop-Process -Id $ids -Force
        Start-Sleep -Seconds 2
    }
}

if ($Action -eq "Bootstrap") { Invoke-Bootstrap }
if ($Action -eq "Start") { Invoke-Start }
if ($Action -eq "Stop") { Invoke-Stop }

$embeddingReady = Test-HttpEndpoint "http://127.0.0.1:8081/health"
$rerankerReady = Test-HttpEndpoint "http://127.0.0.1:8082/health"
$lightRagReady = Test-HttpEndpoint "http://127.0.0.1:9621/health"
[pscustomobject]@{
    action = $Action
    runtime = $Runtime
    embedding = [pscustomobject]@{
        ready = $embeddingReady
        endpoint = "http://127.0.0.1:8081/v1"
        model = "Qwen3-Embedding-0.6B-Q8_0"
        dimension = 1024
    }
    reranker = [pscustomobject]@{
        ready = $rerankerReady
        endpoint = "http://127.0.0.1:8082/v1/rerank"
        model = "BGE-Reranker-v2-M3-Q4_K_M"
    }
    lightrag = [pscustomobject]@{
        ready = $lightRagReady
        endpoint = "http://127.0.0.1:9621"
        version = "1.5.4"
        workspace = "scholarflow_acceptance"
    }
    scholarflow_env = (Join-Path $Runtime "scholarflow.env.ps1")
} | ConvertTo-Json -Depth 4
