[CmdletBinding(PositionalBinding = $false)]
param(
    [string] $DataDir = ".tmp\scholarflow-cli",
    [string[]] $Capabilities = @("project-evidence"),
    [switch] $LibraryRead,
    [switch] $Status,
    [string] $WriteGrantFile,
    [string] $TaskWriteGrantFile,
    [string] $NoteWriteGrantFile,
    [string] $NoteProjectGrantFile,
    [string] $PdfReadGrantFile,
    [string] $PdfNoteGrantFile,
    [string] $KnowledgeReadGrantFile,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $PiArgs
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "python_runtime.ps1")
$python = Resolve-ScholarFlowPython -RepoRoot $root -MinimumVersion ([version]"3.11.0")
. (Join-Path $PSScriptRoot "node_runtime.ps1")
$node = Resolve-ScholarFlowNode
$package = Join-Path $root "packages\scholarflow-pi"
$profile = Join-Path $package "profiles\minimal"
$cli = Join-Path $package "node_modules\@earendil-works\pi-coding-agent\dist\cli.js"
if (-not (Test-Path -LiteralPath $cli)) { throw "Install the reviewed project-local Pi dependencies first." }
$previous = @{}
foreach ($name in @("SCHOLARFLOW_PYTHON", "SCHOLARFLOW_DATA_DIR", "PYTHONNOUSERSITE", "SCHOLARFLOW_CAPABILITIES", "SCHOLARFLOW_PAPER_SCOPE", "SCHOLARFLOW_PROJECT_WRITE_GRANT_FILE", "SCHOLARFLOW_TASK_WRITE_GRANT_FILE", "SCHOLARFLOW_NOTE_WRITE_GRANT_FILE", "SCHOLARFLOW_NOTE_PROJECT_GRANT_FILE", "SCHOLARFLOW_PDF_READ_GRANT_FILE", "SCHOLARFLOW_PDF_NOTE_GRANT_FILE", "SCHOLARFLOW_LIBRARY_GRANT_FILE")) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
Push-Location $profile
try {
    $env:SCHOLARFLOW_PYTHON = $python.Path
    $env:PYTHONNOUSERSITE = "1"
    $env:SCHOLARFLOW_DATA_DIR = if ([IO.Path]::IsPathRooted($DataDir)) { $DataDir } else { Join-Path $root $DataDir }
    $env:SCHOLARFLOW_CAPABILITIES = $Capabilities -join ","
    $env:SCHOLARFLOW_PAPER_SCOPE = if ($LibraryRead) { "local-catalog-read" } else { "" }
    $env:SCHOLARFLOW_PROJECT_WRITE_GRANT_FILE = if ($WriteGrantFile) { if ([IO.Path]::IsPathRooted($WriteGrantFile)) { $WriteGrantFile } else { Join-Path $root $WriteGrantFile } } else { "" }
    $env:SCHOLARFLOW_TASK_WRITE_GRANT_FILE = if ($TaskWriteGrantFile) { if ([IO.Path]::IsPathRooted($TaskWriteGrantFile)) { $TaskWriteGrantFile } else { Join-Path $root $TaskWriteGrantFile } } else { "" }
    $env:SCHOLARFLOW_NOTE_WRITE_GRANT_FILE = if ($NoteWriteGrantFile) { if ([IO.Path]::IsPathRooted($NoteWriteGrantFile)) { $NoteWriteGrantFile } else { Join-Path $root $NoteWriteGrantFile } } else { "" }
    $env:SCHOLARFLOW_NOTE_PROJECT_GRANT_FILE = if ($NoteProjectGrantFile) { if ([IO.Path]::IsPathRooted($NoteProjectGrantFile)) { $NoteProjectGrantFile } else { Join-Path $root $NoteProjectGrantFile } } else { "" }
    $env:SCHOLARFLOW_PDF_READ_GRANT_FILE = if ($PdfReadGrantFile) { if ([IO.Path]::IsPathRooted($PdfReadGrantFile)) { $PdfReadGrantFile } else { Join-Path $root $PdfReadGrantFile } } else { "" }
    $env:SCHOLARFLOW_PDF_NOTE_GRANT_FILE = if ($PdfNoteGrantFile) { if ([IO.Path]::IsPathRooted($PdfNoteGrantFile)) { $PdfNoteGrantFile } else { Join-Path $root $PdfNoteGrantFile } } else { "" }
    $env:SCHOLARFLOW_LIBRARY_GRANT_FILE = if ($KnowledgeReadGrantFile) { if ([IO.Path]::IsPathRooted($KnowledgeReadGrantFile)) { $KnowledgeReadGrantFile } else { Join-Path $root $KnowledgeReadGrantFile } } else { "" }
    if ($Status) {
        $compositionJson = & $node (Join-Path $package "harness\composition.mjs") --status
        if ($LASTEXITCODE -ne 0) { throw "Capability composition failed." }
        $composition = $compositionJson | ConvertFrom-Json
        $grant = @{ principal = "pi-local"; capabilities = @($composition.capabilities | Where-Object { $_ -in @("project-evidence", "paper-read") }); project_catalog = $composition.capabilities -contains "project-evidence"; paper_catalog = [bool]$LibraryRead; stage_ids = @() } | ConvertTo-Json -Compress
        $auditArgs = @()
        if ($env:SCHOLARFLOW_READ_AUDIT_PATH) {
            $auditPath = if ([IO.Path]::IsPathRooted($env:SCHOLARFLOW_READ_AUDIT_PATH)) { $env:SCHOLARFLOW_READ_AUDIT_PATH } else { Join-Path $root $env:SCHOLARFLOW_READ_AUDIT_PATH }
            $auditArgs = @("--audit-path", $auditPath)
        }
        $domainJson = $grant | & $python.Path (Join-Path $package "harness\operation_runner.py") --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-stdin --status @auditArgs
        $result = $LASTEXITCODE
        $writeStatus = $null
        if ($composition.capabilities -contains "project-write") {
            $writeJson = & $python.Path (Join-Path $package "capabilities\project-write\adapter.py") --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-file $env:SCHOLARFLOW_PROJECT_WRITE_GRANT_FILE --status
            if ($LASTEXITCODE -ne 0) { $result = $LASTEXITCODE }
            $writeStatus = $writeJson | ConvertFrom-Json
        }
        $taskWriteStatus = $null
        if ($composition.capabilities -contains "task-write") {
            $taskWriteJson = & $python.Path (Join-Path $package "capabilities\task-write\adapter.py") --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-file $env:SCHOLARFLOW_TASK_WRITE_GRANT_FILE --status
            if ($LASTEXITCODE -ne 0) { $result = $LASTEXITCODE }
            $taskWriteStatus = $taskWriteJson | ConvertFrom-Json
        }
        $noteWriteStatus = $null
        if ($composition.capabilities -contains "note-write") {
            $noteWriteJson = & $python.Path (Join-Path $package "capabilities\note-write\adapter.py") --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-file $env:SCHOLARFLOW_NOTE_WRITE_GRANT_FILE --status
            if ($LASTEXITCODE -ne 0) { $result = $LASTEXITCODE }
            $noteWriteStatus = $noteWriteJson | ConvertFrom-Json
        }
        $noteProjectStatus = $null
        if ($composition.capabilities -contains "note-project") {
            $noteProjectJson = & $python.Path (Join-Path $package "capabilities\note-project\adapter.py") --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-file $env:SCHOLARFLOW_NOTE_PROJECT_GRANT_FILE --status
            if ($LASTEXITCODE -ne 0) { $result = $LASTEXITCODE }
            $noteProjectStatus = $noteProjectJson | ConvertFrom-Json
        }
        $pdfReadStatus = $null
        if ($composition.capabilities -contains "pdf-read") {
            $pdfJson = & $python.Path (Join-Path $package "capabilities\pdf-read\adapter.py") --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-file $env:SCHOLARFLOW_PDF_READ_GRANT_FILE --status
            if ($LASTEXITCODE -ne 0) { $result = $LASTEXITCODE }
            $pdfReadStatus = $pdfJson | ConvertFrom-Json
        }
        $pdfNoteStatus = $null
        if ($composition.capabilities -contains 'pdf-note') {
            $json = & $python.Path (Join-Path $package 'capabilities\pdf-note\adapter.py') --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-file $env:SCHOLARFLOW_PDF_NOTE_GRANT_FILE --status
            if ($LASTEXITCODE -ne 0) { $result = $LASTEXITCODE }
            $pdfNoteStatus = $json | ConvertFrom-Json
        }
        $knowledgeReadStatus = $null
        if ($composition.capabilities -contains 'library-read') {
            $json = & $python.Path (Join-Path $package 'capabilities\library-read\adapter.py') --data-dir $env:SCHOLARFLOW_DATA_DIR --grant-file $env:SCHOLARFLOW_LIBRARY_GRANT_FILE --status
            if ($LASTEXITCODE -ne 0) { $result = $LASTEXITCODE }
            $knowledgeReadStatus = $json | ConvertFrom-Json
        }
        @{ composition = $composition; domain = ($domainJson | ConvertFrom-Json); project_write = $writeStatus; task_write = $taskWriteStatus; note_write = $noteWriteStatus; note_project = $noteProjectStatus; pdf_read = $pdfReadStatus; pdf_note = $pdfNoteStatus; knowledge_read = $knowledgeReadStatus } | ConvertTo-Json -Depth 10 -Compress
    } else {
        $runtimeArgsJson = & $node (Join-Path $package "harness\composition.mjs")
        if ($LASTEXITCODE -ne 0) { throw "Capability composition failed." }
        $runtimeArgs = @($runtimeArgsJson | ConvertFrom-Json)
        Write-Host "ScholarFlow minimal: Pi-native, explicit capability effects, no Workbench or knowledge daemon required."
        & $node $cli @runtimeArgs @PiArgs
        $result = $LASTEXITCODE
    }
}
finally {
    Pop-Location
    foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process") }
}
exit $result
