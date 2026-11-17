$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "TransitGDL - iniciando..." -ForegroundColor Cyan

# Liberar puerto 8000 si hay un servidor viejo (causa mapa en blanco)
$onPort = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($onPort) {
    $pids = $onPort.OwningProcess | Sort-Object -Unique
    foreach ($procId in $pids) {
        Write-Host "Deteniendo proceso antiguo en :8000 (PID $procId)..." -ForegroundColor Yellow
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Creado .env desde .env.example"
}

if (-not (Test-Path "data/gtfs_amg.zip")) {
    Write-Host "Descargando el GTFS oficial del AMG..."
    python scripts/download_gtfs.py
    # Si la descarga falla, la app genera sola un GTFS de demostracion.
}

$env:PYTHONPATH = "."
$env:DEMO_SIMULATOR = "true"
$env:USE_MEMORY_CACHE = "true"

Write-Host ""
Write-Host "Abre en el navegador:  http://127.0.0.1:8000/" -ForegroundColor Green
Write-Host "NO abras index.html directo debe ser via el servidor." -ForegroundColor Yellow
Write-Host ""

python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
