param(
    [string]$ApiUrl = "http://127.0.0.1:9900",
    [int]$Concurrency = 20,
    [int]$Requests = 40,
    [int]$TimeoutSeconds = 30,
    [string]$AuthUser = "local-admin",
    [string]$SpaceId = "default",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$scopeMessage = "Expected API configuration: CHAT_PROVIDER=fake, EMBEDDING_PROVIDER=fake, VECTOR_STORE_PROVIDER=fake. This verifies API concurrency, timeout, and error response paths only; it does not prove real 80-user capacity or answer quality."

function New-Report {
    param(
        [string]$Status,
        [string]$Message,
        [object[]]$Results = @()
    )

    return [ordered]@{
        status = $Status
        message = $Message
        scope = $scopeMessage
        apiUrl = $ApiUrl
        concurrency = $Concurrency
        requests = $Requests
        statuses = [ordered]@{
            verified = @($Results | Where-Object { $_.status -eq "verified" }).Count
            skipped = @($Results | Where-Object { $_.status -eq "skipped" }).Count
            failed = @($Results | Where-Object { $_.status -eq "failed" }).Count
        }
        results = $Results
    }
}

function Write-Report {
    param([hashtable]$Report)

    if ($Json) {
        $Report | ConvertTo-Json -Depth 8
        return
    }

    Write-Host ("status={0} {1}" -f $Report.status, $Report.message)
    Write-Host $Report.scope
    Write-Host ("verified={0} skipped={1} failed={2}" -f $Report.statuses.verified, $Report.statuses.skipped, $Report.statuses.failed)
}

try {
    Invoke-RestMethod -Method Get -Uri "$ApiUrl/health" -TimeoutSec $TimeoutSeconds | Out-Null
}
catch {
    $report = New-Report -Status "skipped" -Message "API health endpoint is not reachable; start RAGqs with fake providers before running this command."
    Write-Report $report
    exit 0
}

$safeConcurrency = [Math]::Max(1, $Concurrency)
$safeRequests = [Math]::Max(1, $Requests)
$jobs = New-Object System.Collections.Generic.List[object]
$results = New-Object System.Collections.Generic.List[object]

for ($i = 0; $i -lt $safeRequests; $i++) {
    while (@($jobs | Where-Object { $_.State -eq "Running" }).Count -ge $safeConcurrency) {
        $finished = Wait-Job -Job $jobs -Any -Timeout 1
        if ($finished) {
            foreach ($job in @($finished)) {
                $results.Add((Receive-Job -Job $job))
                Remove-Job -Job $job
                [void]$jobs.Remove($job)
            }
        }
    }

    $job = Start-Job -ScriptBlock {
        param($BaseUrl, $Index, $Timeout, $User, $TargetSpace)

        $body = @{
            Id = "fake-load-$Index"
            Question = "fake provider load probe $Index"
            spaceId = $TargetSpace
        } | ConvertTo-Json -Compress
        $headers = @{ "X-RAG-User" = $User }

        try {
            $response = Invoke-WebRequest -Method Post -Uri "$BaseUrl/api/chat" -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec $Timeout
            return [ordered]@{
                status = "verified"
                request = $Index
                statusCode = [int]$response.StatusCode
            }
        }
        catch {
            $statusCode = 0
            if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                $statusCode = [int]$_.Exception.Response.StatusCode
            }
            if ($statusCode -eq 429 -or $statusCode -eq 504) {
                return [ordered]@{
                    status = "verified"
                    request = $Index
                    statusCode = $statusCode
                    errorPath = $true
                }
            }
            return [ordered]@{
                status = "failed"
                request = $Index
                statusCode = $statusCode
                error = $_.Exception.Message
            }
        }
    } -ArgumentList $ApiUrl, $i, $TimeoutSeconds, $AuthUser, $SpaceId
    $jobs.Add($job)
}

while ($jobs.Count -gt 0) {
    $finished = Wait-Job -Job $jobs -Any -Timeout $TimeoutSeconds
    if (-not $finished) {
        foreach ($job in @($jobs)) {
            Stop-Job -Job $job
            $results.Add([ordered]@{
                status = "failed"
                request = "unknown"
                statusCode = 0
                error = "job timed out"
            })
            Remove-Job -Job $job
        }
        $jobs.Clear()
        break
    }

    foreach ($job in @($finished)) {
        $results.Add((Receive-Job -Job $job))
        Remove-Job -Job $job
        [void]$jobs.Remove($job)
    }
}

$failedCount = @($results | Where-Object { $_.status -eq "failed" }).Count
$status = if ($failedCount -eq 0) { "verified" } else { "failed" }
$message = if ($failedCount -eq 0) {
    "fake-provider API concurrency path completed"
} else {
    "fake-provider API concurrency path had failed requests"
}

$finalReport = New-Report -Status $status -Message $message -Results @($results)
Write-Report $finalReport
exit $(if ($failedCount -eq 0) { 0 } else { 1 })
