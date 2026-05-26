[CmdletBinding()]
param(
    [switch]$SkipPreflight
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $repoRoot

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host "[check] $Name" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

try {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $python = "python"
    }

    if (Test-Path -LiteralPath (Join-Path $repoRoot ".venv\Scripts\python.exe")) {
        Invoke-Step "ruff check app tests" {
            & $python -m ruff check app tests
        }

        Invoke-Step "pytest backend baseline/provider tests" {
            & $python -m pytest `
                tests\test_phase0_baseline.py `
                tests\test_api_models.py `
                tests\test_config_groups.py `
                tests\test_provider_contracts.py `
                tests\test_agent_provider_injection.py `
                tests\test_provider_factory.py `
                tests\test_knowledge_tool_provider.py `
                tests\test_background_indexing_worker.py `
                tests\test_ingestion_foundation.py `
                tests\test_postgres_indexing_job_store.py `
                tests\test_postgres_document_catalog.py `
                tests\test_postgres_checkpoint_provider.py `
                tests\test_vector_index_service_ingestion.py `
                tests\test_file_upload_ingestion.py `
                tests\test_upload_security.py `
                tests\test_knowledge_spaces_lifecycle.py `
                tests\test_evaluation_foundation.py `
                tests\test_retrieval_pipeline.py `
                tests\test_retrieval_enhancers.py `
                tests\test_retrieval_profiles.py `
                tests\test_chat_retrieval_trace.py `
                tests\test_rag_state_graph.py `
                tests\test_rag_agent_graph_runtime.py `
                tests\test_session_store_service.py `
                tests\test_sqlite_session_store.py `
                tests\test_postgres_session_store.py `
                tests\test_phase7_operations.py `
                tests\test_phase8_foundation_templates.py `
                -q
        }
    }
    else {
        Invoke-Step "python tests\test_phase0_baseline.py" {
            & $python tests\test_phase0_baseline.py
        }
    }

    Invoke-Step "node tests\chat-history.test.js" {
        node tests\chat-history.test.js
    }

    Invoke-Step "powershell tests\start-script.validation.ps1" {
        powershell -NoProfile -ExecutionPolicy Bypass -File tests\start-script.validation.ps1
    }

    Invoke-Step "powershell scripts\run-evaluation.ps1" {
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-evaluation.ps1
    }

    if (-not $SkipPreflight) {
        Invoke-Step "start.ps1 -PreflightOnly" {
            & .\start.ps1 -PreflightOnly
        }
    }
    else {
        Write-Host "[skip] start.ps1 -PreflightOnly" -ForegroundColor Yellow
    }

    Write-Host "[ ok ] baseline validation passed" -ForegroundColor Green
}
finally {
    Pop-Location
}
