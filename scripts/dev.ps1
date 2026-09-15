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
    [string]$FrontendDir,
    # Skip database detection entirely.
    [string]$DatabaseUrl,
    # Superuser used only to create the agentscrape role and database.
    [string]$PostgresUser = 'postgres',
    [int]$PostgresPort
)

$ErrorActionPreference = 'Stop'

function Say  { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Warn { param($m) Write-Host "!   $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "x   $m" -ForegroundColor Red; exit 1 }
function Have { param($c) [bool](Get-Command $c -ErrorAction SilentlyContinue) }

function Test-PortFree {
    param([int]$Port)
    # Bind it ourselves: the only reliable way to know it is actually free.
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        $listener.Start(); $listener.Stop()
        return $true
    } catch { return $false }
}

function Get-PortHolder {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -First 1
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) { return "$($proc.ProcessName) (PID $($proc.Id))" }
    } catch { }
    return $null
}

function Resolve-Port {
    <# Use the requested port, or the next free one, rather than dying. #>
    param([int]$Requested, [string]$Label)
    if (Test-PortFree $Requested) { return $Requested }

    $holder = Get-PortHolder $Requested
    $detail = if ($holder) { " (in use by $holder)" } else { ' (in use)' }

    foreach ($candidate in ($Requested + 1)..($Requested + 20)) {
        if (Test-PortFree $candidate) {
            Warn "Port $Requested is busy$detail; using $candidate for the $Label instead."
            return $candidate
        }
    }
    Die "Ports $Requested-$($Requested + 20) are all in use. Free one, or pass -${Label}Port."
}

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

# Resolve ports first: .env, CORS and the UI config all embed them.
$BackendPort  = Resolve-Port -Requested $BackendPort  -Label 'Backend'
$FrontendPort = Resolve-Port -Requested $FrontendPort -Label 'Frontend'

# --- database --------------------------------------------------------------
# Docker Desktop is the documented path; fall back to a local Postgres, because
# plenty of machines have Postgres installed and no Docker.

function Find-PostgresBin {
    <#
      The Windows installer does not put psql on PATH by default, so "is
      Postgres installed" cannot be answered by Get-Command alone. Check PATH
      first, then PGBIN, then the standard install locations, newest first.
    #>
    if (Have 'pg_isready') { return (Split-Path (Get-Command pg_isready).Source) }
    if ($env:PGBIN -and (Test-Path (Join-Path $env:PGBIN 'pg_isready.exe'))) { return $env:PGBIN }

    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, 'C:\Program Files') |
             Where-Object { $_ } | Select-Object -Unique
    foreach ($root in $roots) {
        $base = Join-Path $root 'PostgreSQL'
        if (-not (Test-Path $base)) { continue }
        # Sort on the major version only. Stripping non-digits turns "9.6"
        # into 906, which would beat 18 and pick an ancient install.
        $versions = Get-ChildItem $base -Directory -ErrorAction SilentlyContinue |
                    Sort-Object { [int](($_.Name -split '[^0-9]')[0]) } -Descending
        foreach ($v in $versions) {
            $bin = Join-Path $v.FullName 'bin'
            if (Test-Path (Join-Path $bin 'pg_isready.exe')) { return $bin }
        }
    }
    return $null
}

function Test-PostgresPort {
    param([int]$Port)
    pg_isready -q -h localhost -p $Port *> $null
    return ($LASTEXITCODE -eq 0)
}

$DbUrl = $null

if ($DatabaseUrl) {
    Say 'Using the database URL you supplied'
    $DbUrl = $DatabaseUrl
}

$dockerUp = $false
if (-not $DbUrl -and (Have 'docker')) {
    docker info *> $null
    $dockerUp = ($LASTEXITCODE -eq 0)
}

if (-not $DbUrl -and $dockerUp) {
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

if (-not $DbUrl) {
    $pgBin = Find-PostgresBin
    if ($pgBin) {
        # Only for this process, so the machine's PATH is left alone.
        $env:Path = $pgBin + [IO.Path]::PathSeparator + $env:Path
        Say "Found Postgres at $pgBin"

        $port = @($PostgresPort, 5432, 5433) | Where-Object { $_ } |
                Select-Object -Unique | Where-Object { Test-PostgresPort $_ } |
                Select-Object -First 1

        if (-not $port) {
            Die @"
Postgres is installed at $pgBin but is not accepting connections.
Start the service and re-run:
  Get-Service postgresql*          # find the service name
  Start-Service <name>
"@
        }

        Warn "Docker is not running; using the Postgres on localhost:$port"

        # The installer creates a 'postgres' superuser with a password set
        # during setup. There is no way to discover it, so ask once and keep it
        # only for this process.
        if (-not $env:PGPASSWORD) {
            $secure = Read-Host "Password for the 'postgres' user (set when you installed Postgres)" -AsSecureString
            $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        }

        $psqlArgs = @('-h', 'localhost', '-p', "$port", '-U', $PostgresUser, '-d', 'postgres', '-q', '-t', '-A')

        & psql @psqlArgs -c 'SELECT 1' *> $null
        if ($LASTEXITCODE -ne 0) {
            Die @"
Could not sign in to Postgres on localhost:$port as '$PostgresUser'.
Check the password, or pass a different superuser:
  .\scripts\dev.ps1 -PostgresUser postgres
"@
        }

        $hasRole = (& psql @psqlArgs -c "SELECT 1 FROM pg_roles WHERE rolname='agentscrape'")
        if ("$hasRole".Trim() -ne '1') {
            Say 'Creating the agentscrape role'
            & psql @psqlArgs -c "CREATE ROLE agentscrape LOGIN PASSWORD 'agentscrape' SUPERUSER" *> $null
        }

        $hasDb = (& psql @psqlArgs -c "SELECT 1 FROM pg_database WHERE datname='agentscrape'")
        if ("$hasDb".Trim() -ne '1') {
            Say 'Creating the agentscrape database'
            & psql @psqlArgs -c 'CREATE DATABASE agentscrape OWNER agentscrape' *> $null
        }

        $DbUrl = "postgresql+asyncpg://agentscrape:agentscrape@localhost:$port/agentscrape"
    }
}

if (-not $DbUrl) {
    Die @'
No database found.

If Postgres IS installed, its tools are probably not on PATH. Point the script
at them directly:
  $env:PGBIN = "C:\Program Files\PostgreSQL\18\bin"
  .\scripts\dev.ps1

Otherwise either start Docker Desktop, or install Postgres:
  winget install PostgreSQL.PostgreSQL.16
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
        Die @"
The API did not start on port $BackendPort.
Run it directly to see the error:
  uv run uvicorn agentscrape.api.main:app --port $BackendPort
"@
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
