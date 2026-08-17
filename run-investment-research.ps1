[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$PlannerSmoke,
    [switch]$PlannerWeb,
    [switch]$ResearchWeb,
    [switch]$Workbench,
    [ValidateSet("planner", "fundamental", "industry_competition", "market_catalyst", "risk", "reviewer_arbiter", "report_writer")]
    [string]$AgentSmoke,
    [switch]$ValidateAllAgents,
    [switch]$FullChainValidation,
    [switch]$CrewAIFlowSmoke,
    [switch]$CrewAIFullChainValidation,
    [switch]$ReportWriterSmoke,
    [switch]$ReportSectionsSmoke,
    [switch]$ReviewerAuditSmoke,
    [string]$RunId,
    [string]$Company = "CATL",
    [string]$AsOfDate = (Get-Date -Format "yyyy-MM-dd"),
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$OpenHarnessArgs
)

$ErrorActionPreference = "Stop"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$projectRoot = Split-Path -Parent $PSCommandPath
$secretPath = Join-Path $projectRoot ".openharness\secrets\tavily-key.dpapi"
$arkSecretPath = Join-Path $projectRoot ".openharness\secrets\ark-key.dpapi"
# Optional pool of Ark keys. A group discussion runs several Agents at once, and
# concurrent calls on one account hit that account's limits, so each speaker
# leases its own credential and the number of keys is the concurrency cap.
$arkPoolSecretPath = Join-Path $projectRoot ".openharness\secrets\ark-keys.dpapi"
$openHarnessLauncher = Join-Path $projectRoot "run-openh.ps1"
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"

$needsTavily = $PlannerSmoke -or $PlannerWeb -or $ResearchWeb -or $Workbench -or $ValidateAllAgents -or $FullChainValidation -or $CrewAIFlowSmoke -or $CrewAIFullChainValidation
if ($AgentSmoke -and $AgentSmoke -notin @("reviewer_arbiter", "report_writer")) {
    $needsTavily = $true
}
$needsArk = $PlannerSmoke -or $PlannerWeb -or $ResearchWeb -or $Workbench -or $AgentSmoke -or $ValidateAllAgents -or $FullChainValidation -or $CrewAIFlowSmoke -or $CrewAIFullChainValidation -or $ReportWriterSmoke -or $ReportSectionsSmoke -or $ReviewerAuditSmoke
if ($needsTavily -and -not (Test-Path -LiteralPath $secretPath)) {
    throw "Tavily is not configured. Run .\setup-tavily-key.ps1 first."
}
if ($needsArk -and -not (Test-Path -LiteralPath $arkSecretPath)) {
    throw "Volcengine Ark is not configured. Run .\setup-ark-key.ps1 first."
}

if ($PlannerSmoke -or $PlannerWeb -or $ResearchWeb -or $Workbench -or $AgentSmoke -or $ValidateAllAgents -or $FullChainValidation -or $CrewAIFlowSmoke -or $CrewAIFullChainValidation -or $ReportWriterSmoke -or $ReportSectionsSmoke -or $ReviewerAuditSmoke) {
    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        throw "Existing Python environment was not found: .venv\Scripts\python.exe"
    }
}
elseif (-not (Test-Path -LiteralPath $openHarnessLauncher)) {
    throw "Existing launcher was not found: run-openh.ps1"
}

$encryptedKey = $null
$secureKey = $null
$keyPointer = [IntPtr]::Zero
$encryptedArkKey = $null
$secureArkKey = $null
$arkKeyPointer = [IntPtr]::Zero
if ($needsTavily) {
    $encryptedKey = [System.IO.File]::ReadAllText($secretPath).Trim()
    if (-not $encryptedKey) {
        throw "The encrypted Tavily credential is empty. Run setup again."
    }
    $secureKey = ConvertTo-SecureString -String $encryptedKey
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
}
if ($needsArk) {
    $encryptedArkKey = [System.IO.File]::ReadAllText($arkSecretPath).Trim()
    if (-not $encryptedArkKey) {
        throw "The encrypted Ark credential is empty. Run setup again."
    }
    $secureArkKey = ConvertTo-SecureString -String $encryptedArkKey
    $arkKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureArkKey)
}
$previousTavilyKey = [Environment]::GetEnvironmentVariable("TAVILY_API_KEY", "Process")
$previousArkKey = [Environment]::GetEnvironmentVariable("ARK_API_KEY", "Process")
$previousArkKeys = [Environment]::GetEnvironmentVariable("ARK_API_KEYS", "Process")
$previousOpenAiKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "Process")
$previousOpenHarnessDataDir = [Environment]::GetEnvironmentVariable("OPENHARNESS_DATA_DIR", "Process")
$exitCode = 0

