<#
.SYNOPSIS
  Run the whole stack locally on Windows: Postgres, the API, and the UI.

.DESCRIPTION
  The PowerShell counterpart to scripts/dev.sh. Installs dependencies, starts
  Postgres, applies migrations, seeds demo data, clones the frontend beside this
  repo, and runs the API and UI together. Safe to re-run.

.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -NoSeed
  .\scripts\dev.ps1 -Reset
#>
[CmdletBinding()]
param(
    [switch]$NoSeed,
    [switch]$Reset,
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [string]$FrontendRepo = 'https://github.com/marvalarva2929/agentscrape-frontend.git',
    [string]$FrontendDir
)

$ErrorActionPreference = 'Stop'

function Say  { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Warn { param($m) Write-Host "!   $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "x   $m" -ForegroundColor Red; exit 1 }
function Have { param($c) [bool](Get-Command $c -ErrorAction SilentlyContinue) }

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not $FrontendDir) {
    $FrontendDir = Join-Path (Split-Path -Parent $Root) 'agentscrape-frontend'
}

# --- prerequisites ---------------------------------------------------------
if (-not (Have 'node')) { Die 'Node.js is required: https://nodejs.org (or: winget install OpenJS.NodeJS.LTS)' }
if (-not (Have 'git'))  { Die 'Git is required: https://git-scm.com/download/win' }

if (-not (Have 'uv')) {
    Say 'Installing uv (Python toolchain manager)'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    # The installer adds uv to PATH for new shells; make it visible in this one.
    $userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    if ($userHome) {
        $env:Path = (Join-Path $userHome '.local\bin') + [IO.Path]::PathSeparator + $env:Path
    }
}
if (-not (Have 'uv')) { Die 'uv installed but not on PATH. Open a new terminal and re-run.' }

# --- database --------------------------------------------------------------
# Docker Desktop is the documented path; fall back to a local Postgres because
# plenty of machines do not have Docker running.
$DbUrl = $null
$dockerUp = $false
if (Have 'docker') {
    docker info *> $null
    $dockerUp = ($LASTEXITCODE -eq 0)
}

if ($dockerUp) {
    Say 'Starting Postgres in Docker'
    docker compose up -d | Out-Null
    $ready = $false
    foreach ($i in 1..60) {
        docker compose exec -T postgres pg_isready -U agentscrape -d agentscrape *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { Die 'Postgres container did not become ready. Try: docker compose logs postgres' }
    $DbUrl = 'postgresql+asyncpg://agentscrape:agentscrape@localhost:5433/agentscrape'
}
elseif (Have 'pg_isready') {
    pg_isready -q -h localhost -p 5432 *> $null
    if ($LASTEXITCODE -eq 0) {
        Warn 'Docker is not running; using the Postgres already on localhost:5432'
        psql -h localhost -p 5432 -d postgres -q -c "CREATE ROLE agentscrape LOGIN PASSWORD 'agentscrape' SUPERUSER" *> $null
        psql -h localhost -p 5432 -d postgres -q -c 'CREATE DATABASE agentscrape OWNER agentscrape' *> $null
        $DbUrl = 'postgresql+asyncpg://agentscrape:agentscrape@localhost:5432/agentscrape'
    }
}

if (-not $DbUrl) {
    Die @'
No database available. Either:
  - start Docker Desktop and re-run, or
  - install Postgres: winget install PostgreSQL.PostgreSQL.16
'@
}

# --- backend config --------------------------------------------------------
if (-not (Test-Path '.env')) {
    Say 'Creating .env from .env.example'
    Copy-Item '.env.example' '.env'
}

function Set-EnvLine {
    param([string]$Key, [string]$Value)
    $lines = Get-Content '.env'
    if ($lines -match "^$Key=") {
        $lines = $lines -replace "^$Key=.*", "$Key=$Value"
    } else {
        $lines += "$Key=$Value"
    }
    # Plain LF: the file is read by Python, and CRLF ends up inside the values.
    [IO.File]::WriteAllText((Join-Path $Root '.env'), ($lines -join "`n") + "`n")
}

Set-EnvLine 'DATABASE_URL' $DbUrl

# The browser enforces CORS, so the API must name the exact UI origin.
$uiOrigins = "http://localhost:$FrontendPort,http://127.0.0.1:$FrontendPort"
$existing = (Get-Content '.env' | Where-Object { $_ -match '^CORS_ORIGINS=' }) -replace '^CORS_ORIGINS=', ''
if ($existing -and $existing -notlike "*localhost:$FrontendPort*") {
    Set-EnvLine 'CORS_ORIGINS' "$existing,$uiOrigins"
} elseif (-not $existing) {
    Set-EnvLine 'CORS_ORIGINS' $uiOrigins
}

Say 'Installing Python dependencies'
uv sync --quiet
if ($LASTEXITCODE -ne 0) { Die 'uv sync failed.' }

# LOCALAPPDATA only exists on Windows; fall back so the check never throws.
$cacheRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA }
             elseif ($env:HOME) { Join-Path $env:HOME '.cache' }
             else { $null }
$playwrightCache = if ($cacheRoot) { Join-Path $cacheRoot 'ms-playwright' } else { $null }
if (-not $playwrightCache -or -not (Test-Path $playwrightCache)) {
    Say 'Installing headless Chromium (only needed for crawling)'
    uv run playwright install chromium *> $null
    if ($LASTEXITCODE -ne 0) { Warn 'Chromium install failed; crawling will not work, everything else will.' }
}

Say 'Applying database migrations'
uv run alembic upgrade head | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'Migrations failed.' }

