#Requires -Version 5.1
<#
.SYNOPSIS
    Starts Cuadra's own local llama.cpp server (standalone).

.DESCRIPTION
    Thin wrapper over `python -m cuadra.ai.llm_server start`. Useful to run the
    model without starting the whole app. In normal operation Cuadra starts the
    server itself on boot (CUADRA_LLM_AUTOSTART=1).

.EXAMPLE
    .\start_llm.ps1
    .\start_llm.ps1 -Model Bonsai-8B-Q1_0.gguf
#>
[CmdletBinding()]
param(
    [string]$Model
)

$ErrorActionPreference = "Stop"
$backendDir = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

$arguments = @("-m", "cuadra.ai.llm_server", "start")
if ($Model) { $arguments += @("--model", $Model) }

Push-Location $backendDir
try {
    & $python @arguments
}
finally {
    Pop-Location
}