Push-Location $projectRoot
try {
    # Keep transient tool artifacts and CrewAI/OpenHarness runtime data inside
    # this project.  The default user-home data directory can be locked by
    # enterprise policy and would otherwise break a successful search result
    # while it is being cached.
    [Environment]::SetEnvironmentVariable(
        "OPENHARNESS_DATA_DIR",
        (Join-Path $projectRoot ".openharness\data"),
        "Process"
    )
    if ($needsTavily) {
        $plainTextKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
        [Environment]::SetEnvironmentVariable("TAVILY_API_KEY", $plainTextKey, "Process")
        $plainTextKey = $null
    }
    if ($needsArk) {
        $plainTextArkKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($arkKeyPointer)
        # ARK_API_KEY is the project-specific variable. OPENAI_API_KEY is set
        # only for this child process because Ark exposes an OpenAI-compatible API.
        [Environment]::SetEnvironmentVariable("ARK_API_KEY", $plainTextArkKey, "Process")
        [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $plainTextArkKey, "Process")
        $plainTextArkKey = $null
    }
    if ($needsArk -and (Test-Path -LiteralPath $arkPoolSecretPath)) {
        $encryptedArkPool = [System.IO.File]::ReadAllText($arkPoolSecretPath).Trim()
        if ($encryptedArkPool) {
            $secureArkPool = ConvertTo-SecureString -String $encryptedArkPool
            $arkPoolPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureArkPool)
            try {
                $plainTextArkPool = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($arkPoolPointer)
                [Environment]::SetEnvironmentVariable("ARK_API_KEYS", $plainTextArkPool, "Process")
                $plainTextArkPool = $null
            }
            finally {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($arkPoolPointer)
                $secureArkPool.Dispose()
            }
        }
    }

    if ($Workbench) {
        $previousCrewAIStorage = [Environment]::GetEnvironmentVariable("CREWAI_STORAGE_DIR", "Process")
        $previousOtelDisabled = [Environment]::GetEnvironmentVariable("OTEL_SDK_DISABLED", "Process")
        $previousPythonUtf8 = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
        $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
        try {
            [Environment]::SetEnvironmentVariable("CREWAI_STORAGE_DIR", (Join-Path $projectRoot ".openharness\data\crewai"), "Process")
            [Environment]::SetEnvironmentVariable("OTEL_SDK_DISABLED", "true", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
            & $pythonExecutable -m openharness.invest_research.workbench_server --port $Port --open-browser
        }
        finally {
            [Environment]::SetEnvironmentVariable("CREWAI_STORAGE_DIR", $previousCrewAIStorage, "Process")
            [Environment]::SetEnvironmentVariable("OTEL_SDK_DISABLED", $previousOtelDisabled, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", $previousPythonUtf8, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $previousPythonIoEncoding, "Process")
        }
    }
    elseif ($PlannerWeb -or $ResearchWeb) {
        & $pythonExecutable -m openharness.invest_research.demo_web `
            --port $Port `
            --open-browser
    }
    elseif ($PlannerSmoke) {
        & $pythonExecutable -m openharness.invest_research.run_planner_smoke `
            --company $Company `
            --as-of-date $AsOfDate
    }
    elseif ($AgentSmoke) {
        & $pythonExecutable -m openharness.invest_research.validation_harness `
            --mode agent `
            --agent $AgentSmoke
    }
    elseif ($ValidateAllAgents) {
        & $pythonExecutable -m openharness.invest_research.validation_harness `
            --mode all
    }
    elseif ($FullChainValidation) {
        & $pythonExecutable -m openharness.invest_research.validation_harness `
            --mode chain
    }
    elseif ($CrewAIFlowSmoke) {
        $previousCrewAIStorage = [Environment]::GetEnvironmentVariable(
            "CREWAI_STORAGE_DIR",
            "Process"
        )
        $previousOtelDisabled = [Environment]::GetEnvironmentVariable(
            "OTEL_SDK_DISABLED",
            "Process"
        )
        $previousPythonUtf8 = [Environment]::GetEnvironmentVariable(
            "PYTHONUTF8",
            "Process"
        )
        $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable(
            "PYTHONIOENCODING",
            "Process"
        )
        try {
            [Environment]::SetEnvironmentVariable(
                "CREWAI_STORAGE_DIR",
                (Join-Path $projectRoot ".openharness\data\crewai"),
                "Process"
            )
            [Environment]::SetEnvironmentVariable("OTEL_SDK_DISABLED", "true", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
            & $pythonExecutable -m openharness.invest_research.orchestration.flow_runner `
                --company $Company `
                --as-of-date $AsOfDate
        }
        finally {
            [Environment]::SetEnvironmentVariable(
                "CREWAI_STORAGE_DIR",
                $previousCrewAIStorage,
                "Process"
            )
            [Environment]::SetEnvironmentVariable(
                "OTEL_SDK_DISABLED",
                $previousOtelDisabled,
                "Process"
            )
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", $previousPythonUtf8, "Process")
            [Environment]::SetEnvironmentVariable(
                "PYTHONIOENCODING",
                $previousPythonIoEncoding,
                "Process"
            )
        }
    }
    elseif ($CrewAIFullChainValidation) {
        $previousCrewAIStorage = [Environment]::GetEnvironmentVariable(
            "CREWAI_STORAGE_DIR", "Process"
        )
        $previousOtelDisabled = [Environment]::GetEnvironmentVariable(
            "OTEL_SDK_DISABLED", "Process"
        )
        $previousPythonUtf8 = [Environment]::GetEnvironmentVariable(
            "PYTHONUTF8", "Process"
        )
        $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable(
            "PYTHONIOENCODING", "Process"
        )
        try {
            [Environment]::SetEnvironmentVariable(
                "CREWAI_STORAGE_DIR", (Join-Path $projectRoot ".openharness\data\crewai"), "Process"
            )
            [Environment]::SetEnvironmentVariable("OTEL_SDK_DISABLED", "true", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
            & $pythonExecutable -m openharness.invest_research.orchestration.flow_runner `
                --company $Company `
                --as-of-date $AsOfDate `
                --complete-report
        }
        finally {
            [Environment]::SetEnvironmentVariable("CREWAI_STORAGE_DIR", $previousCrewAIStorage, "Process")
            [Environment]::SetEnvironmentVariable("OTEL_SDK_DISABLED", $previousOtelDisabled, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", $previousPythonUtf8, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $previousPythonIoEncoding, "Process")
        }
    }
    elseif ($ReviewerAuditSmoke) {
        if (-not $RunId) {
            throw "-ReviewerAuditSmoke requires -RunId, for example RUN-CREWAI-XXXXXXXXXXXX."
        }
        $previousPythonUtf8 = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
        $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
        try {
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
            & $pythonExecutable -m openharness.invest_research.run_reviewer_audit_smoke `
                --run-id $RunId
        }
        finally {
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", $previousPythonUtf8, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $previousPythonIoEncoding, "Process")
        }
    }
    elseif ($ReportSectionsSmoke) {
        if (-not $RunId) {
            throw "-ReportSectionsSmoke requires -RunId, for example RUN-CREWAI-XXXXXXXXXXXX."
        }
        $previousPythonUtf8 = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
        $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
        try {
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
            & $pythonExecutable -m openharness.invest_research.run_report_sections_smoke `
                --run-id $RunId
        }
        finally {
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", $previousPythonUtf8, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $previousPythonIoEncoding, "Process")
        }
    }
    elseif ($ReportWriterSmoke) {
        if (-not $RunId) {
            throw "-ReportWriterSmoke requires -RunId, for example RUN-CREWAI-XXXXXXXXXXXX."
        }
        $previousPythonUtf8 = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
        $previousPythonIoEncoding = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "Process")
        try {
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
            & $pythonExecutable -m openharness.invest_research.run_report_writer_smoke `
                --run-id $RunId
        }
        finally {
            [Environment]::SetEnvironmentVariable("PYTHONUTF8", $previousPythonUtf8, "Process")
            [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", $previousPythonIoEncoding, "Process")
        }
    }
    else {
        & $openHarnessLauncher @OpenHarnessArgs
    }

    if ($null -ne $LASTEXITCODE) {
        $exitCode = $LASTEXITCODE
    }
}
finally {
    [Environment]::SetEnvironmentVariable(
        "TAVILY_API_KEY",
        $previousTavilyKey,
        "Process"
    )
    [Environment]::SetEnvironmentVariable("ARK_API_KEY", $previousArkKey, "Process")
    [Environment]::SetEnvironmentVariable("ARK_API_KEYS", $previousArkKeys, "Process")
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $previousOpenAiKey, "Process")
    [Environment]::SetEnvironmentVariable(
        "OPENHARNESS_DATA_DIR",
        $previousOpenHarnessDataDir,
        "Process"
    )
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    if ($arkKeyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($arkKeyPointer)
    }
    if ($null -ne $secureKey) {
        $secureKey.Dispose()
    }
    $encryptedKey = $null
    $encryptedArkKey = $null
    Pop-Location
}

exit $exitCode
