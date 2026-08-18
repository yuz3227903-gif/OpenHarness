[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This setup script requires Windows DPAPI."
}

$projectRoot = Split-Path -Parent $PSCommandPath
$secretDirectory = Join-Path $projectRoot ".openharness\secrets"
$secretPath = Join-Path $secretDirectory "tavily-key.dpapi"

New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null

$secureKey = Read-Host "Enter Tavily API Key (input is hidden)" -AsSecureString
try {
    if ($secureKey.Length -eq 0) {
        throw "No Tavily API Key was entered. Nothing was saved."
    }

    # On Windows this uses DPAPI. Only the same Windows user on this machine
    # can decrypt the stored ciphertext.
    $encryptedKey = ConvertFrom-SecureString -SecureString $secureKey
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($secretPath, $encryptedKey, $utf8WithoutBom)
}
finally {
    if ($null -ne $secureKey) {
        $secureKey.Dispose()
    }
    $encryptedKey = $null
}

Write-Host "Tavily credential encrypted and saved." -ForegroundColor Green
Write-Host "Stored at: .openharness\secrets\tavily-key.dpapi"
Write-Host "The file is ignored by Git. Do not copy or commit it."
Write-Host "Start with: .\run-investment-research.ps1"
