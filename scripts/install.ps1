$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (Get-Command pipx -ErrorAction SilentlyContinue) {
    pipx install --force $Root
} else {
    $Base = Join-Path $env:LOCALAPPDATA 'DADO'
    $Venv = Join-Path $Base 'venv'
    py -m venv $Venv
    & (Join-Path $Venv 'Scripts/python.exe') -m pip install --upgrade $Root
    $Bin = Join-Path $env:USERPROFILE '.local\bin'
    New-Item -ItemType Directory -Force -Path $Bin | Out-Null
    $Wrapper = Join-Path $Bin 'dado.cmd'
    if (Test-Path $Wrapper) {
        $Existing = Get-Content -Raw $Wrapper
        if (-not $Existing.Contains('DADO CLI')) { throw "Refusing to overwrite existing $Wrapper" }
    }
    Set-Content -Path $Wrapper -Value "@rem DADO CLI`r`n@echo off`r`n`"$(Join-Path $Venv 'Scripts/python.exe')`" -m dado.cli %*`r`n"
    Write-Host "Installed isolated CLI. Add $Bin to PATH if needed."
}
Write-Host "DADO CLI installed. Run 'dado init' separately inside each project."
