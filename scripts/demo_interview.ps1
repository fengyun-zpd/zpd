param(
    [switch]$SkipTests,
    [switch]$Isolated,
    [switch]$Reset
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$apiBase = "http://127.0.0.1:8000"
$webBase = "http://127.0.0.1:3000"

if ($Reset -and -not $Isolated) {
    throw "-Reset requires -Isolated so the main demo database is never removed."
}

if ($Isolated) {
    Write-Host "Starting isolated interview stack..."
    Push-Location $repoRoot
    try {
        if ($Reset) {
            Write-Host "Resetting isolated database, supplier state, and model cache..."
            & docker compose -f docker-compose.iso.yml -p stockmind-interview down -v
            if ($LASTEXITCODE -ne 0) {
                throw "Isolated Compose reset failed with exit code $LASTEXITCODE."
            }
        }
        & docker compose -f docker-compose.iso.yml -p stockmind-interview up -d --build
        if ($LASTEXITCODE -ne 0) {
            throw "Isolated Compose failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
    $apiBase = "http://127.0.0.1:18000"
    $webBase = "http://127.0.0.1:13000"
}

function Assert-Equal([string]$Name, $Actual, $Expected) {
    if ($Actual -ne $Expected) {
        throw "$Name check failed: actual='$Actual', expected='$Expected'."
    }
}

function Wait-Rest([string]$Name, [string]$Url, [int]$Attempts = 60) {
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            return Invoke-RestMethod $Url -ErrorAction Stop
        }
        catch {
            if ($attempt -eq $Attempts) {
                throw "$Name did not become reachable after $Attempts attempts: $Url"
            }
            Start-Sleep -Seconds 2
        }
    }
}

Write-Host "[1/3] Checking API health and readiness..."
$health = Wait-Rest "API health" "$apiBase/health"
$ready = Wait-Rest "API readiness" "$apiBase/ready"
Assert-Equal "health.status" $health.status "ok"
Assert-Equal "health.capabilities.agent" $health.capabilities.agent $true
Assert-Equal "ready.status" $ready.status "ready"
Write-Host "  health=$($health.status), ready=$($ready.status), agent=$($health.capabilities.agent)"

Write-Host "[2/3] Checking Web and API demo entrypoints..."
$webResponse = Invoke-WebRequest $webBase -UseBasicParsing -ErrorAction Stop
$docsResponse = Invoke-WebRequest "$apiBase/docs" -UseBasicParsing -ErrorAction Stop
Assert-Equal "web.status" $webResponse.StatusCode 200
Assert-Equal "api-docs.status" $docsResponse.StatusCode 200
Write-Host "  Web entrypoint and API docs are reachable."
if ($Isolated) {
    Write-Host "  Warming the optional retrieval model before browser checks..."
    $null = Invoke-RestMethod "$apiBase/api/v1/knowledge/search?q=stock&top_k=1" -Headers @{"X-Actor-Id" = "alice"} -ErrorAction Stop
}

if (-not $SkipTests) {
    Write-Host "[3/3] Running interview demo regression tests..."
    if (-not (Test-Path $python)) {
        throw "Python venv not found at $python. Create backend/.venv or use -SkipTests."
    }
    Push-Location $backendRoot
    try {
        $env:E2E_BASE_URL = $webBase
        & $python -m pytest tests/unit/test_health.py tests/unit/test_permissions.py tests/integration/test_plan_flow.py -q
        if ($LASTEXITCODE -ne 0) {
            throw "Interview demo regression tests failed with exit code $LASTEXITCODE."
        }
        if ($Isolated) {
            Write-Host "  Running isolated browser and procurement-closure E2E..."
            & $python -m pytest tests/ -m e2e -q
            if ($LASTEXITCODE -ne 0) {
                throw "Isolated E2E tests failed with exit code $LASTEXITCODE."
            }
        }
    }
    finally {
        Remove-Item Env:E2E_BASE_URL -ErrorAction SilentlyContinue
        Pop-Location
    }
}
else {
    Write-Host "[3/3] Local tests skipped (-SkipTests)."
}

Write-Host ""
Write-Host "Demo entrypoints:"
Write-Host "  Web:      $webBase"
Write-Host "  API docs: $apiBase/docs"
if ($Isolated) {
    Write-Host "  Supplier: http://127.0.0.1:18100/fault-modes"
} else {
    Write-Host "  Supplier: http://127.0.0.1:8100/fault-modes"
}
Write-Host "Suggested actors: alice (full demo), bob (operator), dave (buyer), eve (admin governance)."
if ($Isolated) {
    Write-Host "To stop the isolated demo stack: docker compose -f docker-compose.iso.yml -p stockmind-interview down -v"
}
