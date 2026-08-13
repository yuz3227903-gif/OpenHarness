[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$PlannerSmoke,
    [switch]$PlannerWeb,
    [switch]$ResearchWeb,
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
$openHarnessLauncher = Join-Path $projectRoot "run-openh.ps1"
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"

$needsTavily = $PlannerSmoke -or $PlannerWeb -or $ResearchWeb -or $ValidateAllAgents -or $FullChainValidation -or $CrewAIFlowSmoke -or $CrewAIFullChainValidation
if ($AgentSmoke -and $AgentSmoke -notin @("reviewer_arbiter", "report_writer")) {
    $needsTavily = $true
}
if ($needsTavily -and -not (Test-Path -LiteralPath $secretPath)) {
    throw "Tavily is not configured. Run .\setup-tavily-key.ps1 first."
}

if ($PlannerSmoke -or $PlannerWeb -or $ResearchWeb -or $AgentSmoke -or $ValidateAllAgents -or $FullChainValidation -or $CrewAIFlowSmoke -or $CrewAIFullChainValidation -or $ReportWriterSmoke -or $ReportSectionsSmoke -or $ReviewerAuditSmoke) {
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
if ($needsTavily) {
    $encryptedKey = [System.IO.File]::ReadAllText($secretPath).Trim()
    if (-not $encryptedKey) {
        throw "The encrypted Tavily credential is empty. Run setup again."
    }
    $secureKey = ConvertTo-SecureString -String $encryptedKey
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
}
$previousTavilyKey = [Environment]::GetEnvironmentVariable("TAVILY_API_KEY", "Process")
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

    if ($PlannerWeb -or $ResearchWeb) {
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
    [Environment]::SetEnvironmentVariable(
        "OPENHARNESS_DATA_DIR",
        $previousOpenHarnessDataDir,
        "Process"
    )
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    if ($null -ne $secureKey) {
        $secureKey.Dispose()
    }
    $encryptedKey = $null
    Pop-Location
}

exit $exitCode
