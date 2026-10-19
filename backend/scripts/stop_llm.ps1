#Requires -Version 5.1
<#
.SYNOPSIS
    Stops Cuadra's local llama.cpp server and frees VRAM.

.EXAMPLE
    .\stop_llm.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$backendDir = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

Push-Location $backendDir
try {
    & $python -m cuadra.ai.llm_server stop
}
finally {
    Pop-Location
}
