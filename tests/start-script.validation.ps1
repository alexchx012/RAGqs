Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$scriptPath = Join-Path $repoRoot "start.ps1"

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

Assert-True (Test-Path -LiteralPath $scriptPath) "start.ps1 should exist at the repository root."

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)

Assert-True ($parseErrors.Count -eq 0) "start.ps1 should parse without PowerShell syntax errors."

$text = Get-Content -Raw -Encoding UTF8 -LiteralPath $scriptPath

foreach ($required in @(
        "StopMilvusOnExit",
        "SkipDocker",
        "NoReload",
        "PreflightOnly",
        "DockerProfile",
        "MilvusTimeoutSeconds",
        "Get-ExistingMilvusContainers",
        "Start-ExistingMilvusContainers",
        "Assert-AppConfiguration",
        "app.operations.config_validation",
        "milvus-etcd",
        "milvus-minio",
        "milvus-standalone",
        "DASHSCOPE_API_KEY",
        "vector-database.yml",
        "uvicorn",
        "app.main:app"
    )) {
    Assert-True ($text.Contains($required)) "start.ps1 should contain '$required'."
}

Assert-True ($text -match 'docker\s+compose') "start.ps1 should start Docker Compose services."
Assert-True ($text -match '--profile') "start.ps1 should support Docker Compose profiles."
Assert-True ($text -match '@\("start"') "start.ps1 should be able to start existing named Milvus containers."
Assert-True ($text -match 'finally\s*\{') "start.ps1 should use a finally block for cleanup."
Assert-True ($text -match 'if\s*\(\s*\$StopMilvusOnExit\s*\)') "Milvus shutdown should be gated by -StopMilvusOnExit."
Assert-True ($ast.ParamBlock.Parameters.Count -gt 0) "start.ps1 should expose script parameters."

$psExe = (Get-Process -Id $PID).Path
if (-not $psExe) {
    $psExe = "powershell"
}

$stdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) "ragqs-start-script-smoke.out"
$stderrPath = Join-Path ([System.IO.Path]::GetTempPath()) "ragqs-start-script-smoke.err"
Remove-Item -LiteralPath $stdoutPath, $stderrPath -ErrorAction SilentlyContinue

$escapedScriptPath = $scriptPath.Replace("'", "''")
$runtimeCommand = "`$env:Path=''; `$env:DASHSCOPE_API_KEY='placeholder'; & '$escapedScriptPath' -SkipDocker -Port 65534"
$runtime = Start-Process `
    -FilePath $psExe `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $runtimeCommand) `
    -Wait `
    -PassThru `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath

$runtimeOutput = ""
if (Test-Path -LiteralPath $stdoutPath) {
    $runtimeOutput += Get-Content -Raw -Encoding UTF8 -LiteralPath $stdoutPath
}
if (Test-Path -LiteralPath $stderrPath) {
    $runtimeOutput += Get-Content -Raw -Encoding UTF8 -LiteralPath $stderrPath
}

Assert-True ($runtime.ExitCode -ne 0) "Safe failure smoke test should exit non-zero."
Assert-True ($runtimeOutput -notmatch "CancelKeyPress") "Safe failure smoke test should not fail on Console.CancelKeyPress handling."
Assert-True ($runtimeOutput -match "Python was not found|DASHSCOPE_API_KEY|Required command|Configuration validation failed") "Safe failure smoke test should fail with an actionable preflight message."

Write-Host "start.ps1 validation passed."
