# stop_cuadra.ps1 — stops cuadra-core and the LLM server.
#
# Shuts down everything Cuadra starts: the core (bot + dashboard + queue), its
# managed llama-server and any orphan. Useful after running it with
# .\start_cuadra.ps1 -Background, or if something got stuck.
#
# The console NEVER closes on its own: it always waits for Enter, whether the
# stop succeeds or fails, so you can read what happened.
#
# Examples:
#   .\stop_cuadra.ps1            # stop core + LLM
#   .\stop_cuadra.ps1 -CoreOnly  # core only (leave the LLM if managed elsewhere)

[CmdletBinding()]
param(
    [switch]$CoreOnly
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root "backend"
$python = Join-Path $backend ".venv\Scripts\python.exe"
$script:exitCode = 0

function Stop-ByName {
    param([string]$Name, [string]$Label)
    $procs = Get-Process -Name $Name -ErrorAction SilentlyContinue
    if (-not $procs) {
        Write-Host "  ${Label}: no estaba corriendo." -ForegroundColor DarkGray
        return
    }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.Id -Force -ErrorAction Stop
            Write-Host "  ${Label}: detenido (pid $($p.Id))." -ForegroundColor Green
        } catch {
            Write-Host "  ${Label}: no se pudo detener pid $($p.Id): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
}

try {
    Write-Host ""
    Write-Host "  Deteniendo Cuadra..." -ForegroundColor Cyan

    # 1) Managed LLM: via PID file (frees VRAM) and in case it was orphaned.
    if (-not $CoreOnly) {
        if (Test-Path $python) {
            Push-Location $backend
            $prevEap = $ErrorActionPreference
            try {
                # Native command stderr must not abort the script.
                $ErrorActionPreference = "Continue"
                & $python -m cuadra.ai.llm_server stop 2>&1 | Out-Null
                $code = $LASTEXITCODE
                if ($code -ne 0) {
                    Write-Host "  llm: aviso, el comando de parada devolvió código $code." -ForegroundColor Yellow
                }
            } catch {
                Write-Host "  llm: no se pudo ejecutar la parada gestionada: $($_.Exception.Message)" -ForegroundColor Yellow
            } finally {
                $ErrorActionPreference = $prevEap
                Pop-Location
            }
        }
        Stop-ByName -Name "llama-server" -Label "llama-server"
    }

    # 2) Core: venv python processes running uvicorn.
    $cores = @()
    try {
        $cores = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*cuadra.main:app*" })
    } catch {
        Write-Host "  core: no se pudo listar procesos: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    if ($cores.Count -eq 0) {
        Write-Host "  core: no estaba corriendo." -ForegroundColor DarkGray
    } else {
        foreach ($c in $cores) {
            try {
                Stop-Process -Id $c.ProcessId -Force -ErrorAction Stop
                Write-Host "  core: detenido (pid $($c.ProcessId))." -ForegroundColor Green
            } catch {
                Write-Host "  core: no se pudo detener pid $($c.ProcessId): $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
    }

    # 3) Single-instance lock (in case an orphaned lock remains).
    $lock = Join-Path $backend "data\cuadra.lock"
    if (Test-Path $lock) {
        Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
        Write-Host "  lock: liberado." -ForegroundColor Green
    }

    Write-Host "  Listo." -ForegroundColor Green
} catch {
    $script:exitCode = 1
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ScriptStackTrace) {
        Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    }
} finally {
    Write-Host ""
    try {
        Read-Host "Pulsa Enter para cerrar" | Out-Null
    } catch {
        # Non-interactive console: there is nothing to wait for.
    }
    if ($script:exitCode -ne 0) { exit $script:exitCode }
}
