# Start the WiFi billing service (development / foreground).
#
#   .\run.ps1              normal start
#   .\run.ps1 -Setup       install dependencies first
#   .\run.ps1 -Tunnel      also open a cloudflared tunnel for M-Pesa callbacks
#
param(
    [switch]$Setup,
    [switch]$Tunnel,
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = "C:\Users\f\AppData\Local\Programs\Python\Python312\python.exe"
}

if (-not (Test-Path "$root\config.json")) {
    Copy-Item "$root\config.example.json" "$root\config.json"
    Write-Host "Created config.json from the example - edit it before going live." -ForegroundColor Yellow
}

if ($Setup) {
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    & $python -m pip install --upgrade pip
    & $python -m pip install -r "$root\requirements.txt"
}

if ($Tunnel) {
    Write-Host "Starting cloudflared tunnel (copy the https URL into server.public_base_url)" -ForegroundColor Cyan
    Start-Process cloudflared -ArgumentList "tunnel", "--url", "http://localhost:$Port"
    Start-Sleep -Seconds 6
}

Write-Host "Portal   http://localhost:$Port" -ForegroundColor Green
Write-Host "Admin    http://localhost:$Port/admin" -ForegroundColor Green
Write-Host "Health   http://localhost:$Port/healthz" -ForegroundColor Green

& $python -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port $Port
