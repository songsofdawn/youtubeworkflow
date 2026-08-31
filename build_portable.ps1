[CmdletBinding()]
param(
    [ValidateSet("cpu", "gpu", "all")]
    [string]$Edition = "all",
    [string]$Version = "0.4.0",
    [string]$PythonVersion = "3.11.9",
    [switch]$SkipArchive
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$DistRoot = Join-Path $ProjectRoot "dist"
$CacheRoot = Join-Path $ProjectRoot "build\portable-cache"
$HostPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $HostPython -PathType Leaf)) {
    $HostPython = Join-Path $ProjectRoot ".venv_stage3\Scripts\python.exe"
}

function Assert-File([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
}

function Assert-Directory([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label not found: $Path"
    }
}

function Reset-PackageDirectory([string]$Path) {
    $resolvedParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $Path)).TrimEnd('\')
    $resolvedDist = [System.IO.Path]::GetFullPath($DistRoot).TrimEnd('\')
    if ($resolvedParent -ne $resolvedDist) {
        throw "Refusing to reset a directory outside dist: $Path"
    }
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Copy-Directory([string]$Source, [string]$Destination) {
    Assert-Directory $Source "Source directory"
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Copy-Item -Path (Join-Path $Source "*") -Destination $Destination -Recurse -Force
}

function Invoke-External([string]$Executable, [string[]]$Arguments) {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Executable @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $Executable $($Arguments -join ' ')"
    }
}

function Prepare-PythonRuntime([string]$PackageRoot, [string]$SelectedEdition) {
    $embedName = "python-$PythonVersion-embed-amd64.zip"
    $embedZip = Join-Path $CacheRoot $embedName
    $embedUrl = "https://www.python.org/ftp/python/$PythonVersion/$embedName"
    New-Item -ItemType Directory -Path $CacheRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $embedZip -PathType Leaf)) {
        Write-Host "[DOWNLOAD] $embedUrl"
        Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -UseBasicParsing
    }

    $runtimeRoot = Join-Path $PackageRoot "runtime\python"
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    Expand-Archive -LiteralPath $embedZip -DestinationPath $runtimeRoot -Force

    $pythonMinor = ($PythonVersion.Split('.')[0..1] -join '')
    $pthPath = Join-Path $runtimeRoot "python$pythonMinor._pth"
    Assert-File $pthPath "Python embedded path file"
    @(
        "python$pythonMinor.zip"
        "."
        "..\.."
        "Lib\site-packages"
        "import site"
    ) | Set-Content -LiteralPath $pthPath -Encoding ASCII

    $sitePackages = Join-Path $runtimeRoot "Lib\site-packages"
    New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
    $pythonMajorMinor = ($PythonVersion.Split('.')[0..1] -join '.')
    $pythonAbi = "cp$pythonMinor"
    $targetWheelArguments = @(
        "--platform", "win_amd64",
        "--python-version", $pythonMajorMinor,
        "--implementation", "cp",
        "--abi", $pythonAbi,
        "--no-deps"
    )
    $baseRequirements = Join-Path $ProjectRoot "requirements_portable_cpu.lock.txt"
    Invoke-External $HostPython @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "--quiet",
        "--no-compile",
        "--target", $sitePackages,
        "--requirement", $baseRequirements
        $targetWheelArguments
    )
    if ($SelectedEdition -eq "gpu") {
        $gpuRequirements = Join-Path $ProjectRoot "requirements_portable_gpu_extra.lock.txt"
        Invoke-External $HostPython @(
            "-m", "pip", "install",
            "--disable-pip-version-check",
            "--quiet",
            "--no-compile",
            "--target", $sitePackages,
            "--requirement", $gpuRequirements
            $targetWheelArguments
        )
    }

    $incompatibleExtensions = Get-ChildItem -LiteralPath $sitePackages -Recurse -File -Filter "*.pyd" |
        Where-Object {
            $_.Name -match '\.cp(?<abi>[0-9]+)-' -and $Matches['abi'] -ne $pythonMinor
        }
    if ($incompatibleExtensions) {
        $names = $incompatibleExtensions.FullName -join "`n"
        throw "Portable runtime contains extensions for the wrong Python ABI (expected $pythonAbi):`n$names"
    }

    return @{
        RuntimeRoot = $runtimeRoot
        PythonExe = Join-Path $runtimeRoot "python.exe"
        EmbedSha256 = (Get-FileHash -LiteralPath $embedZip -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Copy-Application([string]$PackageRoot) {
    Copy-Directory (Join-Path $ProjectRoot "src") (Join-Path $PackageRoot "src")
    Copy-Directory (Join-Path $ProjectRoot "config") (Join-Path $PackageRoot "config")

    $toolsDestination = Join-Path $PackageRoot "tools\bin"
    New-Item -ItemType Directory -Path $toolsDestination -Force | Out-Null
    foreach ($tool in @("deno.exe", "ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe")) {
        $source = Join-Path $ProjectRoot "tools\bin\$tool"
        Assert-File $source "Bundled tool"
        Copy-Item -LiteralPath $source -Destination $toolsDestination -Force
    }

    Copy-Directory `
        (Join-Path $ProjectRoot "models\faster-whisper-large-v3") `
        (Join-Path $PackageRoot "models\faster-whisper-large-v3")

    $biliupSource = Join-Path $ProjectRoot "biliup\bbup-app"
    $biliupDestination = Join-Path $PackageRoot "biliup\bbup-app"
    Copy-Directory (Join-Path $biliupSource "binaries") (Join-Path $biliupDestination "binaries")
    Assert-File (Join-Path $biliupSource "tauri-app.exe") "biliup account application"
    Copy-Item -LiteralPath (Join-Path $biliupSource "tauri-app.exe") -Destination $biliupDestination -Force
    New-Item -ItemType Directory -Path (Join-Path $biliupDestination "data") -Force | Out-Null
    "This directory is intentionally empty. Sign in with tauri-app.exe to create your own account file." |
        Set-Content -LiteralPath (Join-Path $biliupDestination "data\README.txt") -Encoding UTF8

    foreach ($directory in @("candidates", "downloads", "logs", "work", "private\biliup_accounts")) {
        New-Item -ItemType Directory -Path (Join-Path $PackageRoot $directory) -Force | Out-Null
    }
    @"
Put an existing biliup account JSON in this directory if you do not use the bundled login application.
Never send a used copy of this folder to another person.
"@ | Set-Content -LiteralPath (Join-Path $PackageRoot "private\biliup_accounts\README.txt") -Encoding UTF8

    $rootFiles = @(
        "set_runtime.bat",
        "start_panel.bat",
        "START_HERE.bat",
        "login_bilibili.bat",
        ".env.example",
        "requirements_dubbing.txt",
        "THIRD_PARTY_NOTICES.md"
    )
    foreach ($name in $rootFiles) {
        $source = Join-Path $ProjectRoot $name
        Assert-File $source "Application file"
        Copy-Item -LiteralPath $source -Destination (Join-Path $PackageRoot $name) -Force
    }
    $userReadme = Join-Path $ProjectRoot "PORTABLE_README.md"
    Assert-File $userReadme "Portable user README"
    Copy-Item -LiteralPath $userReadme -Destination (Join-Path $PackageRoot "README.md") -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination (Join-Path $PackageRoot ".env") -Force

    Get-ChildItem -LiteralPath (Join-Path $PackageRoot "src") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
}

function Set-EditionConfiguration([string]$PackageRoot, [string]$SelectedEdition) {
    $configPath = Join-Path $PackageRoot "config\stage3_config.json"
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($SelectedEdition -eq "gpu") {
        $config.asr.device = "cuda"
        $config.asr.compute_type = "float16"
    } else {
        $config.asr.device = "cpu"
        $config.asr.compute_type = "int8"
    }
    $config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $configPath -Encoding UTF8
}

function Test-Package([string]$PackageRoot, [string]$PythonExe, [string]$SelectedEdition) {
    $importScript = @"
import json
from pathlib import Path
import requests, dotenv, openai, faster_whisper, ctranslate2, langdetect
root = Path(r'$PackageRoot')
cfg = json.loads((root / 'config' / 'stage3_config.json').read_text(encoding='utf-8-sig'))
assert cfg['asr']['device'] == '$SelectedEdition'.replace('gpu', 'cuda').replace('cpu', 'cpu')
assert (root / 'models' / 'faster-whisper-large-v3' / 'model.bin').stat().st_size > 2_000_000_000
assert not (root / 'biliup' / 'bbup-app' / 'data' / '104407846.json').exists()
assert not (root / 'private' / 'cookies.txt').exists()
print('portable-imports-ok', ctranslate2.__version__, cfg['asr']['device'], cfg['asr']['compute_type'])
"@
    Invoke-External $PythonExe @("-c", $importScript)
    Invoke-External $PythonExe @("-m", "compileall", "-q", (Join-Path $PackageRoot "src"))
    foreach ($module in @("src.download_video", "src.run_stage3", "src.run_dubbing", "src.run_stage4", "src.run_control_panel")) {
        Invoke-External $PythonExe @("-m", $module, "--help")
    }
    Invoke-External (Join-Path $PackageRoot "tools\bin\yt-dlp.exe") @("--version")
    Invoke-External (Join-Path $PackageRoot "tools\bin\ffmpeg.exe") @("-version")
    Invoke-External (Join-Path $PackageRoot "biliup\bbup-app\binaries\biliup.exe") @("--version")
    Get-ChildItem -LiteralPath (Join-Path $PackageRoot "src") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
}

function Write-PackageFileList([string]$PackageRoot) {
    Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Force |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($PackageRoot.Length).TrimStart('\').Replace('\', '/')
            "$relative`t$($_.Length)"
        } | Set-Content -LiteralPath (Join-Path $PackageRoot "PACKAGE_FILES.txt") -Encoding UTF8
}

function New-Archive([string]$PackageRoot) {
    $archivePath = "$PackageRoot.zip"
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    $tar = Get-Command "tar.exe" -ErrorAction Stop
    Push-Location $DistRoot
    try {
        Invoke-External $tar.Source @("-a", "-c", "-f", $archivePath, (Split-Path -Leaf $PackageRoot))
    } finally {
        Pop-Location
    }
    $hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $(Split-Path -Leaf $archivePath)" |
        Set-Content -LiteralPath "$archivePath.sha256.txt" -Encoding ASCII
    return $archivePath
}

function Build-Edition([string]$SelectedEdition) {
    $name = "YouTubeWorkflow-Portable-$($SelectedEdition.ToUpperInvariant())-$Version"
    $packageRoot = Join-Path $DistRoot $name
    Write-Host "[BUILD] $name"
    Reset-PackageDirectory $packageRoot
    Copy-Application $packageRoot
    Set-EditionConfiguration $packageRoot $SelectedEdition
    $runtime = Prepare-PythonRuntime $packageRoot $SelectedEdition

    $manifest = [ordered]@{
        product = "YouTube Workflow Portable"
        version = $Version
        portable = $true
        edition = $SelectedEdition
        python_version = $PythonVersion
        python_embed_sha256 = $runtime.EmbedSha256
        asr_model = "faster-whisper-large-v3"
        asr_device = if ($SelectedEdition -eq "gpu") { "cuda" } else { "cpu" }
        asr_compute_type = if ($SelectedEdition -eq "gpu") { "float16" } else { "int8" }
        includes_biliup = $true
        includes_user_credentials = $false
        created_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $packageRoot "portable_manifest.json") -Encoding UTF8

    Test-Package $packageRoot $runtime.PythonExe $SelectedEdition
    Write-PackageFileList $packageRoot

    $size = (Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Force | Measure-Object Length -Sum).Sum
    Write-Host ("[READY] {0} ({1:N2} GB unpacked)" -f $packageRoot, ($size / 1GB))
    if (-not $SkipArchive) {
        $archive = New-Archive $packageRoot
        Write-Host "[ARCHIVE] $archive"
    }
}

Assert-File $HostPython "Host Python build interpreter"
Assert-File (Join-Path $ProjectRoot "requirements_portable_cpu.lock.txt") "CPU requirements"
Assert-File (Join-Path $ProjectRoot "requirements_portable_gpu_extra.lock.txt") "GPU requirements"
Assert-Directory (Join-Path $ProjectRoot "models\faster-whisper-large-v3") "Whisper model"
Assert-File (Join-Path $ProjectRoot "biliup\bbup-app\binaries\biliup.exe") "biliup CLI"
Assert-File (Join-Path $ProjectRoot "biliup\bbup-app\tauri-app.exe") "biliup account application"
New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null

$editions = if ($Edition -eq "all") { @("cpu", "gpu") } else { @($Edition) }
foreach ($selected in $editions) {
    Build-Edition $selected
}

Write-Host "[DONE] Portable build completed."
