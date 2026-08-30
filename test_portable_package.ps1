[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [int]$Port = 18765,
    [switch]$LoadModel,
    [switch]$ExpectCleanCredentials,
    [switch]$TestWorkerEntrypoint
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath $PackageRoot).Path
$Python = Join-Path $Root "runtime\python\python.exe"
$Model = Join-Path $Root "models\faster-whisper-large-v3"
$Config = Join-Path $Root "config\stage3_config.json"
$UserReadme = Join-Path $Root "README.md"
$Launcher = Join-Path $Root "START_HERE.bat"

foreach ($path in @($Python, (Join-Path $Model "model.bin"), $Config, $UserReadme, $Launcher)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required Portable file is missing: $path"
    }
}

$profile = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$device = [string]$profile.asr.device
$computeType = [string]$profile.asr.compute_type
$expectedAbi = (& $Python -c "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0 -or -not $expectedAbi) {
    throw "Cannot determine the Portable Python ABI."
}
$incompatibleExtensions = Get-ChildItem -LiteralPath (Join-Path $Root "runtime\python\Lib\site-packages") -Recurse -File -Filter "*.pyd" |
    Where-Object {
        $_.Name -match '\.cp(?<abi>[0-9]+)-' -and "cp$($Matches['abi'])" -ne $expectedAbi
    }
if ($incompatibleExtensions) {
    throw "Portable package contains native extensions for the wrong Python ABI: $($incompatibleExtensions.FullName -join ', ')"
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PORTABLE_TEST_ROOT = $Root
$env:PORTABLE_TEST_MODEL = $Model
$env:PORTABLE_TEST_DEVICE = $device
$env:PORTABLE_TEST_COMPUTE = $computeType

$runtimeCode = @"
import os, sys
sys.path.insert(0, os.environ['PORTABLE_TEST_ROOT'])
device = os.environ['PORTABLE_TEST_DEVICE']
if device == 'cuda':
    from src.stage3.cuda_runtime import configure_cuda_runtime
    state = configure_cuda_runtime(require_dlls=True)
    import ctranslate2
    count = ctranslate2.get_cuda_device_count()
    print('CUDA_RUNTIME_OK', state['registered_directory_count'], count)
    if count < 1:
        raise SystemExit('No CUDA device detected')
if os.environ.get('PORTABLE_TEST_LOAD_MODEL') == '1':
    from faster_whisper import WhisperModel
    WhisperModel(
        os.environ['PORTABLE_TEST_MODEL'],
        device=device,
        compute_type=os.environ['PORTABLE_TEST_COMPUTE'],
    )
    print('MODEL_LOAD_OK', device, os.environ['PORTABLE_TEST_COMPUTE'])
else:
    import faster_whisper, ctranslate2
    print('RUNTIME_IMPORT_OK', device, ctranslate2.__version__)
"@
$env:PORTABLE_TEST_LOAD_MODEL = if ($LoadModel) { "1" } else { "0" }
& $Python -c $runtimeCode
if ($LASTEXITCODE -ne 0) {
    throw "Portable ASR runtime validation failed with exit code $LASTEXITCODE"
}

foreach ($module in @("src.download_video", "src.run_stage3", "src.run_stage4")) {
    & $Python -m $module --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Portable module entrypoint failed: $module"
    }
}

if ($TestWorkerEntrypoint) {
    $workerCode = @"
import os
from pathlib import Path
from src.control_panel.app import ControlPanelApp
root = Path(os.environ['PORTABLE_TEST_ROOT'])
app = ControlPanelApp(root)
try:
    command = app.worker._build_commands({
        'kind': 'download',
        'target': 'portable-entrypoint-test',
        'payload': {'url': 'https://youtu.be/abcdefghijk'},
    })[0][1]
    assert command[1:3] == ['-m', 'src.download_video'], command
    exit_code = app.worker._run_command(
        'portable-entrypoint-test',
        command[:3] + ['--help'],
        root / 'work' / 'portable-worker-entrypoint.log',
    )
    assert exit_code == 0, exit_code
    print('WORKER_ENTRYPOINT_OK', ' '.join(command[:3]))
finally:
    app.close()
"@
    & $Python -c $workerCode
    if ($LASTEXITCODE -ne 0) {
        throw "Portable worker entrypoint validation failed with exit code $LASTEXITCODE"
    }
}

$process = Start-Process `
    -FilePath $Python `
    -ArgumentList @("src\run_control_panel.py", "--no-browser", "--port", [string]$Port) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru
try {
    $health = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            break
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if ($null -eq $health) {
        throw "Portable control panel did not become ready on port $Port"
    }
    if (-not $health.ready) {
        throw "Portable health endpoint reported ready=false"
    }
    if ($ExpectCleanCredentials -and (
        $health.checks.youtube_api -or
        $health.checks.youtube_cookies -or
        $health.checks.deepseek_api -or
        $health.checks.biliup_account
    )) {
        throw "Portable release unexpectedly contains user credentials"
    }
    [PSCustomObject]@{
        PackageRoot = $Root
        Edition = $health.profile.edition
        Ready = $health.ready
        PythonRuntime = $health.checks.python_runtime
        Tools = $health.checks.tools
        WhisperModel = $health.checks.whisper_model
        YouTubeApi = $health.checks.youtube_api
        YouTubeCookies = $health.checks.youtube_cookies
        DeepSeekApi = $health.checks.deepseek_api
        Biliup = $health.checks.biliup
        BiliupAccount = $health.checks.biliup_account
    } | Format-List
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit()
    }
}
