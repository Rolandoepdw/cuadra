#Requires -Version 5.1
<#
.SYNOPSIS
    Downloads the upstream llama.cpp Windows binaries used by Cuadra.

.DESCRIPTION
    Fetches the official ggml-org/llama.cpp release assets (CUDA + optional CPU
    fallback) into backend/tools and writes a VERSION.txt marker.

    Run it again with a newer -Version to upgrade. Existing complete zips are
    skipped; interrupted downloads are resumed with curl -C -.

.NOTES
    Antivirus: several engines flag llama.cpp binaries as `IDP.Generic` (a
    behavior-shield false positive) and silently quarantine them AFTER they have
    already run once. If `llama-server.exe` vanishes mid-run, add an exclusion
    for `backend\tools` (and `backend\models`) in your AV UI and re-run this
    script. Avast keeps its quarantine index at
    `C:\ProgramData\Avast Software\Avast\chest\index.xml`.

.EXAMPLE
    .\install_llm.ps1
    .\install_llm.ps1 -Version b11739
    .\install_llm.ps1 -SkipCpu -ForceRedownload
#>
[CmdletBinding()]
param(
    [string]$Version = "b11739",
    [string]$CudaFlavor = "cuda-13.4",
    [switch]$SkipCpu,
    [switch]$ForceRedownload
)

$ErrorActionPreference = "Stop"

$backendDir = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $backendDir "tools"
$downloadDir = Join-Path $toolsDir "_downloads"
$gpuDir = Join-Path $toolsDir "llamacpp"
$cpuDir = Join-Path $toolsDir "llamacpp-cpu"

New-Item -ItemType Directory -Force -Path $downloadDir, $gpuDir, $cpuDir | Out-Null

$baseUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$Version"

$assets = @(
    [pscustomobject]@{
        Zip   = "llama-$Version-bin-win-$CudaFlavor-x64.zip"
        Url   = "$baseUrl/llama-$Version-bin-win-$CudaFlavor-x64.zip"
        Dest  = $gpuDir
        MinMB = 50
    },
    [pscustomobject]@{
        Zip   = "cudart-llama-bin-win-$CudaFlavor-x64.zip"
        Url   = "$baseUrl/cudart-llama-bin-win-$CudaFlavor-x64.zip"
        Dest  = $gpuDir
        MinMB = 200
    }
)

if (-not $SkipCpu) {
    $assets += [pscustomobject]@{
        Zip   = "llama-$Version-bin-win-cpu-x64.zip"
        Url   = "$baseUrl/llama-$Version-bin-win-cpu-x64.zip"
        Dest  = $cpuDir
        MinMB = 10
    }
}

foreach ($asset in $assets) {
    $zipPath = Join-Path $downloadDir $asset.Zip
    Write-Host "==> $($asset.Zip)" -ForegroundColor Cyan

    $complete = $false
    if ((Test-Path $zipPath) -and -not $ForceRedownload) {
        $sizeMB = (Get-Item $zipPath).Length / 1MB
        if ($sizeMB -ge $asset.MinMB) {
            $complete = $true
            Write-Host ("    already cached ({0:N1} MB)" -f $sizeMB)
        }
    }

    if (-not $complete) {
        & curl.exe -L --fail --ssl-no-revoke --retry 5 --retry-all-errors --retry-delay 3 -C - -o $zipPath $asset.Url
        if ($LASTEXITCODE -ne 0) {
            throw "curl failed for $($asset.Url) (exit $LASTEXITCODE)"
        }
        $sizeMB = (Get-Item $zipPath).Length / 1MB
        if ($sizeMB -lt $asset.MinMB) {
            throw "$($asset.Zip) looks truncated ($([math]::Round($sizeMB, 1)) MB < $($asset.MinMB) MB)"
        }
        Write-Host ("    downloaded {0:N1} MB" -f $sizeMB)
    }

    $extractDir = Join-Path $downloadDir ("_extract_" + [IO.Path]::GetFileNameWithoutExtension($asset.Zip))
    if (Test-Path $extractDir) {
        Remove-Item -LiteralPath $extractDir -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

    $sourceDir = $extractDir
    $children = @(Get-ChildItem -LiteralPath $extractDir)
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        $sourceDir = $children[0].FullName
    }

    Copy-Item -Path (Join-Path $sourceDir "*") -Destination $asset.Dest -Recurse -Force
    Remove-Item -LiteralPath $extractDir -Recurse -Force
    Write-Host "    extracted -> $($asset.Dest)"
}

$stamp = @(
    "llama.cpp release: $Version"
    "source: $baseUrl"
    "installed: $(Get-Date -Format o)"
) -join "`n"

Set-Content -LiteralPath (Join-Path $gpuDir "VERSION.txt") -Value $stamp -Encoding ASCII
if (-not $SkipCpu) {
    Set-Content -LiteralPath (Join-Path $cpuDir "VERSION.txt") -Value $stamp -Encoding ASCII
}

Write-Host ""
Write-Host "--- verification ---" -ForegroundColor Cyan
$gpuServer = Join-Path $gpuDir "llama-server.exe"
$cpuServer = Join-Path $cpuDir "llama-server.exe"

$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"

foreach ($server in @($gpuServer, $cpuServer)) {
    if (Test-Path $server) {
        Write-Host "==> $server"
        $versionOutput = & $server --version 2>&1
        $versionOutput | Select-Object -First 4 | ForEach-Object { Write-Host "    $_" }
    }
    else {
        Write-Warning "$server NOT FOUND. A previous extraction may have been quarantined by your antivirus (IDP.Generic). Add an exclusion for $toolsDir and re-run."
    }
}

if (Test-Path $gpuServer) {
    Write-Host "==> CUDA devices visible to llama.cpp"
    $deviceOutput = & $gpuServer --list-devices 2>&1
    $deviceOutput | Select-Object -First 14 | ForEach-Object { Write-Host "    $_" }
}

$ErrorActionPreference = $previousPreference

Write-Host ""
Write-Host "Done. GPU runtime: $gpuDir" -ForegroundColor Green
if (-not $SkipCpu) {
    Write-Host "CPU fallback: $cpuDir" -ForegroundColor Green
}
