[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$PlannerSmoke,
    [switch]$PlannerWeb,
    [switch]$ResearchWeb,
    [ValidateSet("planner", "fundamental", "industry_competition", "market_catalyst", "risk", "reviewer_arbiter", "report_writer")]
    [string]$AgentSmoke,
    [switch]$ValidateAllAgents,
    [switch]$FullChainValidation,
    [string]$Company = "CATL",
    [string]$AsOfDate = (Get-Date -Format "yyyy-MM-dd"),
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$OpenHarnessArgs
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSCommandPath
$secretPath = Join-Path $projectRoot ".openharness\secrets\tavily-key.dpapi"
$openHarnessLauncher = Join-Path $projectRoot "run-openh.ps1"
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"

$needsTavily = $PlannerSmoke -or $PlannerWeb -or $ResearchWeb -or $ValidateAllAgents -or $FullChainValidation
if ($AgentSmoke -and $AgentSmoke -notin @("reviewer_arbiter", "report_writer")) {
    $needsTavily = $true
}
if ($needsTavily -and -not (Test-Path -LiteralPath $secretPath)) {
    throw "Tavily is not configured. Run .\setup-tavily-key.ps1 first."
}

if ($PlannerSmoke -or $PlannerWeb -or $ResearchWeb -or $AgentSmoke -or $ValidateAllAgents -or $FullChainValidation) {
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
$exitCode = 0

Push-Location $projectRoot
try {
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