if (-not $NoSeed) {
    Say 'Seeding demo data'
    if ($Reset) { uv run agentscrape seed-demo --reset | Out-Null }
    else        { uv run agentscrape seed-demo | Out-Null }
}

# --- frontend --------------------------------------------------------------
if (-not (Test-Path $FrontendDir)) {
    Say "Cloning the frontend to $FrontendDir"
    git clone --quiet $FrontendRepo $FrontendDir
}

Say 'Installing frontend dependencies'
Push-Location $FrontendDir
npm install --silent
Pop-Location

[IO.File]::WriteAllText(
    (Join-Path $FrontendDir '.env.local'),
    "VITE_API_BASE_URL=http://localhost:$BackendPort/api/v1`nVITE_USE_MOCK_API=false`n"
)

# --- run both --------------------------------------------------------------
$api = $null
$ui  = $null
try {
    Say "Starting the API on :$BackendPort"
    $api = Start-Process -FilePath 'uv' `
        -ArgumentList @('run', 'uvicorn', 'agentscrape.api.main:app', '--port', "$BackendPort", '--log-level', 'warning') `
        -WorkingDirectory $Root -NoNewWindow -PassThru

    $healthy = $false
    foreach ($i in 1..40) {
        try {
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 `
                "http://localhost:$BackendPort/api/v1/health" | Out-Null
            $healthy = $true; break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $healthy) {
        Die "The API did not start. Run it directly to see why: uv run uvicorn agentscrape.api.main:app --port $BackendPort"
    }

    Say "Starting the UI on :$FrontendPort"
    $ui = Start-Process -FilePath 'npm' `
        -ArgumentList @('run', 'dev', '--', '--port', "$FrontendPort", '--strictPort') `
        -WorkingDirectory $FrontendDir -NoNewWindow -PassThru

    $clientPw = ((Get-Content '.env' | Where-Object { $_ -match '^APP_PASSWORD=' }) -replace '^APP_PASSWORD=', '')
    $adminPw  = ((Get-Content '.env' | Where-Object { $_ -match '^ADMIN_PASSWORD=' }) -replace '^ADMIN_PASSWORD=', '')

    Write-Host ""
    Write-Host "  ──────────────────────────────────────────────────────────────"
    Write-Host "   Open:      http://localhost:$FrontendPort"
    Write-Host "   API:       http://localhost:$BackendPort/api/v1"
    Write-Host "   API docs:  http://localhost:$BackendPort/api/v1/docs"
    Write-Host ""
    Write-Host "   Passwords (from .env)"
    Write-Host "     client:  $clientPw"
    Write-Host "     admin:   $adminPw"
    Write-Host ""
    Write-Host "   Demo data is already loaded. Ctrl-C stops both."
    Write-Host "  ──────────────────────────────────────────────────────────────"
    Write-Host ""

    Wait-Process -Id $api.Id
}
finally {
    foreach ($p in @($ui, $api)) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
