[CmdletBinding()]
param(
    [string]$HostAddress = "0.0.0.0",
    [ValidateRange(1, 65535)]
    [int]$Port = 9900,
    [switch]$NoReload,
    [switch]$PreflightOnly,
    [switch]$SkipDocker,
    [switch]$StopMilvusOnExit,
    [ValidateSet("core", "ui")]
    [string]$DockerProfile = "core",
    [ValidateRange(10, 600)]
    [int]$MilvusTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$script:RepoRoot = $PSScriptRoot
$script:ComposeFile = Join-Path $script:RepoRoot "vector-database.yml"
$script:DockerPath = $null
$script:UsingExistingMilvusContainers = $false
$script:MilvusCoreContainerNames = @("milvus-etcd", "milvus-minio", "milvus-standalone")
$script:MilvusOptionalContainerNames = @("milvus-attu")
$script:MilvusContainerNames = if ($DockerProfile -eq "ui") {
    $script:MilvusCoreContainerNames + $script:MilvusOptionalContainerNames
}
else {
    $script:MilvusCoreContainerNames
}

function Write-Step {
    param([string]$Message)
    Write-Host "[start] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[ ok ] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Get-CommandPath {
    param([string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Required command '$Name' was not found in PATH."
    }

    return $command.Source
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    & $FilePath @Arguments
    $exitCode = $LASTEXITCODE
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')"
    }

    return $exitCode
}

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$PortNumber,
        [int]$TimeoutMs = 1000
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $PortNumber)
        if (-not $task.Wait($TimeoutMs)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-HttpHealth {
    param([int]$PortNumber)

    try {
        Invoke-WebRequest `
            -Uri "http://127.0.0.1:$($PortNumber)/health" `
            -UseBasicParsing `
            -TimeoutSec 2 `
            -ErrorAction Stop | Out-Null
        return $true
    }
    catch {
        if ($_.Exception.Response) {
            return $true
        }
        return $false
    }
}

function Get-DotEnvValue {
    param([string]$Key)

    $envFile = Join-Path $script:RepoRoot ".env"
    if (-not (Test-Path -LiteralPath $envFile)) {
        return ""
    }

    foreach ($line in Get-Content -Encoding UTF8 -LiteralPath $envFile) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }

    return ""
}

function Get-MilvusHostPort {
    $value = [Environment]::GetEnvironmentVariable("MILVUS_PORT")
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = Get-DotEnvValue -Key "MILVUS_PORT"
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        return 19530
    }

    $parsed = 0
    if (-not [int]::TryParse($value, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535) {
        throw "MILVUS_PORT must be an integer between 1 and 65535."
    }
    return $parsed
}

function Test-WindowsExcludedPort {
    param([int]$PortNumber)

    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        return $false
    }

    try {
        $ranges = netsh interface ipv4 show excludedportrange protocol=tcp 2>$null
    }
    catch {
        return $false
    }

    foreach ($line in $ranges) {
        if ($line -match "^\s*(\d+)\s+(\d+)") {
            $startPort = [int]$Matches[1]
            $endPort = [int]$Matches[2]
            if ($PortNumber -ge $startPort -and $PortNumber -le $endPort) {
                return $true
            }
        }
    }

    return $false
}

function Assert-MilvusHostPortAvailable {
    param([int]$PortNumber)

    if (Test-WindowsExcludedPort -PortNumber $PortNumber) {
        throw "Milvus host port $PortNumber is reserved by Windows excluded TCP port ranges. Set MILVUS_PORT to an available port in .env, for example 19630, then recreate the Milvus standalone container so Docker can apply the new port mapping."
    }
}

