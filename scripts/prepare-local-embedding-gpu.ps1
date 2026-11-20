[CmdletBinding()]
param([switch]$StartCandidate)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$candidateRoot=Join-Path $root '.local\research-library\runtime-candidates\llama-b9631-vulkan'
$archive=Join-Path $candidateRoot 'llama-b9631-bin-win-vulkan-x64.zip'
$extract=Join-Path $candidateRoot 'runtime'
[IO.Directory]::CreateDirectory($candidateRoot)|Out-Null
if(-not(Test-Path -LiteralPath $archive)){Invoke-WebRequest 'https://github.com/ggml-org/llama.cpp/releases/download/b9631/llama-b9631-bin-win-vulkan-x64.zip' -OutFile $archive -TimeoutSec 120}
if((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '1A83D4A49DFDEFA7DFF73ABFEAD4F2F8971525722BB175936E6957C5A5F44BCD'){throw 'Official release checksum mismatch; candidate not executed.'}
if(-not(Test-Path -LiteralPath $extract)){
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($archive)
    try{foreach($entry in $zip.Entries){$target=[IO.Path]::GetFullPath((Join-Path $extract $entry.FullName));if(-not $target.StartsWith($extract+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe archive path'}}}finally{$zip.Dispose()}
    Expand-Archive -LiteralPath $archive -DestinationPath $extract
}
$servers=@(Get-ChildItem -LiteralPath $extract -Filter llama-server.exe -Recurse -File)
if($servers.Count -ne 1){throw 'Expected exactly one candidate server'}
$exe=$servers[0].FullName
& $exe --list-devices
if($LASTEXITCODE -ne 0){throw 'Candidate cannot enumerate GPU; CPU unchanged.'}
if($StartCandidate){
    if(Get-NetTCPConnection -LocalPort 8082 -State Listen -ErrorAction SilentlyContinue){throw 'Candidate port8082 occupied; no process stopped.'}
    $model=Join-Path $root '.tmp\lightrag-native\models\Qwen3-Embedding-0.6B-Q8_0.gguf'
    if((Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash -ne '06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'){throw 'Model differs from reviewed CPU model'}
    $tag=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
    $p=Start-Process -FilePath $exe -ArgumentList @('-m',('"'+$model+'"'),'--embedding','--pooling','last','--host','127.0.0.1','--port','8082','-c','8192','-b','512','-ngl','99','-t','4','-tb','4','--cache-ram','0','--no-cache-prompt','--log-verbosity','0') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $candidateRoot "$tag.stdout.log") -RedirectStandardError (Join-Path $candidateRoot "$tag.stderr.log")
    [pscustomobject]@{pid=$p.Id;port=8082;executable=$exe;cpu_service_unchanged=$true;prompt_cache_ram_mib=0;logs_tag=$tag}|ConvertTo-Json -Compress
}
