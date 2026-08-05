param(
    [switch]$SkipIntegration,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

function Invoke-VerificationCommand {
    param([string[]]$Arguments)

    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Invoke-VerificationCommand @("-m", "ruff", "check", "app", "alembic", "tests")
Invoke-VerificationCommand @("-m", "black", "--check", "app", "alembic", "tests")
Invoke-VerificationCommand @("-m", "isort", "--check-only", "app", "alembic", "tests")
Invoke-VerificationCommand @("-m", "mypy", "app")

if ($SkipIntegration) {
    Invoke-VerificationCommand @("-m", "pytest", "tests/platform", "-q", "-m", "not integration")
} else {
    Invoke-VerificationCommand @("-m", "pytest", "tests/platform", "-q")
}