function Get-ContainerHostPort {
    param(
        [string]$ContainerName,
        [string]$ContainerPort
    )

    if (-not $script:DockerPath) {
        return ""
    }

    $template = "{{with (index .HostConfig.PortBindings `"$ContainerPort/tcp`")}}{{(index . 0).HostPort}}{{end}}"
    $value = & $script:DockerPath inspect --format $template $ContainerName 2>$null
    if ($LASTEXITCODE -ne 0) {
        return ""
    }

    return ([string]::Join("", $value)).Trim()
}

function Resolve-PythonRuntime {
    $venvPython = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python was not found. Install Python 3.11+ or create .venv first."
    }

    return $pythonCommand.Source
}

function Test-PythonDependencies {
    param([string]$PythonExe)

    $checkScript = "import importlib.util; modules=['fastapi','uvicorn','pymilvus','langchain_milvus','langgraph','langchain_qwq']; missing=[name for name in modules if importlib.util.find_spec(name) is None]; raise SystemExit(1 if missing else 0)"

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PythonExe -c $checkScript *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Ensure-PythonDependencies {
    $pythonExe = Resolve-PythonRuntime
    if (Test-PythonDependencies -PythonExe $pythonExe) {
        Write-Ok "Python dependencies are available"
        return $pythonExe
    }

    Write-Warn "Python dependencies are missing; attempting local install."
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    $venvPython = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"

    if ($uvCommand) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            Write-Step "Creating .venv with uv"
            Invoke-Native -FilePath $uvCommand.Source -Arguments @("venv") | Out-Null
        }

        Write-Step "Installing project dependencies with uv"
        Invoke-Native -FilePath $uvCommand.Source -Arguments @("pip", "install", "-e", ".", "--python", $venvPython) | Out-Null
        $pythonExe = $venvPython
    }
    elseif ($pythonExe -like "$($script:RepoRoot)*\.venv\Scripts\python.exe") {
        Write-Step "Installing project dependencies with pip"
        Invoke-Native -FilePath $pythonExe -Arguments @("-m", "pip", "install", "-e", ".") | Out-Null
    }
    else {
        throw "Dependencies are missing and uv is not installed. Install uv, then rerun this script."
    }

    if (-not (Test-PythonDependencies -PythonExe $pythonExe)) {
        throw "Dependency installation finished, but required Python modules are still missing."
    }

    Write-Ok "Python dependencies installed"
    return $pythonExe
}

function Assert-AppConfiguration {
    param([string]$PythonExe)

    $envFile = Join-Path $script:RepoRoot ".env"
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw ".env not found. Create it from .env.example and set CHAT_MODEL, DEEPSEEK_API_KEY, and DASHSCOPE_EMBEDDING_MODEL (or the selected provider keys) before starting the app."
    }

    # Preflight only calls configuration validation; do not print API keys.
    # Chat credentials (CHAT_MODEL / DEEPSEEK_API_KEY) are diagnosed separately
    # from embedding credentials (DASHSCOPE_API_KEY / DASHSCOPE_EMBEDDING_MODEL).
    Write-Step "Running configuration preflight (config validation only; no upstream smoke calls)"
    $stdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) "ragqs-config-validation.out"
    $stderrPath = Join-Path ([System.IO.Path]::GetTempPath()) "ragqs-config-validation.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList @("-m", "app.operations.config_validation") `
        -Wait `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath

    $validationOutput = ""
    if (Test-Path -LiteralPath $stdoutPath) {
        $validationOutput += Get-Content -Raw -Encoding UTF8 -LiteralPath $stdoutPath
    }
    if (Test-Path -LiteralPath $stderrPath) {
        $validationOutput += Get-Content -Raw -Encoding UTF8 -LiteralPath $stderrPath
    }

    if ($process.ExitCode -ne 0) {
        $message = $validationOutput.Trim()
        if ($message -match "^Traceback") {
            $message = "Python dependencies could not be loaded while validating app.operations.config_validation. Run dependency installation, then rerun start.ps1."
        }
        elseif ([string]::IsNullOrWhiteSpace($message)) {
            $message = "Fix CHAT_MODEL / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / DASHSCOPE_EMBEDDING_MODEL (as required by selected providers) and rerun start.ps1."
        }
        throw "Configuration preflight failed. $message"
    }

    Write-Ok "Application configuration is valid (configured; not smoke-tested)"
}

function Get-ExistingMilvusContainers {
    $containers = @()

    foreach ($containerName in $script:MilvusContainerNames) {
        $status = & $script:DockerPath inspect `
            --format "{{.State.Status}}" `
            $containerName `
            2>$null

        if ($LASTEXITCODE -eq 0) {
            $containers += [pscustomobject]@{
                Name = $containerName
                Status = ([string]::Join("", $status)).Trim()
            }
        }
    }

    return $containers
}

