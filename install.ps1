param(
    [string]$Repo = "gustavosousa2208/lstty",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "lstty\app")
)

$ErrorActionPreference = "Stop"

$headers = @{
    "Accept"     = "application/vnd.github+json"
    "User-Agent" = "lstty-installer"
}

$release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $headers
$tag = $release.tag_name
$asset = $release.assets | Where-Object { $_.name -eq "lstty-windows-x86_64.zip" } | Select-Object -First 1
$checksumAsset = $release.assets | Where-Object { $_.name -eq "checksums.txt" } | Select-Object -First 1
if (-not $asset) {
    throw "Release $tag does not contain lstty-windows-x86_64.zip"
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("lstty-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    $zipPath = Join-Path $tmp "lstty-windows-x86_64.zip"
    Invoke-WebRequest -Uri $asset.browser_download_url -Headers @{ "User-Agent" = "lstty-installer" } -OutFile $zipPath

    if ($checksumAsset) {
        $csPath = Join-Path $tmp "checksums.txt"
        Invoke-WebRequest -Uri $checksumAsset.browser_download_url -Headers @{ "User-Agent" = "lstty-installer" } -OutFile $csPath
        $expected = (Get-Content $csPath | Where-Object { $_ -match "lstty-windows-x86_64\.zip$" } | Select-Object -First 1).Split()[0].ToLowerInvariant()
        $actual = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash.ToLowerInvariant()
        if ($expected -and $actual -ne $expected) {
            throw "SHA-256 mismatch: expected $expected, got $actual"
        }
    }

    if (Test-Path $InstallDir) {
        Remove-Item -Recurse -Force $InstallDir
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Expand-Archive -Path $zipPath -DestinationPath $InstallDir -Force
} finally {
    if (Test-Path $tmp) {
        Remove-Item -Recurse -Force $tmp
    }
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$entries = @()
if ($userPath) { $entries = $userPath.Split(";") | Where-Object { $_ } }
if ($entries -notcontains $InstallDir) {
    $newPath = (@($InstallDir) + $entries) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added $InstallDir to User PATH (open a new terminal to use 'lstty')."
}

Write-Host "Installed lstty $tag to $InstallDir"
