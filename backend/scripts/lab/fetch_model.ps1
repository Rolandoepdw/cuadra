#Requires -Version 5.1
<#
.SYNOPSIS
    Copies ONE candidate GGUF into backend/models for testing (one at a time).

.DESCRIPTION
    Cuadra keeps its own copy of the model it uses. This script moves candidates
    from the local LM Studio store into the repo, one at a time, so disk usage
    stays bounded while we compare models.

.EXAMPLE
    .\fetch_model.ps1 -List
    .\fetch_model.ps1 -Fetch nemotron-4b
    .\fetch_model.ps1 -Clean
#>
[CmdletBinding()]
param(
    [string]$SourceDir = "C:\Users\<usuario>\.lmstudio\models",
    [string]$Fetch,
    [switch]$List,
    [switch]$Clean,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$backendDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$modelsDir = Join-Path $backendDir "models"
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null

# key -> relative path inside the LM Studio models store
$candidates = [ordered]@{
    "qwen35-2b-q6"    = "lmstudio-community\Qwen3.5-2B-Q6_K\Qwen3.5-2B-Q6_K.gguf"
    "qwen35-2b-q4"    = "lmstudio-community\Qwen3.5-2B-Q4_K_M\Qwen3.5-2B-Q4_K_M.gguf"
    "nemotron-4b"     = "lmstudio-community\NVIDIA-Nemotron-3-Nano-4B-GGUF\NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf"
    "bonsai-27b"      = "lmstudio-community\Bonsai-27B-GGUF\Bonsai-27B-Q1_0.gguf"
    "bonsai-8b"       = "lmstudio-community\Bonsai-8B-Q1_0\Bonsai-8B-Q1_0.gguf"
    "bonsai-4b"       = "lmstudio-community\Bonsai-4B-Q1_0\Bonsai-4B-Q1_0.gguf"
    "qwen3vl-4b"      = "lmstudio-community\Qwen3-VL-4B-Instruct-Q4_K_M\Qwen3-VL-4B-Instruct-Q4_K_M.gguf"
}

if ($Clean) {
    $existing = Get-ChildItem -LiteralPath $modelsDir -File -Filter *.gguf -ErrorAction SilentlyContinue
    foreach ($file in $existing) {
        Write-Host "removing $($file.Name)"
        Remove-Item -LiteralPath $file.FullName -Force
    }
    if (-not $existing) { Write-Host "nothing to remove" }
    return
}

if ($List -or -not $Fetch) {
    Write-Host "Candidates (source: $SourceDir)"
    Write-Host ""
    foreach ($key in $candidates.Keys) {
        $path = Join-Path $SourceDir $candidates[$key]
        if (Test-Path -LiteralPath $path) {
            $gb = (Get-Item -LiteralPath $path).Length / 1GB
            Write-Host ("  {0,-16} {1,7:N2} GB  {2}" -f $key, $gb, $candidates[$key])
        }
        else {
            Write-Host ("  {0,-16} MISSING       {1}" -f $key, $candidates[$key])
        }
    }
    Write-Host ""
    Write-Host "Copied now:"
    Get-ChildItem -LiteralPath $modelsDir -File -Filter *.gguf -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host ("  {0} ({1:N2} GB)" -f $_.Name, ($_.Length / 1GB)) }
    return
}

if (-not $candidates.Contains($Fetch)) {
    throw "Unknown candidate '$Fetch'. Use -List to see the keys."
}

$sourcePath = Join-Path $SourceDir $candidates[$Fetch]
if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "Source model not found: $sourcePath"
}

$targetPath = Join-Path $modelsDir (Split-Path -Leaf $sourcePath)
$sourceSize = (Get-Item -LiteralPath $sourcePath).Length
if ((Test-Path -LiteralPath $targetPath) -and -not $Force) {
    $targetSize = (Get-Item -LiteralPath $targetPath).Length
    if ($targetSize -eq $sourceSize) {
        Write-Host "already copied: $targetPath"
        Write-Host "MODEL_PATH=$targetPath"
        return
    }
}

if (-not $Force) {
    $others = Get-ChildItem -LiteralPath $modelsDir -File -Filter *.gguf -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne (Split-Path -Leaf $sourcePath) }
    if ($others) {
        Write-Warning "Removing previously copied model(s) to keep only one:"
        foreach ($file in $others) {
            Write-Host "  $($file.Name)"
            Remove-Item -LiteralPath $file.FullName -Force
        }
    }
}

Write-Host ("copying {0:N2} GB -> {1}" -f ($sourceSize / 1GB), $targetPath)
Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force

Write-Host "MODEL_PATH=$targetPath"