function Start-ExistingMilvusContainers {
    param([object[]]$ExistingContainers)

    $existingNames = @($ExistingContainers | ForEach-Object { $_.Name })
    $missingCore = @($script:MilvusCoreContainerNames | Where-Object { $_ -notin $existingNames })

    if ($missingCore.Count -gt 0) {
        $found = [string]::Join(", ", $existingNames)
        $missing = [string]::Join(", ", $missingCore)
        throw "Found existing Milvus containers ($found), but missing required containers: $missing. Remove stale Milvus containers or restore the full stack before rerunning."
    }

    Write-Warn "Existing Milvus containers detected; reusing them instead of running docker compose up."

    $configuredMilvusPort = Get-MilvusHostPort
    $existingMilvusPort = Get-ContainerHostPort -ContainerName "milvus-standalone" -ContainerPort "19530"
    if (-not [string]::IsNullOrWhiteSpace($existingMilvusPort)) {
        $existingMilvusPortNumber = 0
        if ([int]::TryParse($existingMilvusPort, [ref]$existingMilvusPortNumber)) {
            Assert-MilvusHostPortAvailable -PortNumber $existingMilvusPortNumber
            if ($existingMilvusPortNumber -ne $configuredMilvusPort) {
                throw "Existing milvus-standalone maps host port $existingMilvusPortNumber, but MILVUS_PORT is $configuredMilvusPort. Recreate the Milvus standalone container so Docker can apply the configured port."
            }
        }
    }

    foreach ($containerName in $script:MilvusCoreContainerNames) {
        $container = $ExistingContainers | Where-Object { $_.Name -eq $containerName } | Select-Object -First 1
        if ($container.Status -ne "running") {
            Write-Step "Starting existing container $containerName"
            Invoke-Native -FilePath $script:DockerPath -Arguments @("start", $containerName) | Out-Null
        }
    }

    foreach ($containerName in $script:MilvusOptionalContainerNames) {
        if ($DockerProfile -ne "ui") {
            break
        }
        $container = $ExistingContainers | Where-Object { $_.Name -eq $containerName } | Select-Object -First 1
        if ($container -and $container.Status -ne "running") {
            Write-Step "Starting existing container $containerName"
            Invoke-Native -FilePath $script:DockerPath -Arguments @("start", $containerName) -AllowFailure | Out-Null
        }
    }

    $script:UsingExistingMilvusContainers = $true
    Wait-MilvusHealthy
}

function Start-MilvusStack {
    $milvusHostPort = Get-MilvusHostPort
    if ($SkipDocker) {
        Write-Warn "Skipping Docker Compose startup by request."
        if (-not (Test-TcpPort -HostName "127.0.0.1" -PortNumber $milvusHostPort -TimeoutMs 1000)) {
            throw "Milvus port $milvusHostPort is not reachable. Start Milvus or rerun without -SkipDocker."
        }
        Write-Ok "Milvus port $milvusHostPort is reachable"
        return
    }

    if (-not (Test-Path -LiteralPath $script:ComposeFile)) {
        throw "Docker Compose file not found: $($script:ComposeFile)"
    }

    $script:DockerPath = Get-CommandPath "docker"
    Assert-MilvusHostPortAvailable -PortNumber $milvusHostPort
    Write-Step "Checking Docker daemon"
    Invoke-Native -FilePath $script:DockerPath -Arguments @("info") | Out-Null

    $existingContainers = @(Get-ExistingMilvusContainers)
    if ($existingContainers.Count -gt 0) {
        Start-ExistingMilvusContainers -ExistingContainers $existingContainers
        return
    }

    Write-Step "Starting Milvus stack with Docker profile '$DockerProfile'"
    $composeArgs = @("compose", "-f", $script:ComposeFile)
    if ($DockerProfile -eq "ui") {
        $composeArgs += @("--profile", "ui")
    }
    Invoke-Native -FilePath $script:DockerPath -Arguments ($composeArgs + @("up", "-d")) | Out-Null

    Wait-MilvusHealthy
}

function Wait-MilvusHealthy {
    $deadline = (Get-Date).AddSeconds($MilvusTimeoutSeconds)
    $lastStatus = "unknown"

    while ((Get-Date) -lt $deadline) {
        $inspectOutput = & $script:DockerPath inspect -f "{{.State.Health.Status}}" "milvus-standalone" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $lastStatus = ([string]::Join("", $inspectOutput)).Trim()
            if ($lastStatus -eq "healthy") {
                Write-Ok "Milvus is healthy"
                return
            }
        }

        Write-Host "." -NoNewline
        Start-Sleep -Seconds 3
    }

    Write-Host ""
    Write-Warn "Milvus did not become healthy within $MilvusTimeoutSeconds seconds. Last status: $lastStatus"
    Write-Warn "Recent Milvus logs:"
    Invoke-Native -FilePath $script:DockerPath -Arguments @("compose", "-f", $script:ComposeFile, "logs", "--tail", "80", "standalone") -AllowFailure | Out-Null
    throw "Milvus health check timed out."
}

