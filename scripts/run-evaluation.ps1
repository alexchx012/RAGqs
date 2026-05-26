[CmdletBinding()]
param(
    [string]$Dataset = "data\evaluation\golden.jsonl",
    [ValidateSet("fake", "service", "http")]
    [string]$Mode = "fake",
    [ValidateSet("none", "static", "model")]
    [string]$FaithfulnessJudge = "static",
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [double]$TimeoutSeconds = 30.0,
    [string]$ReportPath = "artifacts\evaluation-report.json",
    [int]$MinExamples = 2,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $repoRoot

try {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $python = "python"
    }

    $runnerArgs = @(
        "-m", "app.evaluation.runner",
        "--dataset", $Dataset,
        "--mode", $Mode,
        "--base-url", $BaseUrl,
        "--timeout-seconds", $TimeoutSeconds,
        "--faithfulness-judge", $FaithfulnessJudge,
        "--report-path", $ReportPath,
        "--min-examples", $MinExamples,
        "--output-json",
        "--min-retrieval-hit-rate", "1.0",
        "--min-answer-trait-coverage", "1.0",
        "--min-faithfulness", "1.0",
        "--min-citation-accuracy", "1.0",
        "--min-refusal-rate", "1.0"
    )
    if ($PreflightOnly) {
        $runnerArgs += "--preflight-only"
    }

    & $python @runnerArgs

    if ($LASTEXITCODE -ne 0) {
        throw "evaluation failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
