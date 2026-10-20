# build_dashboard.ps1 — compiles the React dashboard into the backend.
#
# The dashboard is source-versioned under dashboard/. Its build output goes to
# backend/cuadra/web_dashboard/ which is gitignored and served by cuadra-core
# as a static SPA (see main.py). Run this after any frontend change.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dashboard = Join-Path $root "dashboard"

if (-not (Test-Path (Join-Path $dashboard "node_modules"))) {
    Write-Host "Instalando dependencias del dashboard..." -ForegroundColor Cyan
    Push-Location $dashboard
    try { npm install } finally { Pop-Location }
}

Write-Host "Compilando el dashboard (Vite)..." -ForegroundColor Cyan
Push-Location $dashboard
try { npm run build } finally { Pop-Location }

$out = Join-Path $root "backend\cuadra\web_dashboard\index.html"
if (Test-Path $out) {
    Write-Host "OK: dashboard listo en backend\cuadra\web_dashboard\" -ForegroundColor Green
} else {
    throw "La compilación no generó index.html en $out"
}
