[CmdletBinding()]
param(
    [ValidateSet('Status','StartEmbedding','Search','Ask','Chat','DeltaPlan','DeltaApply','ProjectIndexPlan','ProjectIndexApply','Coverage')][string]$Action='Status',
    [string]$StageId='stage-scholarflow-library-research',
    [string]$Query,
    [string]$GrantFile,
    [string]$ContinuityGrantFile,
    [string]$PlanId,
    [string]$ReviewedHash
)
$ErrorActionPreference='Stop'
if ($Action -ne 'StartEmbedding') { throw 'This public preview only supports StartEmbedding.' }
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'python_runtime.ps1')
$python=Resolve-ScholarFlowPython -RepoRoot $root -MinimumVersion ([version]'3.11.0')
$env:SCHOLARFLOW_PYTHON=$python.Path
$native=Join-Path $root '.tmp\lightrag-native'
$knowledgePython=Join-Path $native 'venv\Scripts\python.exe'
$libraryHome=Join-Path $root '.local\research-library'
$env:PYTHONNOUSERSITE='1'
$env:PYTHONUTF8='1'
$embeddingPort=8081
$embeddingConfig=Join-Path $libraryHome 'embedding-runtime.json'
if(Test-Path -LiteralPath $embeddingConfig){
    $embeddingSettings=Get-Content -LiteralPath $embeddingConfig -Raw | ConvertFrom-Json
    if($embeddingSettings.endpoint -eq 'http://127.0.0.1:8082/v1/embeddings' -and $embeddingSettings.parity_status -eq 'verified' -and $embeddingSettings.model_sha256 -eq '06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439'){$embeddingPort=8082}
    elseif($embeddingSettings.endpoint -ne 'http://127.0.0.1:8081/v1/embeddings'){throw 'Unreviewed embedding configuration; no service started.'}
}
$effectiveGrant=if($GrantFile){$GrantFile}elseif($StageId -eq 'stage-scholarflow-library-research'){Join-Path $libraryHome 'projects\library-research\read-grant.json'}else{Join-Path $libraryHome 'pilot-v2\library-grant.json'}
function Test-Embedding {
    try { $health=Invoke-RestMethod "http://127.0.0.1:$embeddingPort/health" -TimeoutSec 2; return $health.status -eq 'ok' } catch { return $false }
}
function Start-Embedding {
    if(Test-Embedding){ return }
    if($embeddingPort -eq 8082){
        & (Join-Path $PSScriptRoot 'prepare-local-embedding-gpu.ps1') -StartCandidate
        if($LASTEXITCODE -and $LASTEXITCODE -ne 0){throw 'Reviewed GPU candidate failed to start; no automatic CPU rerouting.'}
        $deadline=[DateTime]::UtcNow.AddSeconds(30)
        while([DateTime]::UtcNow -lt $deadline){if(Test-Embedding){return};Start-Sleep -Milliseconds 250}
        throw 'GPU embedding did not become ready; inspect candidate logs.'
    }
    if(Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue){throw 'Embedding port 8081 is occupied by an unhealthy or unrecognized service. No process was stopped.'}
    $exe=Join-Path $native 'llama\llama-server.exe'
    $model=Join-Path $native 'models\Qwen3-Embedding-0.6B-Q8_0.gguf'
    foreach($path in @($exe,$model,$knowledgePython)){if(-not(Test-Path -LiteralPath $path -PathType Leaf)){throw "Pinned local dependency missing: $path"}}
    if((Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash -ne '06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'){throw 'Embedding model hash differs from reviewed model; host review required.'}
    $runtime=Join-Path $libraryHome 'runtime'
    [IO.Directory]::CreateDirectory($runtime)|Out-Null
    $tag=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
    $process=Start-Process -FilePath $exe -ArgumentList @('-m',('"'+$model+'"'),'--embedding','--pooling','last','--host','127.0.0.1','--port','8081','-c','8192','-b','512','-ngl','0','-t','4','-tb','4') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime "$tag-embedding.stdout.log") -RedirectStandardError (Join-Path $runtime "$tag-embedding.stderr.log")
    $deadline=[DateTime]::UtcNow.AddSeconds(30)
    while([DateTime]::UtcNow -lt $deadline){if(Test-Embedding){Write-Host "Local CPU embedding ready, owned PID $($process.Id). No text model or Workbench was started.";return};$process.Refresh();if($process.HasExited){throw 'Owned embedding exited; inspect runtime logs.'};Start-Sleep -Milliseconds 250}
    throw "Owned embedding PID $($process.Id) is not ready; inspect logs. It was not force-killed."
}
Push-Location $root
try {
    switch($Action){
        'Status' { & $python.Path -s -B (Join-Path $PSScriptRoot 'library_operator.py') status; Write-Host "Embedding port $embeddingPort ready: $(Test-Embedding)"; exit $LASTEXITCODE }
        'StartEmbedding' { Start-Embedding; exit 0 }
        'Search' { if(-not $Query){throw '-Query is required.'}; if(-not(Test-Embedding)){throw 'Run -Action StartEmbedding first.'}; & $knowledgePython -s -B (Join-Path $PSScriptRoot 'library_knowledge.py') query --stage-id $StageId --query $Query; exit $LASTEXITCODE }
        'Ask' { if(-not $Query){throw '-Query is required.'}; if(-not(Test-Path -LiteralPath $effectiveGrant)){throw 'Project read grant missing; finish the reviewed project binding first.'}; . (Join-Path $PSScriptRoot 'node_runtime.ps1'); $node=Resolve-ScholarFlowNode; Start-Embedding; & $node (Join-Path $root 'packages\scholarflow-pi\scripts\ask-library.mjs') --stage $StageId --question $Query --grant $effectiveGrant; exit $LASTEXITCODE }
        'Chat' {
            if(-not(Test-Path -LiteralPath $effectiveGrant)){throw 'Project read grant missing.'}
            $capabilities=@('library-read')
            $previousContinuity=$env:SCHOLARFLOW_CONTINUITY_GRANT_FILE
            try {
                # Explicit opt-in only: never infer a workflow or inherit another
                # task's checkpoint authority from a parent process.
                Remove-Item Env:SCHOLARFLOW_CONTINUITY_GRANT_FILE -ErrorAction SilentlyContinue
                if($ContinuityGrantFile){
                    $continuityPath=(Resolve-Path -LiteralPath $ContinuityGrantFile -ErrorAction Stop).Path
                    $continuity=Get-Content -LiteralPath $continuityPath -Raw | ConvertFrom-Json
                    if($continuity.stage_id -cne $StageId -or [IO.Path]::GetFullPath($continuity.library_grant_file) -cne [IO.Path]::GetFullPath($effectiveGrant)){throw 'Continuity grant must match the exact research project and library read grant.'}
                    $env:SCHOLARFLOW_CONTINUITY_GRANT_FILE=$continuityPath
                    $capabilities+=@('research-continuity')
                }
                Start-Embedding
                & (Join-Path $PSScriptRoot 'scholarflow-local.ps1') -Tunnel -DataDir (Join-Path $libraryHome 'facts') -Capabilities $capabilities -KnowledgeReadGrantFile $effectiveGrant
                $chatExit=$LASTEXITCODE
            } finally {
                if($null -eq $previousContinuity){Remove-Item Env:SCHOLARFLOW_CONTINUITY_GRANT_FILE -ErrorAction SilentlyContinue}else{$env:SCHOLARFLOW_CONTINUITY_GRANT_FILE=$previousContinuity}
            }
            exit $chatExit
        }
        'Coverage' { & $python.Path -s -B (Join-Path $PSScriptRoot 'library-coverage.py'); exit $LASTEXITCODE }
        'DeltaPlan' { & $knowledgePython -s -B (Join-Path $PSScriptRoot 'library-delta.py') plan --project $StageId; exit $LASTEXITCODE }
        'DeltaApply' { if(-not $PlanId -or -not $ReviewedHash){throw '-PlanId and -ReviewedHash are required.'}; Start-Embedding; & $knowledgePython -s -B (Join-Path $PSScriptRoot 'library-delta.py') apply --plan-id $PlanId --reviewed-hash $ReviewedHash --operator; exit $LASTEXITCODE }
        'ProjectIndexPlan' { & $knowledgePython -s -B (Join-Path $PSScriptRoot 'project-library-index.py') plan --project $StageId; exit $LASTEXITCODE }
        'ProjectIndexApply' { if(-not $PlanId -or -not $ReviewedHash){throw '-PlanId and -ReviewedHash are required.'}; Start-Embedding; & $knowledgePython -s -B (Join-Path $PSScriptRoot 'project-library-index.py') run --plan-id $PlanId --reviewed-hash $ReviewedHash --operator; exit $LASTEXITCODE }
    }
} finally { Pop-Location }
