[CmdletBinding()]
param(
    [string]$Url = "http://127.0.0.1:9900/health",
    [double]$TimeoutSeconds = 5
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
    & $python -m app.operations.health_preflight --url $Url --timeout $TimeoutSeconds
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
