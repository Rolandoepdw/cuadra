# start_cuadra.ps1 — starts cuadra-core (bot + dashboard + queue).
#
# Convenience wrapper so you can boot the service without remembering the
# uvicorn command. The core serves the dashboard itself, so once it is up you
# can open it from the laptop or phone on the same network (PIN protected).
#
# The console NEVER closes on its own: it always waits for Enter, whether the
# start succeeds or fails, so you can read what happened.
#
# Examples:
#   .\start_cuadra.ps1              # foreground (Ctrl+C to stop)
#   .\start_cuadra.ps1 -Background  # background, log to data\logs\core.out.log
#   .\start_cuadra.ps1 -Host 127.0.0.1 -Port 8321

[CmdletBinding()]
param(
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8321,
    [switch]$Background
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root "backend"
$python = Join-Path $backend ".venv\Scripts\python.exe"
$script:exitCode = 0

try {
    if (-not (Test-Path $python)) {
        throw "No encuentro el entorno virtual en $python. Crea el venv en backend\.venv primero."
    }

    # Never launch a second core. The instance guard would reject it and print a
    # raw traceback, and in -Background mode the health poll would then report a
    # success that belongs to the previous process. Check health first, then the
    # lock file so a core that is still starting is reported instead of cloned.
    $healthUrl = "http://127.0.0.1:$Port/api/health"
    $coreIsUp = $false
    try {
        # The health endpoint inspects the LLM/GPU and can take ~5 s on this
        # laptop, so a 2 s probe would misread a healthy core as missing.
        $resp = Invoke-WebRequest -Uri $healthUrl -Headers @{ "X-PIN" = "0000" } -UseBasicParsing -TimeoutSec 10
        $coreIsUp = ($resp.StatusCode -eq 200)
    } catch { }

    $lockPath = Join-Path $backend "data\cuadra.lock"
    if ($coreIsUp) {
        $holderPid = "?"
        if (Test-Path $lockPath) {
            $firstLine = Get-Content $lockPath -TotalCount 1 -ErrorAction SilentlyContinue
            if ($firstLine) { $holderPid = $firstLine.Trim() }
        }
        Write-Host ""
        Write-Host "  Ya hay un cuadra-core respondiendo en http://127.0.0.1:$Port (pid $holderPid)." -ForegroundColor Yellow
        Write-Host "  No arranco una segunda instancia." -ForegroundColor Yellow
        Write-Host "  Para reiniciarlo:  .\stop_cuadra.ps1  y luego este script." -ForegroundColor DarkGray
        Write-Host ""
        return
    }
    if (Test-Path $lockPath) {
        $lockPid = 0
        $firstLine = Get-Content $lockPath -TotalCount 1 -ErrorAction SilentlyContinue
        [void][int]::TryParse("$firstLine".Trim(), [ref]$lockPid)
        if ($lockPid -gt 0 -and (Get-Process -Id $lockPid -ErrorAction SilentlyContinue)) {
            Write-Host ""
            Write-Host "  Ya hay un cuadra-core arrancando o activo (pid $lockPid), pero todavía no responde en el puerto $Port." -ForegroundColor Yellow
            Write-Host "  Espera unos segundos; si no responde, usa .\stop_cuadra.ps1 y reintenta." -ForegroundColor DarkGray
            Write-Host ""
            return
        }
    }

    $webIndex = Join-Path $backend "cuadra\web_dashboard\index.html"
    if (-not (Test-Path $webIndex)) {
        Write-Host "El dashboard no está compilado. Ejecutando build_dashboard.ps1..." -ForegroundColor Yellow
        & (Join-Path $root "build_dashboard.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "La compilación del dashboard falló (código $LASTEXITCODE)."
        }
    }

    # Local IP to open the dashboard from the phone (best-effort).
    $lanIp = $null
    try {
        $lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            Select-Object -First 1 -ExpandProperty IPAddress)
    } catch { }

    Write-Host ""
    Write-Host "  Cuadra arrancando..." -ForegroundColor Cyan
    Write-Host "  Laptop:  http://127.0.0.1:$Port" -ForegroundColor Green
    if ($lanIp) {
        Write-Host "  Móvil:   http://${lanIp}:$Port   (misma red, PIN)" -ForegroundColor Green
    }
    Write-Host ""

    $arguments = @("-m", "uvicorn", "cuadra.main:app", "--host", $BindHost, "--port", "$Port")

    if ($Background) {
        $logDir = Join-Path $backend "data\logs"
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $outLog = Join-Path $logDir "core.out.log"
        Write-Host "En segundo plano. Log: $outLog" -ForegroundColor DarkGray

        # Start-Process with redirection may print a spurious PowerShell error
        # ("ChildProcess.kill"); the process starts anyway. That is why we verify
        # by querying the core instead of trusting the message.
        try {
            Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $backend `
                -RedirectStandardOutput $outLog -RedirectStandardError "$outLog.err" -WindowStyle Hidden
        } catch {
            Write-Host "  aviso al lanzar el proceso: $($_.Exception.Message)" -ForegroundColor Yellow
        }

        $health = "http://127.0.0.1:$Port/api/health"
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            try {
                $resp = Invoke-WebRequest -Uri $health -Headers @{ "X-PIN" = "0000" } -UseBasicParsing -TimeoutSec 10
                if ($resp.StatusCode -eq 200) { $ready = $true; break }
            } catch { }
        }

        if ($ready) {
            Write-Host "cuadra-core arrancado y respondiendo en http://127.0.0.1:$Port" -ForegroundColor Green
        } else {
            $script:exitCode = 1
            Write-Host "cuadra-core lanzado, pero aún no responde. Revisa $outLog" -ForegroundColor Yellow
        }
        Write-Host "Para pararlo:  .\stop_cuadra.ps1" -ForegroundColor DarkGray
    } else {
        Push-Location $backend
        try {
            & $python @arguments
            if ($LASTEXITCODE -ne 0) {
                $script:exitCode = $LASTEXITCODE
                Write-Host "cuadra-core terminó con código $LASTEXITCODE." -ForegroundColor Yellow
            }
        } finally {
            Pop-Location
        }
    }
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
