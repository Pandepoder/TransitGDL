$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example"
}

if (-not (Test-Path "data/gtfs_amg.zip")) {
    Write-Host "Descargando el GTFS oficial del AMG..."
    python scripts/download_gtfs.py
    # Si la descarga falla, la app genera sola un GTFS de demostracion.
}

$env:PYTHONPATH = "."
$env:DEMO_SIMULATOR = "true"
$env:USE_MEMORY_CACHE = "true"
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
