param(
    [switch]$NoBuild,
    [switch]$StopAfter
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$demoScript = Join-Path $PSScriptRoot "demo_interview.ps1"
$backendRoot = Join-Path $repoRoot "backend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$e2eBaseUrl = "http://127.0.0.1:13000"
$mcpVerification = Join-Path $repoRoot "scripts\verify_mcp_stdio.py"
$sseVerification = Join-Path $repoRoot "scripts\verify_sse_redis.py"

function Invoke-PytestStrict([string[]]$Arguments, [string]$Name) {
    $logPath = Join-Path ([System.IO.Path]::GetTempPath()) ("stockmind-{0}.log" -f ([guid]::NewGuid()))
    try {
        Push-Location $backendRoot
        try {
            & $python -m pytest @Arguments 2>&1 | Tee-Object -FilePath $logPath
            $exitCode = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }

        if ($exitCode -ne 0) {
            throw "$Name failed with exit code $exitCode."
        }

        $summary = Get-Content $logPath -Raw
        $skipMatches = [regex]::Matches($summary, "(?m)(\d+) skipped")
        $skipped = 0
        foreach ($match in $skipMatches) {
            $skipped += [int]$match.Groups[1].Value
        }
        if ($skipped -gt 0) {
            throw "$Name has $skipped skipped test(s). Critical interview checks must execute; reset the isolated stack and retry."
        }
    }
    finally {
        Remove-Item $logPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path $python)) {
    throw "Python venv not found at $python. Create backend/.venv before running the interview verification."
}

Write-Host "[1/6] Preparing a clean isolated demo stack..."
if ($NoBuild) {
    Write-Host "  -NoBuild is accepted for compatibility; the existing demo starter always verifies the current images."
}
& $demoScript -Isolated -Reset -SkipTests
if ($LASTEXITCODE -ne 0) {
    throw "Isolated demo preparation failed with exit code $LASTEXITCODE."
}

Write-Host "[2/4] Running critical backend regression tests (skip is an error)..."
$env:E2E_BASE_URL = $e2eBaseUrl
try {
    Invoke-PytestStrict @(
        "tests/unit/test_health.py",
        "tests/unit/test_permissions.py",
        "tests/integration/test_plan_flow.py",
        "tests/integration/test_conversation_ownership.py",
        "tests/integration/test_concurrency.py",
        "-q",
        "--disable-warnings"
    ) "Critical backend regression tests"
}
finally {
    Remove-Item Env:E2E_BASE_URL -ErrorAction SilentlyContinue
}

Write-Host "[3/6] Verifying MCP stdio protocol interoperability (skip is an error)..."
if (-not (Test-Path $mcpVerification)) {
    throw "MCP verification script not found at $mcpVerification."
}
$env:POSTGRES_DSN = "postgresql+psycopg://stockmind:stockmind@127.0.0.1:15432/stockmind"
$env:REDIS_URL = "redis://127.0.0.1:16379/0"
$env:CHECKPOINTER_BACKEND = "memory"
$env:EMBEDDING_ENABLED = "false"
try {
    & $python $mcpVerification --actor-id bob --python $python
    if ($LASTEXITCODE -ne 0) {
        throw "MCP stdio verification failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:POSTGRES_DSN,Env:REDIS_URL,Env:CHECKPOINTER_BACKEND,Env:EMBEDDING_ENABLED -ErrorAction SilentlyContinue
}

Write-Host "[4/6] Verifying Redis SSE recovery across processes (skip is an error)..."
$env:REDIS_URL = "redis://127.0.0.1:16379/0"
try {
    & $python $sseVerification
    if ($LASTEXITCODE -ne 0) {
        throw "Redis SSE verification failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:REDIS_URL -ErrorAction SilentlyContinue
}

Write-Host "[5/6] Running the complete isolated browser flow (skip is an error)..."
$env:E2E_BASE_URL = $e2eBaseUrl
try {
    Invoke-PytestStrict @("tests/", "-m", "e2e", "-q", "--disable-warnings") "Isolated browser E2E"
}
finally {
    Remove-Item Env:E2E_BASE_URL -ErrorAction SilentlyContinue
}

Write-Host "[6/6] Interview acceptance passed."
Write-Host "  Web:      $e2eBaseUrl"
Write-Host "  API docs: http://127.0.0.1:18000/docs"
Write-Host "  Supplier: http://127.0.0.1:18100/fault-modes"
Write-Host "  Runbook:  docs/12_interview/demo-runbook.md"

if ($StopAfter) {
    Push-Location $repoRoot
    try {
        docker compose -f docker-compose.iso.yml -p stockmind-interview down -v
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to stop the isolated demo stack with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Host "To stop the isolated stack: docker compose -f docker-compose.iso.yml -p stockmind-interview down -v"
}
