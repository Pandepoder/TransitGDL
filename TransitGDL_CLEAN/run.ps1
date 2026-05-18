$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example"
}

if (-not (Test-Path "data/gtfs_guadalajara.zip")) {
    Write-Host "Generating demo GTFS..."
    python scripts/seed_demo.py --force
}

$env:PYTHONPATH = "."
$env:DEMO_SIMULATOR = "true"
$env:USE_MEMORY_CACHE = "true"
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