function Assert-RunningApiHealth {
    param(
        [string]$PythonExe,
        [int]$PortNumber
    )

    $healthUrl = "http://127.0.0.1:$($PortNumber)/health"
    Invoke-Native `
        -FilePath $PythonExe `
        -Arguments @("-m", "app.operations.health_preflight", "--url", $healthUrl, "--timeout", "5")
}

function Assert-ApiPortAvailable {
    param([string]$PythonExe)

    if (-not (Test-TcpPort -HostName "127.0.0.1" -PortNumber $Port -TimeoutMs 500)) {
        return $true
    }

    if (Test-HttpHealth -PortNumber $Port) {
        Assert-RunningApiHealth -PythonExe $PythonExe -PortNumber $Port
        Write-Warn "A service on port $Port already responds to /health."
        Write-Host "Web UI:   http://127.0.0.1:$Port"
        Write-Host "API docs: http://127.0.0.1:$Port/docs"
        return $false
    }

    throw "Port $Port is already in use by another process."
}

function Start-FastApiForeground {
    param([string]$PythonExe)

    $uvicornArgs = @(
        "-m", "uvicorn",
        "app.main:app",
        "--host", $HostAddress,
        "--port", [string]$Port
    )

    if (-not $NoReload) {
        $uvicornArgs += "--reload"
    }

    Write-Step "Starting FastAPI"
    Write-Host "Web UI:   http://127.0.0.1:$Port"
    Write-Host "API docs: http://127.0.0.1:$Port/docs"
    Write-Host "Press Ctrl+C to stop FastAPI. Milvus will keep running unless -StopMilvusOnExit is set."

    & $PythonExe @uvicornArgs
    $exitCode = $LASTEXITCODE

    $exitCodeText = [string]$exitCode
    if ($exitCodeText -in @("", "0", "130", "-1073741510", "3221225786")) {
        return
    }

    if ($exitCode -ne 0) {
        throw "FastAPI exited with code $exitCode."
    }
}

$exitCode = 0
Push-Location $script:RepoRoot

try {
    $pythonExe = Ensure-PythonDependencies
    Assert-AppConfiguration -PythonExe $pythonExe
    Start-MilvusStack

    if ($PreflightOnly) {
        Write-Ok "Preflight checks completed"
        return
    }

    $shouldStartApi = Assert-ApiPortAvailable -PythonExe $pythonExe
    if ($shouldStartApi) {
        Start-FastApiForeground -PythonExe $pythonExe
    }
}
catch [System.Management.Automation.PipelineStoppedException] {
    $exitCode = 0
    Write-Host ""
    Write-Host "[info] Interrupted by user." -ForegroundColor DarkGray
}
catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "[fail] $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    if ($StopMilvusOnExit) {
        if (-not $SkipDocker) {
            try {
                if (-not $script:DockerPath) {
                    $script:DockerPath = Get-CommandPath "docker"
                }
                if ($script:UsingExistingMilvusContainers) {
                    Write-Step "Stopping existing Milvus containers because -StopMilvusOnExit was set"
                    Invoke-Native -FilePath $script:DockerPath -Arguments (@("stop") + $script:MilvusContainerNames) -AllowFailure | Out-Null
                }
                else {
                    Write-Step "Stopping Milvus stack because -StopMilvusOnExit was set"
                    Invoke-Native -FilePath $script:DockerPath -Arguments @("compose", "-f", $script:ComposeFile, "stop") | Out-Null
                }
            }
            catch {
                $exitCode = 1
                Write-Warn "Failed to stop Milvus stack: $($_.Exception.Message)"
            }
        }
    }
    else {
        Write-Host "[info] Milvus stack kept running. Stop it later with:" -ForegroundColor DarkGray
    Write-Host "       docker stop $([string]::Join(' ', $script:MilvusContainerNames))" -ForegroundColor DarkGray
    }

    Pop-Location
}

exit $exitCode
