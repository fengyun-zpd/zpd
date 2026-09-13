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

Write-Host "[1/4] Preparing a clean isolated demo stack..."
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

Write-Host "[3/4] Running the complete isolated browser flow (skip is an error)..."
$env:E2E_BASE_URL = $e2eBaseUrl
try {
    Invoke-PytestStrict @("tests/", "-m", "e2e", "-q", "--disable-warnings") "Isolated browser E2E"
}
finally {
    Remove-Item Env:E2E_BASE_URL -ErrorAction SilentlyContinue
}

Write-Host "[4/4] Interview acceptance passed."
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
