[CmdletBinding(PositionalBinding = $false)]
param(
    [string] $DataDir = '.tmp\scholarflow-cli',
    [string[]] $Capabilities = @('project-evidence'),
    [string] $PdfReadGrantFile,
    [string] $PdfNoteGrantFile,
    [string] $KnowledgeReadGrantFile,
    [ValidateSet('off','minimal','low','medium','high','xhigh','max')] [string] $Thinking = 'medium',
    [switch] $Tunnel,
    [switch] $FullProfile,
    [switch] $ChatOnly,
    [switch] $Status,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $PiArgs
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$package = Join-Path $root 'packages\scholarflow-pi'
$provider = Join-Path $package 'providers\local-qwen\extension.ts'
$cli = Join-Path $package 'node_modules\@earendil-works\pi-coding-agent\dist\cli.js'
. (Join-Path $PSScriptRoot 'node_runtime.ps1')
$node = Resolve-ScholarFlowNode
$base = if ($env:SCHOLARFLOW_QWEN_BASE_URL) { $env:SCHOLARFLOW_QWEN_BASE_URL.TrimEnd('/') } else { 'http://127.0.0.1:18080/v1' }
$uri = [Uri]$base
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1','localhost','[::1]','::1') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath.TrimEnd('/') -ne '/v1') {
    throw 'Local Qwen requires a credential-free loopback HTTP /v1 endpoint.'
}
foreach ($path in @($node,$provider,$cli)) { if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing runtime dependency: $path" } }

function Get-LocalQwen {
    try {
        $response = Invoke-RestMethod -Uri "$base/models" -TimeoutSec 3
        return @($response.data | Where-Object { $_.id -eq 'qwen38-vision' }) | Select-Object -First 1
    } catch { return $null }
}

$ownedTunnel = $null
$previousBase = $env:SCHOLARFLOW_QWEN_BASE_URL
Push-Location $root
try {
    $model = Get-LocalQwen
    if (-not $model -and $Tunnel) {
        if ($uri.Host -notin @('127.0.0.1','localhost')) { throw '-Tunnel supports the IPv4 loopback endpoint only.' }
        $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
        $arguments = @('-N','-T','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-L',"127.0.0.1:$($uri.Port):127.0.0.1:18080",'server')
        $ownedTunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            $model = Get-LocalQwen
            $ownedTunnel.Refresh()
            if ($model -or $ownedTunnel.HasExited) { break }
            Start-Sleep -Milliseconds 300
        } while ([DateTime]::UtcNow -lt $deadline)
    }
    if (-not $model) { throw 'qwen38-vision is unavailable. Open the server SSH tunnel or run this launcher with -Tunnel; no other provider was selected.' }
    if ($model.max_model_len -and [int64]$model.max_model_len -lt 262144) { throw 'Server context is smaller than the registered 262144; review provider metadata before continuing.' }
    $env:SCHOLARFLOW_QWEN_BASE_URL = $base
    $qualificationJson = & $node (Join-Path $package 'providers\local-qwen\status.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve local Qwen tool qualification.' }
    $qualification = $qualificationJson | ConvertFrom-Json
    if ($Status) {
        @{ provider='scholarflow-local'; model='qwen38-vision'; endpoint=$base; reachable=$true; server_context=$model.max_model_len; thinking=$Thinking; request_timeout_ms=600000; maximum_images_per_request=1; owned_tunnel=[bool]$ownedTunnel; profile=$(if ($ChatOnly) {'chat-only'} elseif ($FullProfile) {'existing-full'} else {'minimal'}); tool_calling_enabled=[bool]$qualification.enabled; tool_calling_qualified=[bool]$qualification.qualified; activated_workbench=$false } | ConvertTo-Json -Compress
        $result = 0
    } elseif ($ChatOnly) {
        if ($FullProfile) { throw '-ChatOnly cannot be combined with -FullProfile.' }
        $allToolsJson = & $node (Join-Path $package 'providers\local-qwen\builtin-tools.mjs')
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve Pi built-in tools.' }
        $allTools = @($allToolsJson | ConvertFrom-Json) -join ','
        & $node $cli --offline --no-extensions --no-skills --no-context-files --no-prompt-templates --no-themes --exclude-tools $allTools -e $provider --provider scholarflow-local --model qwen38-vision --thinking $Thinking @PiArgs
        $result = $LASTEXITCODE
    } elseif (-not $qualification.enabled) {
        throw 'Local Qwen tools are disabled. Use -ChatOnly or remove SCHOLARFLOW_QWEN_TOOL_CALLING=0 for the qualified deployment.'
    } elseif ($FullProfile) {
        # Existing package manifest exposes this provider; keep its current tools/skills.
        & $node $cli --offline --provider scholarflow-local --model qwen38-vision --thinking $Thinking @PiArgs
        $result = $LASTEXITCODE
    } else {
        & (Join-Path $PSScriptRoot 'scholarflow-minimal.ps1') -DataDir $DataDir -Capabilities $Capabilities -PdfReadGrantFile $PdfReadGrantFile -PdfNoteGrantFile $PdfNoteGrantFile -KnowledgeReadGrantFile $KnowledgeReadGrantFile -PiArgs (@('--provider','scholarflow-local','--model','qwen38-vision','--thinking',$Thinking) + $PiArgs)
        $result = $LASTEXITCODE
    }
} finally {
    $env:SCHOLARFLOW_QWEN_BASE_URL = $previousBase
    if ($ownedTunnel) {
        $ownedTunnel.Refresh()
        if (-not $ownedTunnel.HasExited) { $ownedTunnel.Kill(); $ownedTunnel.WaitForExit() }
        $ownedTunnel.Dispose()
    }
    Pop-Location
}
exit $result
