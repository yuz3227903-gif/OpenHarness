[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This setup script requires Windows DPAPI."
}

$projectRoot = Split-Path -Parent $PSCommandPath
$secretDirectory = Join-Path $projectRoot ".openharness\secrets"
$secretPath = Join-Path $secretDirectory "ark-keys.dpapi"

New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null

Write-Host "A group discussion runs several Agents at the same time." -ForegroundColor Cyan
Write-Host "Each concurrent speaker leases a different Ark key, so the number of"
Write-Host "keys you enter here is how many Agents can talk at once."
Write-Host "Enter them comma-separated, for example: ark-aaa,ark-bbb,ark-ccc"
Write-Host ""

$secureKeys = Read-Host "Enter Volcengine Ark API Keys (comma separated, input is hidden)" -AsSecureString
try {
    if ($secureKeys.Length -eq 0) {
        throw "No Ark API Keys were entered. Nothing was saved."
    }

    # Windows DPAPI encryption: only this Windows user on this computer can
    # decrypt the credentials. The plaintext is never written to disk.
    $encryptedKeys = ConvertFrom-SecureString -SecureString $secureKeys
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($secretPath, $encryptedKeys, $utf8WithoutBom)
}
finally {
    if ($null -ne $secureKeys) {
        $secureKeys.Dispose()
    }
    $encryptedKeys = $null
}

Write-Host "Ark key pool encrypted and saved." -ForegroundColor Green
Write-Host "Stored at: .openharness\secrets\ark-keys.dpapi"
Write-Host "The file is ignored by Git. Do not copy or commit it."
Write-Host "Run .\run-investment-research.ps1 -Workbench to use it."
