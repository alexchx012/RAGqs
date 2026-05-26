[CmdletBinding()]
param(
    [double]$TimeoutSeconds = 5,
    [switch]$RequireConfigured,
    [switch]$ValidateWritePath,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        Write-Host "[fail] Python was not found. Create .venv or install Python first." -ForegroundColor Red
        exit 1
    }
    $python = $pythonCommand.Source
}

$exitCode = 0
Push-Location $repoRoot
try {
    Write-Host "[smoke] Checking configured Postgres stores. The default check does not create, delete, start, stop, or restart databases." -ForegroundColor Cyan
    $runnerArgs = @(
        "-m", "app.operations.postgres_smoke",
        "--timeout", $TimeoutSeconds
    )
    if ($RequireConfigured) {
        $runnerArgs += "--require-configured"
    }
    if ($ValidateWritePath) {
        Write-Host "[smoke] ValidateWritePath enabled; using temporary tables and rollback only." -ForegroundColor Cyan
        $runnerArgs += "--validate-write-path"
    }
    if ($Json) {
        $runnerArgs += "--json"
    }

    & $python @runnerArgs
    $exitCode = $LASTEXITCODE
}
catch {
    $exitCode = 1
    Write-Host "[fail] $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Pop-Location
}

exit $exitCode
