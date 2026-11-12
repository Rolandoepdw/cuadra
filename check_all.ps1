# check_all.ps1 - local verification before committing (tests + dashboard).
#
# There is no remote CI: this script runs what a pipeline would require, offline.
# The core may be running (tests use a throwaway database).
#
#   .\check_all.ps1              # tests + types + dashboard build
#   .\check_all.ps1 -SkipBuild   # tests + types (faster)
#
# Exits non-zero if anything fails. ASCII only on purpose: Windows PowerShell 5
# reads BOM-less files as ANSI and mangles UTF-8 punctuation.

[CmdletBinding()]
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root "backend"
$dashboard = Join-Path $root "dashboard"
$python = Join-Path $backend ".venv\Scripts\python.exe"
$script:failed = @()

# PowerShell 5 treats native stderr as a terminating error when the preference is
# Stop, and the test runner prints expected warnings there. Run with Continue and
# decide by exit code.
function Invoke-Native {
    param([scriptblock]$Action)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Action
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host ""
    Write-Host "== $Name" -ForegroundColor Cyan
    $started = Get-Date
    try {
        & $Action
    } catch {
        Write-Host "  ERROR: $($_.Exception.Message)" -ForegroundColor Red
        $script:failed += $Name
        return
    }
    $elapsed = [int]((Get-Date) - $started).TotalSeconds
    Write-Host "  OK (${elapsed}s)" -ForegroundColor Green
}

Step "Backend tests" {
    if (-not (Test-Path $python)) { throw "not found: $python" }
    $stderrFile = Join-Path $env:TEMP "cuadra_check_all_tests.err"
    $code = Invoke-Native {
        Push-Location $backend
        try { & $python -m unittest discover -s tests -q 2>$stderrFile }
        finally { Pop-Location }
    }
    if (Test-Path $stderrFile) {
        # Expected warnings (rate limits, guardrails) live here; show them.
        Get-Content $stderrFile | Out-Host
        Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) { throw "unittest exited $code" }
}

Step "Dashboard types" {
    $code = Invoke-Native {
        Push-Location $dashboard
        try { & npx tsc --noEmit }
        finally { Pop-Location }
    }
    if ($code -ne 0) { throw "tsc exited $code" }
}

if (-not $SkipBuild) {
    Step "Dashboard build" {
        $buildScript = Join-Path $root "build_dashboard.ps1"
        $proc = Start-Process -FilePath "powershell" `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $buildScript) `
            -NoNewWindow -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "build_dashboard.ps1 exited $($proc.ExitCode)" }
    }
}

Write-Host ""
if ($script:failed.Count -gt 0) {
    Write-Host ("FAILED: " + ($script:failed -join ", ")) -ForegroundColor Red
    exit 1
}
Write-Host "All green." -ForegroundColor Green
exit 0
