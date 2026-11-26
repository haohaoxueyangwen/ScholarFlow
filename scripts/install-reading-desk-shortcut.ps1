[CmdletBinding()]
param(
    [string] $AgentHome = '',
    [string] $DesktopPath = ''
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$arguments = ''
if ($AgentHome) {
    $target = Join-Path ([IO.Path]::GetFullPath($AgentHome)) 'bin\scholarflow.cmd'
    $arguments = 'web'
} else {
    $target = Join-Path $root 'ScholarFlow-Desk.bat'
}
$icon = Join-Path $root 'apps\research-workbench\public\brand\scholarflow-icon.ico'
foreach ($file in @($target, $icon)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required reading desk file missing: $file" }
}
$desktop = if ($DesktopPath) { [IO.Path]::GetFullPath($DesktopPath) } else { [Environment]::GetFolderPath('Desktop') }
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop -PathType Container)) { throw 'Desktop directory is unavailable.' }
$name = 'ScholarFlow ' + [char]0x9605 + [char]0x8BFB + [char]0x53F0 + '.lnk'
$path = Join-Path $desktop $name
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($path)
if (Test-Path -LiteralPath $path) {
    if ($link.TargetPath -ine $target -or $link.Arguments -cne $arguments) { throw 'An unrelated shortcut already uses this name; nothing overwritten.' }
    [pscustomobject]@{path=$path; target=$link.TargetPath; already_present=$true; changed=$false} | ConvertTo-Json -Compress
    exit 0
}
$link.TargetPath = $target
$link.Arguments = $arguments
$link.WorkingDirectory = $root
$link.IconLocation = $icon + ',0'
$link.Description = 'ScholarFlow Pi reading desk'
$link.Save()
$verified = $shell.CreateShortcut($path)
if ($verified.TargetPath -ine $target -or $verified.Arguments -cne $arguments -or $verified.WorkingDirectory -ine $root) { throw 'Reading desk shortcut verification failed.' }
[pscustomobject]@{path=$path; target=$verified.TargetPath; icon=$verified.IconLocation; already_present=$false; changed=$true} | ConvertTo-Json -Compress
