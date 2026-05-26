[CmdletBinding()]
param(
    [string]$ApiUrl = "",
    [double]$TimeoutSeconds = 5,
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
    Write-Host "[smoke] Checking configuration and Milvus. This script does not stop Milvus." -ForegroundColor Cyan
    $runnerArgs = @(
        "-m", "app.operations.integration_smoke",
        "--timeout", $TimeoutSeconds
    )
    if (-not [string]::IsNullOrWhiteSpace($ApiUrl)) {
        $runnerArgs += @("--api-url", $ApiUrl)
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
