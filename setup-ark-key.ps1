[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This setup script requires Windows DPAPI."
}

$projectRoot = Split-Path -Parent $PSCommandPath
$secretDirectory = Join-Path $projectRoot ".openharness\secrets"
$secretPath = Join-Path $secretDirectory "ark-key.dpapi"

New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null

$secureKey = Read-Host "Enter Volcengine Ark API Key (input is hidden)" -AsSecureString
try {
    if ($secureKey.Length -eq 0) {
        throw "No Ark API Key was entered. Nothing was saved."
    }

    # Windows DPAPI encryption: only this Windows user on this computer can
    # decrypt the credential. The plaintext is never written to disk.
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

Write-Host "Ark credential encrypted and saved." -ForegroundColor Green
Write-Host "Stored at: .openharness\secrets\ark-key.dpapi"
Write-Host "The file is ignored by Git. Do not copy or commit it."
